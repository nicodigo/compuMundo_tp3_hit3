"""
Fragment tracker — uses Redis SADD to track completed fragments.
"""

from __future__ import annotations

import logging

from ..shared.redis_client import RedisClient

logger = logging.getLogger(__name__)


async def track_fragment(
    redis: RedisClient,
    image_id: str,
    fragment_id: int,
    total_fragments: int = 16,
) -> bool:
    """Track a completed fragment in Redis.

    Args:
        redis: Connected RedisClient.
        image_id: UUID string identifying the image.
        fragment_id: Integer fragment identifier (0-15).
        total_fragments: Expected number of fragments (default 16).

    Returns:
        True if the set reached cardinality == total_fragments
        (this fragment was the last one needed).
        False otherwise.

    Raises:
        ValueError: If fragment_id is outside [0, total_fragments).
    """
    if not 0 <= fragment_id < total_fragments:
        raise ValueError(
            f"fragment_id {fragment_id} out of range [0, {total_fragments})"
        )

    await redis.add_fragment(image_id, fragment_id)
    count = await redis.get_fragment_count(image_id)

    is_complete = count == total_fragments
    if is_complete:
        logger.info(
            "Image %s: all %d fragments tracked",
            image_id, total_fragments,
        )
    else:
        logger.debug(
            "Image %s: fragment %d tracked, now %d/%d",
            image_id, fragment_id, count, total_fragments,
        )

    return is_complete
