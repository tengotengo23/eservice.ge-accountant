"""API-ის rate limit (fixed window) ძვირ ან ბოროტად გამოყენებად ოპერაციებზე.

* LLM გასაღების რეგისტრაცია გასაღებს ყოველ ჯერზე პროვაიდერთან ამოწმებს - ლიმიტის გარეშე პლატფორმა
  მოპარული გასაღებების "შემმოწმებლად" გამოდგებოდა.
* rs.ge პაროლი / SMS კოდი - არასწორი მონაცემების მრავალჯერადი მიწოდება rs.ge-ზე ანგარიშს ბლოკავს.
* დოკუმენტის ამოცნობა - API პროცესში LLM-ს ელოდება (წუთებამდე), ნაკადი უნდა იყოს შეზღუდული.

Redis-ის შეცდომისას ლიმიტი არ მოქმედებს (fail-open + warning): ეს დაცვის დამატებითი ფენაა და მისმა
გაუმართაობამ API არ უნდა გააჩეროს.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class Limit:
    name: str
    limit: int
    window_seconds: int
    per: Literal["tenant", "user"] = "tenant"


LLM_KEY_VALIDATION = Limit("llm_key", 10, 3600)
RSGE_CREDENTIAL = Limit("rsge_credential", 10, 3600)
OTP_SUBMIT = Limit("otp", 10, 600, per="user")
DOCUMENT_EXTRACT = Limit("extract", 60, 60)


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: int) -> int | None:
        """ითვლის მოთხოვნას: None - ნებადართულია, სხვაგვარად - რამდენ წამში შეიძლება ხელახლა ცდა."""


# INCR + EXPIRE ერთ ატომურ ნაბიჯად: TTL-ის გარეშე დარჩენილი გასაღები მომხმარებელს სამუდამოდ დაბლოკავდა
_HIT_LUA = """
local n = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
    ttl = tonumber(ARGV[1])
end
return {n, ttl}
"""


class RedisRateLimiter:
    def __init__(self, redis, prefix: str = "rl"):
        self._script = redis.register_script(_HIT_LUA)
        self._prefix = prefix

    async def hit(self, key: str, limit: int, window_seconds: int) -> int | None:
        count, ttl = await self._script(keys=[f"{self._prefix}:{key}"], args=[window_seconds])
        return None if int(count) <= limit else max(int(ttl), 1)


class InMemoryRateLimiter:
    """dev/test: ერთ პროცესში. prod-ში რამდენიმე replica-ს საერთო მრიცხველი სჭირდება (Redis)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._windows: dict[str, tuple[float, int]] = {}

    async def hit(self, key: str, limit: int, window_seconds: int) -> int | None:
        now = self._clock()
        start, count = self._windows.get(key, (now, 0))
        if now - start >= window_seconds:
            start, count = now, 0
        self._windows[key] = (start, count + 1)
        return None if count + 1 <= limit else max(int(start + window_seconds - now), 1)
