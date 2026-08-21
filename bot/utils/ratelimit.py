"""Token-bucket rate limiting, shared by the Gemini client and the OLX client.

Both external services are things we are a guest on: Gemini has a
free-tier RPM ceiling we must stay under, and OLX has no API at all and
will block an IP that hammers it. Every outgoing call to either goes
through a limiter here — do not add call sites that bypass it.
"""

import asyncio
import random
import time
from types import TracebackType


class TokenBucket:
    """Allows `rate` operations per `period` seconds, smoothed over time.

    `acquire()` waits until a token is free, so callers never have to
    think about pacing; the bucket is the only thing that knows the
    ceiling.
    """

    def __init__(self, rate: int, period: float = 60.0, *, burst: int | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.period = period
        self.capacity = burst if burst is not None else rate
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        self._updated = now
        self._tokens = min(self.capacity, self._tokens + elapsed * (self.rate / self.period))

    async def acquire(self, tokens: float = 1.0) -> None:
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / (self.rate / self.period)
            await asyncio.sleep(wait)

    async def __aenter__(self) -> "TokenBucket":
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class PoliteLimiter:
    """Token bucket + capped concurrency + jittered delay, for scraping.

    The randomized sleep between requests is deliberate: a perfectly
    regular request train is exactly what looks automated. See
    ARCHITECTURE.md §6.
    """

    def __init__(
        self,
        *,
        rate: int,
        period: float = 60.0,
        concurrency: int = 3,
        min_delay: float = 1.0,
        max_delay: float = 3.0,
    ) -> None:
        self._bucket = TokenBucket(rate=rate, period=period)
        self._sem = asyncio.Semaphore(concurrency)
        self._min_delay = min_delay
        self._max_delay = max_delay
        #: Set by the client when it sees a 403/429; delays everyone.
        self._penalty_until = 0.0
        self._penalty_lock = asyncio.Lock()

    async def penalize(self, seconds: float) -> None:
        """Back off globally after a block signal from the source."""
        async with self._penalty_lock:
            self._penalty_until = max(self._penalty_until, time.monotonic() + seconds)

    async def _wait_out_penalty(self) -> None:
        while True:
            remaining = self._penalty_until - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    async def __aenter__(self) -> "PoliteLimiter":
        await self._sem.acquire()
        try:
            await self._wait_out_penalty()
            await self._bucket.acquire()
            await asyncio.sleep(random.uniform(self._min_delay, self._max_delay))
        except BaseException:
            self._sem.release()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._sem.release()
