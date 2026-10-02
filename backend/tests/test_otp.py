import asyncio
import os

import pytest

from app.rsge.errors import OtpTimeoutError
from app.rsge.otp import InMemoryOtpBroker, OtpChallenge, RedisOtpBroker


async def test_in_memory_broker_delivers_code_once_and_checks_tenant():
    broker = InMemoryOtpBroker()
    challenge = OtpChallenge("t-1", "c-1", ttl_seconds=2)
    task = asyncio.create_task(broker.request_code(challenge))
    await asyncio.sleep(0)
    assert not await broker.submit_code(challenge.id, "123456", tenant_id="t-OTHER")
    assert await broker.submit_code(challenge.id, " 123456 ", tenant_id="t-1")
    assert await task == "123456"
    assert not await broker.submit_code(challenge.id, "123456", tenant_id="t-1")  # ერთჯერადი


async def test_in_memory_broker_times_out():
    with pytest.raises(OtpTimeoutError):
        await InMemoryOtpBroker().request_code(OtpChallenge("t", "c", ttl_seconds=0))


async def test_code_format_is_validated():
    broker = InMemoryOtpBroker()
    challenge = OtpChallenge("t", "c", ttl_seconds=1)
    task = asyncio.create_task(broker.request_code(challenge))
    await asyncio.sleep(0)
    with pytest.raises(ValueError):
        await broker.submit_code(challenge.id, "12; DROP", tenant_id="t")
    task.cancel()


@pytest.mark.redis
@pytest.mark.skipif(not os.environ.get("TEST_REDIS_URL"), reason="TEST_REDIS_URL not set")
async def test_redis_broker_between_worker_and_api():
    import redis.asyncio as redis

    client = redis.from_url(os.environ["TEST_REDIS_URL"])
    notified = asyncio.Event()

    async def notify(_challenge):
        notified.set()

    worker = RedisOtpBroker(client, notifier=notify, prefix="test-otp")
    api = RedisOtpBroker(client, prefix="test-otp")
    challenge = OtpChallenge("t-1", "c-1", ttl_seconds=5)
    task = asyncio.create_task(worker.request_code(challenge))
    await notified.wait()
    assert not await api.submit_code(challenge.id, "111111", tenant_id="t-2")
    assert await api.submit_code(challenge.id, "654321", tenant_id="t-1")
    assert await task == "654321"
    assert await client.exists("test-otp:pending:" + challenge.id) == 0
    await client.aclose()


@pytest.mark.redis
@pytest.mark.skipif(not os.environ.get("TEST_REDIS_URL"), reason="TEST_REDIS_URL not set")
async def test_redis_broker_accepts_a_code_only_once_even_concurrently():
    import redis.asyncio as redis

    client = redis.from_url(os.environ["TEST_REDIS_URL"])
    notified = asyncio.Event()

    async def notify(_challenge):
        notified.set()

    worker = RedisOtpBroker(client, notifier=notify, prefix="test-otp1")
    api = RedisOtpBroker(client, prefix="test-otp1")
    challenge = OtpChallenge("t-1", "c-1", ttl_seconds=5)
    task = asyncio.create_task(worker.request_code(challenge))
    await notified.wait()
    results = await asyncio.gather(*(api.submit_code(challenge.id, f"11111{i}", tenant_id="t-1") for i in range(5)))
    assert sorted(results) == [False] * 4 + [True]  # ზუსტად ერთი მიღებულია, დანარჩენები -> 404
    winner = f"11111{results.index(True)}"
    assert await task == winner
    await client.aclose()
