"""
DLQ Monitor consumer — processes dead-lettered fragment messages.

Consumes from fragments.dead (bound to sobel.fragments.dlx exchange).
Inspects x-death header, decides retry vs permanent failure.
Retries use sleep-based delay (no delayed message plugin).
"""

from __future__ import annotations

import asyncio
import json
import logging

import aio_pika

from ..shared.config import Settings
from ..shared.rabbitmq import (
    ExchangeName,
    QueueName,
    RabbitMQManager,
    RoutingKey,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


async def start_dlq_consumer(
    rabbitmq: RabbitMQManager,
    settings: Settings,
) -> asyncio.Task:
    """Bind to fragments.dead queue on sobel.fragments.dlx exchange."""

    async def callback(message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process():
            try:
                payload = json.loads(message.body)
                image_id = payload.get("image_id", "unknown")
                fragment_id = payload.get("fragment_id", -1)

                # Parse x-death header to determine retry count
                death_header = message.headers.get("x-death", [])
                if death_header:
                    retry_count = death_header[0].get("count", 1)
                else:
                    retry_count = 0

                if retry_count < MAX_RETRIES:
                    # Retry: republish with exponential delay
                    delay_ms = (2 ** retry_count) * 1000  # 2s, 4s, 8s
                    delay_secs = delay_ms / 1000.0
                    logger.warning(
                        "DLQ: Retrying fragment %d of image %s "
                        "(attempt %d/%d, delay %.1fs)",
                        fragment_id, image_id,
                        retry_count + 1, MAX_RETRIES, delay_secs,
                    )
                    # Sleep before republishing (no delayed message plugin)
                    await asyncio.sleep(delay_secs)
                    await rabbitmq.publish(
                        exchange=ExchangeName.FRAGMENTS,
                        routing_key=RoutingKey.FRAGMENTS_PENDING,
                        message=payload,
                    )
                else:
                    # Permanent failure — log CRITICAL, message is ACK'd and dropped
                    logger.critical(
                        "DLQ: PERMANENT FAILURE for fragment %d of image %s "
                        "after %d retries. x-death: %s",
                        fragment_id, image_id, retry_count, death_header,
                    )

            except Exception:
                logger.exception("DLQ consumer error — message will be NACK'd")

    task = await rabbitmq.consume(
        queue_name=QueueName.FRAGMENTS_DEAD,
        callback=callback,
        prefetch_count=1,
    )
    return task
