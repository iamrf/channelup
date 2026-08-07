"""Token-bucket rate limiter: never exceeds capacity-per-period per key."""
import asyncio
import time

from channelup.ratelimit import RateLimiter, TokenBucket


def run(coro):
    return asyncio.run(coro)


def test_bucket_capacity_burst():
    b = TokenBucket(capacity=5, period_seconds=60)
    ok = [run(b.try_acquire()) for _ in range(5)]
    assert ok == [True] * 5
    # dried out
    assert run(b.try_acquire()) is False


def test_bucket_refills_over_time():
    async def scenario():
        b = TokenBucket(capacity=4, period_seconds=1)  # 4 tokens per 1s
        for _ in range(4):
            await b.acquire()
        # refill 1 token after ~0.25s
        await asyncio.sleep(0.3)
        assert await b.try_acquire() is True
    run(scenario())


def test_acquire_waits_until_token_available():
    async def scenario():
        start = time.monotonic()
        b = TokenBucket(capacity=10, period_seconds=1)
        for _ in range(10):
            await b.acquire()
        # 10 spent; next must wait ~0.1s for 1 token refill
        await b.acquire()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.09
    run(scenario())


def test_rate_limiter_separates_keys():
    async def scenario():
        rl = RateLimiter(default_capacity=2, period_seconds=60)
        await rl.acquire("chan_a", 2)   # drains channel a's bucket
        await rl.acquire("chan_a", 2)
        # channel b is separate and untouched
        return await rl._bucket_for("chan_b", None) is not None
    assert run(scenario())


def test_rate_limiter_enforces_cap_per_channel():
    async def scenario():
        rl = RateLimiter(default_capacity=3, period_seconds=60)
        results = []
        bucket = await rl._bucket_for("c", 3)
        for _ in range(5):
            results.append(await bucket.try_acquire())
        return results
    assert run(scenario()) == [True, True, True, False, False]


def test_zero_capacity_rejected():
    try:
        TokenBucket(0)
        assert False, "expected ValueError"
    except ValueError:
        pass