"""SMS/OTP კოდის "ადამიანი ციკლში" (human-in-the-loop) მიწოდება.

rs.ge-ს ორფაქტორიან ავტორიზაციას არ ვუვლით გვერდს. როცა პორტალი SMS კოდს ითხოვს:
  1. worker ქმნის OtpChallenge-ს და აგზავნის შეტყობინებას (push / Telegram / ვებ-აპი);
  2. მომხმარებელი იღებს SMS-ს საკუთარ ტელეფონზე და შეჰყავს კოდი ჩვენს აპლიკაციაში;
  3. API (`POST /v1/rsge/otp/{challenge_id}`) კოდს დებს Redis-ში; worker იღებს და ავსებს ფორმას.
კოდი DB-ში არ ინახება, Redis-ში ცოცხლობს მაქსიმუმ რამდენიმე წუთი და ერთჯერადია.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .errors import OtpTimeoutError

_CODE_RE = re.compile(r"^\d{4,8}$")


@dataclass(frozen=True)
class OtpChallenge:
    tenant_id: str
    credential_id: str
    phone_hint: str | None = None  # "***-**-12" - პორტალის გვერდიდან
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ttl_seconds: int = 180

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(seconds=self.ttl_seconds)


class OtpProvider(Protocol):
    async def request_code(self, challenge: OtpChallenge) -> str: ...


Notifier = Callable[[OtpChallenge], Awaitable[None]]


def validate_code(code: str) -> str:
    code = code.strip()
    if not _CODE_RE.match(code):
        raise ValueError("OTP code must be 4-8 digits")
    return code


class InMemoryOtpBroker:
    """dev/test: ერთ პროცესში."""

    def __init__(self, notifier: Notifier | None = None):
        self._pending: dict[str, tuple[str, asyncio.Future[str]]] = {}
        self._notifier = notifier

    async def request_code(self, challenge: OtpChallenge) -> str:
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[challenge.id] = (challenge.tenant_id, fut)
        try:
            if self._notifier:
                await self._notifier(challenge)
            return await asyncio.wait_for(fut, timeout=challenge.ttl_seconds)
        except TimeoutError:
            raise OtpTimeoutError("user did not provide the SMS code in time") from None
        finally:
            self._pending.pop(challenge.id, None)

    def pending(self) -> list[str]:
        return list(self._pending)

    async def submit_code(self, challenge_id: str, code: str, *, tenant_id: str) -> bool:
        entry = self._pending.get(challenge_id)
        if entry is None or entry[0] != tenant_id or entry[1].done():
            return False
        entry[1].set_result(validate_code(code))
        return True


# owner-ის შემოწმება + pending-ის წაშლა + კოდის ჩადება - ერთ ატომურ ოპერაციად: მეორე (თუნდაც
# ერთდროული) მიწოდება ვერ "გაიპარება" და API ცალსახად პასუხობს 404-ით.
_SUBMIT_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
redis.call('RPUSH', KEYS[2], ARGV[2])
redis.call('EXPIRE', KEYS[2], 60)
return 1
"""


class RedisOtpBroker:
    """prod: worker და API სხვადასხვა პროცესშია. BLPOP - ერთჯერადი მიწოდება."""

    def __init__(self, redis, notifier: Notifier | None = None, prefix: str = "otp"):
        self._redis = redis  # redis.asyncio.Redis
        self._notifier = notifier
        self._prefix = prefix
        self._submit = redis.register_script(_SUBMIT_LUA)

    def _pending_key(self, challenge_id: str) -> str:
        return f"{self._prefix}:pending:{challenge_id}"

    def _code_key(self, challenge_id: str) -> str:
        return f"{self._prefix}:code:{challenge_id}"

    async def request_code(self, challenge: OtpChallenge) -> str:
        await self._redis.set(self._pending_key(challenge.id), challenge.tenant_id, ex=challenge.ttl_seconds)
        try:
            if self._notifier:
                await self._notifier(challenge)
            item = await self._redis.blpop([self._code_key(challenge.id)], timeout=challenge.ttl_seconds)
            if item is None:
                raise OtpTimeoutError("user did not provide the SMS code in time")
            value = item[1]
            return value.decode() if isinstance(value, bytes) else value
        finally:
            await self._redis.delete(self._pending_key(challenge.id), self._code_key(challenge.id))

    async def submit_code(self, challenge_id: str, code: str, *, tenant_id: str) -> bool:
        """მხოლოდ იგივე tenant-ს შეუძლია კოდის მიწოდება, მხოლოდ ერთხელ; გამოწურულ/უცნობ challenge-ზე - False."""
        code = validate_code(code)
        accepted = await self._submit(keys=[self._pending_key(challenge_id), self._code_key(challenge_id)],
                                      args=[tenant_id, code])
        return bool(accepted)
