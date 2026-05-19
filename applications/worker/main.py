"""
Worker Service — standalone async consumer for Sobel edge detection.

No HTTP server. Connects to RabbitMQ, consumes fragment.task messages
from fragments.pending (with priority support), applies Sobel filter,
uploads results to GCS, and publishes fragment.result to sobel.results.

Runs as a plain Python asyncio process (asyncio.run(main())).
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from datetime import datetime, timezone
from typing import Any

import aio_pika

from ..shared.config import Settings, load_settings
from ..shared.rabbitmq import (
    ExchangeName,
    QueueName,
    RabbitMQManager,
)
from ..shared.gcs_client import GCSClient
from .sobel_filter import apply_sobel

logger = logging.getLogger(__name__)

WORKER_ID: str = socket.gethostname()


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    """Parse gs://bucket/blob_name or local://bucket/blob_name into (bucket, blob_name)."""
    for prefix in ("gs://", "local://"):
        if uri.startswith(prefix):
            parts = uri[len(prefix):].split("/", 1)
            return parts[0], parts[1]
    raise ValueError(f"Invalid GCS URI: {uri}")


async def main() -> None:
    settings = load_settings()
    logger.info("Worker %s starting (settings loaded)", WORKER_ID)

    rabbitmq = RabbitMQManager(settings)
    gcs = GCSClient(settings)

    await rabbitmq.connect()
    await gcs.connect()

    logger.info("Worker %s connected — consuming fragments.pending", WORKER_ID)

    # Start consuming indefinitely
    async def callback(message: aio_pika.abc.AbstractIncomingMessage) -> None:
        start_time = time.monotonic()
        payload: dict[str, Any] | None = None
        try:
            payload = json.loads(message.body)
            image_id: str = payload["image_id"]
            fragment_id: int = payload["fragment_id"]
            gcs_path: str = payload["gcs_path"]
            row: int = payload["row"]
            col: int = payload["col"]

            # 1. Download fragment from GCS
            bucket, blob_name = _parse_gcs_uri(gcs_path)
            fragment_bytes = await gcs.download_bytes(bucket, blob_name)
            logger.debug(
                "Worker %s: downloaded fragment %d of image %s (%d bytes)",
                WORKER_ID, fragment_id, image_id, len(fragment_bytes),
            )

            # 2. Apply Sobel filter (CPU-bound, run in thread)
            result_bytes = await asyncio.to_thread(apply_sobel, fragment_bytes)
            logger.debug(
                "Worker %s: Sobel applied to fragment %d of image %s",
                WORKER_ID, fragment_id, image_id,
            )

            # 3. Upload result to GCS (result bucket)
            result_blob_name = f"{image_id}/fragment_{fragment_id}_sobel.png"
            result_gcs_path = await gcs.upload_bytes(
                bucket=settings.gcs_result_bucket,
                blob_name=result_blob_name,
                data=result_bytes,
            )

            # 4. Publish fragment.result to sobel.results (fanout)
            processing_time_ms = int((time.monotonic() - start_time) * 1000)
            now = datetime.now(timezone.utc).isoformat()

            result_msg: dict[str, Any] = {
                "image_id": image_id,
                "fragment_id": fragment_id,
                "row": row,
                "col": col,
                "gcs_path": result_gcs_path,
                "status": "success",
                "error": None,
                "processing_time_ms": processing_time_ms,
                "worker_id": WORKER_ID,
                "timestamp": now,
            }

            await rabbitmq.publish(
                exchange=ExchangeName.RESULTS,
                routing_key="",  # fanout ignores routing key
                message=result_msg,
            )

            logger.info(
                "Worker %s: processed fragment %d of image %s in %dms",
                WORKER_ID, fragment_id, image_id, processing_time_ms,
            )

            # ACK the message
            await message.ack()

        except KeyError as exc:
            logger.error("Missing field in fragment.task: %s", exc)
            await message.nack(requeue=False)
        except Exception:
            logger.exception(
                "Worker %s: failed to process fragment %s of image %s",
                WORKER_ID,
                payload.get("fragment_id", "?") if payload else "?",
                payload.get("image_id", "?") if payload else "?",
            )
            await message.nack(requeue=False)

    await rabbitmq.consume(
        queue_name=QueueName.FRAGMENTS_PENDING,
        callback=callback,
        prefetch_count=1,
        auto_ack=False,
    )

    # Keep the process alive forever
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await gcs.close()
        await rabbitmq.close()
        logger.info("Worker %s stopped", WORKER_ID)


if __name__ == "__main__":
    asyncio.run(main())
