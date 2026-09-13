"""Rate limiting.

Redis when configured, an in-process fallback otherwise.

The fallback is correct only for a single instance — it is there so local
development and tests need no Redis, not as a production strategy. Running
multiple workers without Redis multiplies every limit by the worker count,
which is why ``Settings.validate_for_production`` refuses to start without
``REDIS_URL``.
"""

from __future__ import annotations

import time
from collections import defaultdict

import redis.asyncio as aioredis

from suliko.config import get_settings


class RateLimiter:
    """Fixed-window counters.

    A sliding window would be more precise, but for "5 logins per 15 minutes"
    the boundary effect is not worth the extra round trips — the worst case is
    an attacker getting 2x the limit across a window boundary, which does not
    change the economics of guessing a password.
    """

    def __init__(self, redis_client: "aioredis.Redis[str] | None" = None) -> None:
        self._redis: aioredis.Redis[str] | None = redis_client
        self._local: dict[str, list[float]] = defaultdict(list)

    async def _hit_count(self, key: str, window_seconds: int) -> int:
        if self._redis is not None:
            pipe = self._redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, window_seconds)
            count, _ = await pipe.execute()
            return int(count)

        now = time.monotonic()
        bucket = self._local[key]
        cutoff = now - window_seconds
        bucket[:] = [t for t in bucket if t > cutoff]
        bucket.append(now)
        return len(bucket)

    async def _current(self, key: str, window_seconds: int) -> int:
        if self._redis is not None:
            value = await self._redis.get(key)
            return int(value) if value else 0

        now = time.monotonic()
        cutoff = now - window_seconds
        bucket = self._local[key]
        bucket[:] = [t for t in bucket if t > cutoff]
        return len(bucket)

    async def _clear(self, key: str) -> None:
        if self._redis is not None:
            await self._redis.delete(key)
        else:
            self._local.pop(key, None)

    # ── Login ───────────────────────────────────────────────────────────────

    async def check_login(self, account_key: str, ip_key: str) -> int | None:
        """Seconds to wait, or None when the attempt may proceed."""
        settings = get_settings()
        window = settings.login_window_seconds

        if await self._current(account_key, window) >= settings.login_max_attempts_per_account:
            return window
        if await self._current(ip_key, window) >= settings.login_max_attempts_per_ip:
            return window
        return None

    async def record_login_failure(self, account_key: str, ip_key: str) -> None:
        window = get_settings().login_window_seconds
        await self._hit_count(account_key, window)
        await self._hit_count(ip_key, window)

    async def clear_login_failures(self, account_key: str) -> None:
        # Only the account counter is cleared on success. The IP counter
        # stands: a successful login to one account does not excuse a burst of
        # failures against others from the same address.
        await self._clear(account_key)

    # ── MFA ─────────────────────────────────────────────────────────────────

    async def check_mfa(self, key: str) -> int | None:
        settings = get_settings()
        if await self._current(key, settings.mfa_window_seconds) >= settings.mfa_max_attempts:
            return settings.mfa_window_seconds
        return None

    async def record_mfa_failure(self, key: str) -> None:
        await self._hit_count(key, get_settings().mfa_window_seconds)

    async def clear_mfa_failures(self, key: str) -> None:
        await self._clear(key)

    # ── Generic writes ──────────────────────────────────────────────────────

    async def check_write(self, key: str) -> int | None:
        settings = get_settings()
        if await self._hit_count(key, 60) > settings.write_max_per_minute:
            return 60
        return None


_limiter: RateLimiter | None = None


async def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        settings = get_settings()
        client = (
            aioredis.from_url(settings.redis_url, decode_responses=True)
            if settings.redis_url
            else None
        )
        _limiter = RateLimiter(client)
    return _limiter


def reset_rate_limiter() -> None:
    """Test hook."""
    global _limiter
    _limiter = None
