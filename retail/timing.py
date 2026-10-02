"""Timing utilities for realistic request patterns and resilient retries."""
import asyncio
import random


def jittered_sleep(base_ms: float, variance_pct: float = 10.0) -> float:
    """Return a randomized sleep duration around base_ms.

    Args:
        base_ms: Base delay in milliseconds (e.g., 4500 for 4.5 seconds).
        variance_pct: Variance as percentage of base. Defaults to 10% giving [base*0.9, base*1.1].

    Returns:
        Random sleep time in milliseconds.
    """
    return int(base_ms * (1.0 - (variance_pct / 200.0) + random.uniform(0, variance_pct / 100.0)))


def exponential_backoff_with_jitter(
    base_delay: float,
    max_attempts: int,
    current_attempt: int,
    jitter_range: tuple = (0.5, 2.0),
) -> float:
    """Calculate retry delay with exponential backoff and random jitter.

    Args:
        base_delay: Base delay in milliseconds.
        max_attempts: Total retry limit.
        current_attempt: Current attempt number (1-indexed).
        jitter_range: (min, max) multiplier for jitter factor. Defaults to [0.5, 2.0].

    Returns:
        Delay in seconds (useful for asyncio.sleep).
    """
    exponential = base_delay * (2 ** (current_attempt - 1))
    jitter_factor = random.uniform(*jitter_range)
    return min(exponential * jitter_factor, base_delay * 2 ** (max_attempts - 1)) / 1000.0


async def bounded_sleep(ms: float, semaphore=None):
    """Sleep with optional semaphore for bounded concurrency."""
    if semaphore is not None:
        async with semaphore:
            await asyncio.sleep(ms / 1000.0)
    else:
        await asyncio.sleep(ms / 1000.0)


class RateLimiter:
    """Simple rate limiter that spreads requests over time."""

    def __init__(self, requests_per_second: float):
        self.requests_per_second = requests_per_second
        self.interval = 1.0 / requests_per_second if requests_per_second > 0 else None
        self.last_request_time = asyncio.get_event_loop().time()
        self.semaphore = asyncio.Semaphore(1)

    async def acquire(self):
        """Wait until the next allowed request time."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self.last_request_time
        if self.interval is not None and elapsed < self.interval:
            delay = self.interval - elapsed
            await asyncio.sleep(delay)
        self.last_request_time = asyncio.get_event_loop().time()
