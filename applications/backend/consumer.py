"""
Backend consumer — processes image.completed events from sobel.final.

When an image is fully processed by the joiner, this consumer updates
the Redis metadata to reflect the completion state.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aio_pika

from ..shared.rabbitmq import QueueName, RabbitMQManager
from ..shared.redis_client import RedisClient

logger = logging.getLogger(__name__)


async def start_completion_consumer(
    rabbitmq: RabbitMQManager,
    redis: RedisClient,
) -> asyncio.Task:
    """Bind to images.completed queue on sobel.final exchange."""

    async def callback(message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process():
            try:
                payload = json.loads(message.body)
                image_id = payload.get("image_id", "")
                status = payload.get("status", "completed")

                await redis.update_image_status(image_id, status)

                # Also store result metadata
                await redis.set_image_meta(
                    image_id,
                    {
                        "result_gcs_path": payload.get("result_gcs_path", ""),
                        "processing_time_ms": payload.get("total_processing_time_ms", 0),
                        "status": status,
                    },
                )

                logger.info(
                    "Image %s completed: %d/%d fragments, %dms",
                    image_id,
                    payload.get("successful_fragments", 0),
                    payload.get("total_fragments", 0),
                    payload.get("total_processing_time_ms", 0),
                )
            except Exception:
                logger.exception("Failed to process completion message")

    task = await rabbitmq.consume(
        queue_name=QueueName.IMAGES_COMPLETED,
        callback=callback,
        prefetch_count=1,
    )
    return task
