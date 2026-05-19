"""
Redis async client for the Sobel image processing workflow.

Thin wrapper around redis.asyncio with application-specific helpers.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import redis.asyncio as aioredis

from .config import Settings

__all__ = ["RedisClient"]

logger = logging.getLogger(__name__)


def _image_meta_key(image_id: str) -> str:
    return f"image:{image_id}:meta"


def _image_fragments_key(image_id: str) -> str:
    return f"image:{image_id}:fragments"


class RedisClient:
    """Async Redis client for Sobel workflow state tracking."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        """Connect with exponential backoff, verify with PING."""
        url = self._settings.redis_url
        delay = 1.0
        max_delay = 30.0
        attempt = 0

        while True:
            attempt += 1
            try:
                self._redis = aioredis.from_url(url)
                await self._redis.ping()
                logger.info("Redis connected on attempt %d", attempt)
                return
            except (aioredis.ConnectionError, OSError, TimeoutError) as exc:
                jitter = delay * random.uniform(-0.25, 0.25)
                actual = delay + jitter
                logger.warning(
                    "Redis connection attempt %d failed: %s. Retrying in %.1fs",
                    attempt,
                    exc,
                    actual,
                )
                await asyncio.sleep(actual)
                delay = min(delay * 2.0, max_delay)

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            logger.info("Redis connection closed")

    # ---- Image metadata (hash) ----

    async def set_image_meta(
        self,
        image_id: str,
        meta: dict[str, Any],
        *,
        ttl: int = 3600,
    ) -> None:
        key = _image_meta_key(image_id)
        await self._redis.hset(key, mapping={k: str(v) for k, v in meta.items()})
        await self._redis.expire(key, ttl)
        logger.debug("Image meta set for %s (TTL=%ds)", image_id, ttl)

    async def get_image_meta(self, image_id: str) -> dict[str, Any] | None:
        key = _image_meta_key(image_id)
        raw = await self._redis.hgetall(key)
        if not raw:
            return None
        # redis-py returns bytes keys/values; decode to str
        return {k.decode("utf-8"): v.decode("utf-8") for k, v in raw.items()}

    async def update_image_status(self, image_id: str, status: str) -> None:
        key = _image_meta_key(image_id)
        await self._redis.hset(key, "status", status)
        logger.debug("Image %s status updated to '%s'", image_id, status)

    # ---- Fragment tracking (set) ----

    async def add_fragment(self, image_id: str, fragment_id: int) -> int:
        key = _image_fragments_key(image_id)
        result = await self._redis.sadd(key, fragment_id)
        return result

    async def get_fragment_count(self, image_id: str) -> int:
        key = _image_fragments_key(image_id)
        return await self._redis.scard(key)

    async def get_fragments(self, image_id: str) -> set[int]:
        key = _image_fragments_key(image_id)
        members = await self._redis.smembers(key)
        return {int(m.decode("utf-8")) for m in members}

    async def is_image_complete(self, image_id: str, total: int = 16) -> bool:
        count = await self.get_fragment_count(image_id)
        return count >= total

    async def clear_image(self, image_id: str) -> None:
        await self._redis.delete(
            _image_meta_key(image_id),
            _image_fragments_key(image_id),
        )
        logger.debug("Cleared Redis state for image %s", image_id)

    # ---- Health ----

    async def ping(self) -> bool:
        try:
            await self._redis.ping()
            return True
        except Exception:
            return False
