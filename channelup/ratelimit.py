"""Strict rate limiting via token buckets.

Telegram enforces **20 messages/minute per target channel**; we stay safely under
it (default 19). The same token-bucket is reused to keep the LLM provider (e.g.
Gemini 2.5 Flash-Lite) within its own per-minute budget. A bucket never sells a
token it does not have — the consumer always waits for refill.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional


class TokenBucket:
    """Capacity / period token bucket. ``acquire`` blocks until a token is available."""

    def __init__(self, capacity: float, period_seconds: float = 60.0) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = float(capacity)
        self.period = float(period_seconds)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate_per_sec(self) -> float:
        return self.capacity / self.period

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_sec)
            self._last = now

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                # wait just long enough for the deficit to refill
                deficit = tokens - self._tokens
                await asyncio.sleep(min(deficit / self.rate_per_sec, self.period / 8))

    async def try_acquire(self, tokens: float = 1.0) -> bool:
        """Non-blocking; returns True if a token was available."""
        async with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False


class RateLimiter:
    """A registry of token buckets keyed by name (e.g. a Telegram channel id)."""

    def __init__(self, default_capacity: float = 19.0, period_seconds: float = 60.0) -> None:
        self.default_capacity = float(default_capacity)
        self.period = period_seconds
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, capacity: Optional[float] = None) -> None:
        bucket = await self._bucket_for(key, capacity)
        await bucket.acquire()

    async def _bucket_for(self, key: str, capacity: Optional[float]) -> TokenBucket:
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(capacity if capacity is not None else self.default_capacity,
                                     self.period)
                self._buckets[key] = bucket
            return bucket