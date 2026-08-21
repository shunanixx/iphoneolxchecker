"""Rate limiting — the guard on both the AI providers' patience and OLX's."""

import asyncio
import time

import pytest

from bot.utils.ratelimit import PoliteLimiter, TokenBucket


async def test_burst_up_to_capacity_is_immediate():
    bucket = TokenBucket(rate=5, period=1.0)
    started = time.monotonic()

    for _ in range(5):
        await bucket.acquire()

    assert time.monotonic() - started < 0.1


async def test_exceeding_the_rate_forces_a_wait():
    bucket = TokenBucket(rate=2, period=0.4)
    for _ in range(2):
        await bucket.acquire()

    started = time.monotonic()
    await bucket.acquire()
    elapsed = time.monotonic() - started

    assert elapsed >= 0.15, "the third call should have been throttled"


async def test_rate_must_be_positive():
    with pytest.raises(ValueError):
        TokenBucket(rate=0)


async def test_polite_limiter_caps_concurrency():
    limiter = PoliteLimiter(rate=100, period=1.0, concurrency=2, min_delay=0, max_delay=0)
    active = 0
    peak = 0

    async def worker():
        nonlocal active, peak
        async with limiter:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(6)))
    assert peak <= 2


async def test_polite_limiter_applies_a_delay_between_requests():
    limiter = PoliteLimiter(rate=100, period=1.0, concurrency=1, min_delay=0.05, max_delay=0.06)
    started = time.monotonic()

    for _ in range(3):
        async with limiter:
            pass

    assert time.monotonic() - started >= 0.15


async def test_penalty_blocks_everyone_not_just_the_caller():
    """A 403 is against the IP, so the backoff has to be global."""
    limiter = PoliteLimiter(rate=100, period=1.0, concurrency=4, min_delay=0, max_delay=0)
    await limiter.penalize(0.2)

    started = time.monotonic()
    async with limiter:
        pass
    assert time.monotonic() - started >= 0.15


async def test_semaphore_is_released_when_the_body_raises():
    limiter = PoliteLimiter(rate=100, period=1.0, concurrency=1, min_delay=0, max_delay=0)

    with pytest.raises(RuntimeError):
        async with limiter:
            raise RuntimeError("boom")

    # If the slot leaked, this would hang rather than complete.
    await asyncio.wait_for(_enter(limiter), timeout=1.0)


async def _enter(limiter: PoliteLimiter) -> None:
    async with limiter:
        pass
