"""API-ის rate limit: fixed window, ცალ-ცალკე მრიცხველები, Redis-ის ატომური ვერსია."""

import os
import uuid

import pytest

from app.security.ratelimit import InMemoryRateLimiter, RedisRateLimiter


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def test_in_memory_window_limits_and_resets():
    clock = Clock()
    limiter = InMemoryRateLimiter(clock)
    assert [await limiter.hit("k", 3, 60) for _ in range(3)] == [None, None, None]
    clock.now += 20
    assert await limiter.hit("k", 3, 60) == 40  # Retry-After: ფანჯრის დასრულებამდე
    assert await limiter.hit("other", 3, 60) is None  # სხვა tenant/მომხმარებელი - ცალკე მრიცხველი
    clock.now += 40
    assert await limiter.hit("k", 3, 60) is None  # ახალი ფანჯარა


@pytest.mark.redis
@pytest.mark.skipif(not os.environ.get("TEST_REDIS_URL"), reason="TEST_REDIS_URL not set")
async def test_redis_limiter_counts_atomically_and_expires():
    import redis.asyncio as redis

    client = redis.from_url(os.environ["TEST_REDIS_URL"])
    limiter = RedisRateLimiter(client, prefix=f"test-rl-{uuid.uuid4().hex[:6]}")
    try:
        assert [await limiter.hit("t", 2, 30) for _ in range(2)] == [None, None]
        retry_after = await limiter.hit("t", 2, 30)
        assert retry_after is not None and 1 <= retry_after <= 30
        keys = await client.keys(f"{limiter._prefix}:*")
        assert len(keys) == 1 and 0 < await client.ttl(keys[0]) <= 30  # გასაღები TTL-ის გარეშე არ რჩება
    finally:
        for key in await client.keys(f"{limiter._prefix}:*"):
            await client.delete(key)
        await client.aclose()
