"""
Split consumer — processes image.new messages from sobel.images.

Downloads the original PNG from GCS, splits it into a grid of fragments,
uploads each fragment to GCS, and publishes fragment.task messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import aio_pika

from ..shared.config import Settings
from ..shared.gcs_client import GCSClient
from ..shared.rabbitmq import ExchangeName, QueueName, RabbitMQManager, RoutingKey
from ..shared.redis_client import RedisClient
from .splitter import split_image

logger = logging.getLogger(__name__)


async def start_consumer(
    rabbitmq: RabbitMQManager,
    redis: RedisClient,
    gcs: GCSClient,
    settings: Settings,
) -> asyncio.Task:
    """Bind to images.new queue on sobel.images exchange."""

    async def callback(message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process():
            try:
                payload = json.loads(message.body)
                image_id = payload["image_id"]
                gcs_path = payload["gcs_path"]

                # Extract bucket and blob name from URI
                # Format: gs://bucket/blob_name or local://bucket/blob_name
                assert gcs_path.startswith("gs://") or gcs_path.startswith("local://"), \
                    f"Unknown GCS path scheme: {gcs_path}"
                prefix_end = gcs_path.index("://") + 3
                parts = gcs_path[prefix_end:].split("/", 1)
                upload_bucket = parts[0]
                blob_name = parts[1]
                else:
                    upload_bucket = settings.gcs_upload_bucket
                    blob_name = gcs_path

                logger.info("Splitting image %s (bucket=%s, blob=%s)",
                            image_id, upload_bucket, blob_name)

                # Download original image
                image_bytes = await gcs.download_bytes(upload_bucket, blob_name)

                # Split into fragments
                grid_size = settings.fragment_grid_size
                fragments = await asyncio.to_thread(split_image, image_bytes, grid_size)

                # Update Redis status
                await redis.update_image_status(image_id, "processing")

                # Upload each fragment and publish task message
                total = len(fragments)
                for frag in fragments:
                    fragment_id = frag["fragment_id"]
                    frag_blob_name = f"{image_id}/fragment_{fragment_id}.png"

                    # Upload fragment to GCS
                    frag_gcs_path = await gcs.upload_bytes(
                        bucket=upload_bucket,
                        blob_name=frag_blob_name,
                        data=frag["data"],
                    )

                    # Publish fragment.task
                    now = datetime.now(timezone.utc).isoformat()
                    await rabbitmq.publish(
                        exchange=ExchangeName.FRAGMENTS,
                        routing_key=RoutingKey.FRAGMENTS_PENDING,
                        message={
                            "image_id": image_id,
                            "fragment_id": fragment_id,
                            "row": frag["row"],
                            "col": frag["col"],
                            "gcs_path": frag_gcs_path,
                            "width": frag["width"],
                            "height": frag["height"],
                            "total_fragments": total,
                            "timestamp": now,
                        },
                    )

                logger.info(
                    "Image %s split into %d fragments and published",
                    image_id, total,
                )

            except KeyError as exc:
                logger.error("Missing field in image.new message: %s", exc)
                await message.nack(requeue=False)
            except Exception:
                logger.exception("Failed to process image.new message")
                await message.nack(requeue=False)

    task = await rabbitmq.consume(
        queue_name=QueueName.IMAGES_NEW,
        callback=callback,
        prefetch_count=1,
    )
    return task
