"""
Joiner consumer — processes fragment.result messages from sobel.results
fanout exchange.

Tracks completed fragments in Redis. When all 16 fragments arrive,
downloads them, reassembles the final image, uploads to GCS, and
publishes image.completed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import aio_pika

from ..shared.config import Settings
from ..shared.gcs_client import GCSClient
from ..shared.rabbitmq import ExchangeName, QueueName, RabbitMQManager, RoutingKey
from ..shared.redis_client import RedisClient
from .tracker import track_fragment
from .joiner import reassemble_image

logger = logging.getLogger(__name__)


async def start_consumer(
    rabbitmq: RabbitMQManager,
    redis: RedisClient,
    gcs: GCSClient,
    settings: Settings,
) -> asyncio.Task:
    """Bind to results.joiner queue on sobel.results (fanout) exchange."""

    async def callback(message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process():
            try:
                payload = json.loads(message.body)
                image_id = payload["image_id"]
                status = payload.get("status", "")

                # Only track successful fragments
                if status != "success":
                    logger.warning(
                        "Fragment %d of image %s has status '%s' — skipping",
                        payload.get("fragment_id", -1), image_id, status,
                    )
                    return

                fragment_id = payload["fragment_id"]
                total_fragments = settings.fragment_grid_size ** 2

                # Track fragment in Redis
                is_complete = await track_fragment(
                    redis, image_id, fragment_id, total_fragments,
                )

                if not is_complete:
                    logger.debug(
                        "Fragment %d/%d tracked for image %s",
                        fragment_id, total_fragments, image_id,
                    )
                    return

                # ---- All fragments received — reassemble! ----
                logger.info(
                    "All %d fragments received for image %s — reassembling",
                    total_fragments, image_id,
                )

                # Get image metadata for dimensions
                meta = await redis.get_image_meta(image_id)
                gcs_path = meta.get("gcs_path", "") if meta else ""
                original_width = int(meta.get("original_width", 0)) if meta else 0
                original_height = int(meta.get("original_height", 0)) if meta else 0

                # Get list of completed fragment IDs
                fragment_ids = sorted(await redis.get_fragments(image_id))

                # Download all processed fragments
                upload_bucket = settings.gcs_upload_bucket
                result_bucket = settings.gcs_result_bucket

                # Fetch the first fragment to determine dimensions if not in meta
                if original_width == 0 or original_height == 0:
                    first_frag_data = await gcs.download_bytes(
                        upload_bucket,
                        f"{image_id}/fragment_{fragment_ids[0]}.png",
                    )
                    # Derive dimensions from the first successful fragment result
                    # The fragment width/height are in the payload
                    first_frag_width = payload.get("width", 0)
                    first_frag_height = payload.get("height", 0)
                    original_width = first_frag_width * settings.fragment_grid_size
                    original_height = first_frag_height * settings.fragment_grid_size

                # Download all fragment results from GCS
                fragments: list[tuple[int, bytes]] = []
                for fid in fragment_ids:
                    frag_blob_name = f"{image_id}/fragment_{fid}_sobel.png"
                    frag_data = await gcs.download_bytes(result_bucket, frag_blob_name)
                    fragments.append((fid, frag_data))

                # Verify we have exactly total_fragments
                if len(fragments) != total_fragments:
                    logger.error(
                        "Expected %d fragments for image %s, got %d. Not reassembling.",
                        total_fragments, image_id, len(fragments),
                    )
                    # Don't publish image.completed — fragments may still arrive
                    return

                # Reassemble
                result_bytes = await asyncio.to_thread(
                    reassemble_image,
                    fragments,
                    original_width,
                    original_height,
                    settings.fragment_grid_size,
                )

                # Upload final result
                result_blob_name = f"{image_id}/final.png"
                result_gcs_path = await gcs.upload_bytes(
                    bucket=result_bucket,
                    blob_name=result_blob_name,
                    data=result_bytes,
                )

                # Publish image.completed
                now = datetime.now(timezone.utc).isoformat()
                processing_time_ms = payload.get("processing_time_ms", 0)

                completion_msg: dict[str, Any] = {
                    "image_id": image_id,
                    "result_gcs_path": result_gcs_path,
                    "status": "completed",
                    "total_fragments": total_fragments,
                    "successful_fragments": len(fragments),
                    "failed_fragments": 0,
                    "total_processing_time_ms": processing_time_ms,
                    "timestamp": now,
                }

                await rabbitmq.publish(
                    exchange=ExchangeName.FINAL,
                    routing_key=RoutingKey.IMAGES_COMPLETED,
                    message=completion_msg,
                )

                # Update Redis status
                await redis.update_image_status(image_id, "completed")
                await redis.set_image_meta(
                    image_id,
                    {
                        "result_gcs_path": result_gcs_path,
                        "status": "completed",
                    },
                )

                logger.info(
                    "Image %s reassembled and published to sobel.final",
                    image_id,
                )

            except KeyError as exc:
                logger.error("Missing field in fragment.result message: %s", exc)
            except Exception:
                logger.exception("Failed to process fragment.result message")

    task = await rabbitmq.consume(
        queue_name=QueueName.RESULTS_JOINER,
        callback=callback,
        prefetch_count=16,  # Allow batching multiple fragments
    )
    return task
