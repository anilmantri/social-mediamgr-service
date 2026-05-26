"""
Redis utilities:
  - CacheClient: generic get/set/delete with JSON serialisation
  - RateLimiter: sliding window rate limiter for AI API calls
  - JobStatusCache: fast job status reads without hitting Postgres
"""
import json
from typing import Any

import redis.asyncio as aioredis
import structlog

from app.core.config import settings

log = structlog.get_logger(__name__)


class CacheClient:
    def __init__(self) -> None:
        self._redis = aioredis.from_url(
            str(settings.REDIS_URL),
            encoding="utf-8",
            decode_responses=True,
        )

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._redis.get(key)
            return json.loads(raw) if raw is not None else None
        except Exception as e:
            log.warning("cache_get_error", key=key, error=str(e))
            return None

    async def set(self, key: str, value: Any, ttl: int = settings.REDIS_CACHE_TTL) -> bool:
        try:
            await self._redis.setex(key, ttl, json.dumps(value, default=str))
            return True
        except Exception as e:
            log.warning("cache_set_error", key=key, error=str(e))
            return False

    async def delete(self, key: str) -> bool:
        try:
            await self._redis.delete(key)
            return True
        except Exception as e:
            log.warning("cache_delete_error", key=key, error=str(e))
            return False

    async def exists(self, key: str) -> bool:
        return bool(await self._redis.exists(key))

    async def health_check(self) -> bool:
        try:
            await self._redis.ping()
            return True
        except Exception:
            return False


class RateLimiter:
    """
    Sliding window rate limiter.
    Prevents hammering AI APIs from a single workspace.
    """

    def __init__(self, redis_client: aioredis.Redis) -> None:
        self._redis = redis_client

    async def check_and_increment(
        self,
        key: str,
        max_requests: int,
        window_seconds: int,
    ) -> tuple[bool, int]:
        """
        Returns (allowed: bool, remaining: int).
        Uses a sorted set with timestamps as scores.
        """
        import time
        now = time.time()
        window_start = now - window_seconds

        pipe = self._redis.pipeline()
        # Remove entries outside the window
        pipe.zremrangebyscore(key, "-inf", window_start)
        # Count requests in window
        pipe.zcard(key)
        # Add current request
        pipe.zadd(key, {str(now): now})
        # Set expiry
        pipe.expire(key, window_seconds + 1)

        results = await pipe.execute()
        current_count = results[1]

        if current_count >= max_requests:
            return False, 0
        return True, max_requests - current_count - 1


class JobStatusCache:
    """
    Short-lived cache for generation job status.
    Frontend polls every 2s — this prevents thundering-herd on Postgres.
    """

    KEY_PREFIX = "job_status:"
    TTL = 30  # seconds — short enough to stay fresh

    def __init__(self, cache: CacheClient) -> None:
        self._cache = cache

    def _key(self, job_id: str) -> str:
        return f"{self.KEY_PREFIX}{job_id}"

    async def set_status(self, job_id: str, status_data: dict) -> None:
        await self._cache.set(self._key(job_id), status_data, ttl=self.TTL)

    async def get_status(self, job_id: str) -> dict | None:
        return await self._cache.get(self._key(job_id))

    async def invalidate(self, job_id: str) -> None:
        await self._cache.delete(self._key(job_id))


# ── Singletons ────────────────────────────────────────────────────────────────
_cache_client: CacheClient | None = None


def get_cache_client() -> CacheClient:
    global _cache_client
    if _cache_client is None:
        _cache_client = CacheClient()
    return _cache_client
