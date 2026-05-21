"""
Worker Autoscaler — polls RabbitMQ queue depth and resizes the Managed
Instance Group (MIG) of worker VMs.

Pure control loop. No HTTP server. Connects to RabbitMQ Management API
for metrics and Google Compute Engine API for MIG mutations.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os

import aiohttp
from google.cloud import compute_v1

logger = logging.getLogger(__name__)

# ---- Configuration (from environment / K8s ConfigMap + Secrets) ----

RABBITMQ_MGMT_URL: str = os.environ.get(
    "RABBITMQ_MGMT_URL",
    "http://rabbitmq.infra.svc.cluster.local:15672",
)
RABBITMQ_USER: str = os.environ.get("RABBITMQ_DEFAULT_USER", "guest")
RABBITMQ_PASSWORD: str = os.environ.get("RABBITMQ_PASSWORD", "")

MIG_PROJECT: str = os.environ.get("MIG_PROJECT", "")
MIG_REGION: str = os.environ.get("MIG_REGION", "")
MIG_NAME: str = os.environ.get("MIG_NAME", "")

MAX_WORKERS: int = int(os.environ.get("MAX_WORKERS", "10"))
MIN_WORKERS: int = int(os.environ.get("MIN_WORKERS", "0"))
WORKER_FRAGMENT_CAPACITY: int = int(os.environ.get("WORKER_FRAGMENT_CAPACITY", "8"))
POLL_INTERVAL_SECS: int = int(os.environ.get("POLL_INTERVAL_SECS", "30"))
SCALING_COOLDOWN_SECS: int = 180


async def _get_queue_depth() -> int:
    """Poll RabbitMQ management API for fragments.pending queue depth.

    Returns:
        Number of messages ready for delivery, or 0 on error.
    """
    url = f"{RABBITMQ_MGMT_URL}/api/queues/%2f/fragments.pending"
    auth = aiohttp.BasicAuth(RABBITMQ_USER, RABBITMQ_PASSWORD)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=auth, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.error("RabbitMQ API returned status %d", resp.status)
                    return 0
                data = await resp.json()
                messages_ready = data.get("messages_ready", 0)
                consumers = data.get("consumers", 0)
                logger.debug(
                    "Queue depth: messages_ready=%d, consumers=%d",
                    messages_ready, consumers,
                )
                return int(messages_ready)
    except Exception:
        logger.exception("Failed to poll RabbitMQ management API")
        return 0


def _calculate_target(messages_ready: int) -> int:
    """Calculate target MIG size from queue depth.

    target = ceil(messages_ready / WORKER_FRAGMENT_CAPACITY)
    Clamped to [MIN_WORKERS, MAX_WORKERS].
    """
    if messages_ready <= 0:
        return MIN_WORKERS
    target = math.ceil(messages_ready / WORKER_FRAGMENT_CAPACITY)
    return max(MIN_WORKERS, min(MAX_WORKERS, target))


async def _get_current_size(mig_client: compute_v1.RegionInstanceGroupManagersClient) -> int:
    """Read current number of managed instances in the MIG (not target size)."""
    try:
        # Use keyword arguments instead of a request object to avoid API type issues
        pager = await asyncio.to_thread(
            mig_client.list_managed_instances,
            project=MIG_PROJECT,
            region=MIG_REGION,
            instance_group_manager=MIG_NAME,
        )
        # Pager yields ManagedInstance objects directly; count RUNNING instances
        count = 0
        for inst in pager:
            if inst.instance_status == "RUNNING":
                count += 1
        logger.debug("Current MIG running instances: %d", count)
        return count
    except Exception:
        logger.exception("Failed to get MIG current size")
        return 0


async def _resize_mig(
    mig_client: compute_v1.RegionInstanceGroupManagersClient,
    target_size: int,
) -> None:
    """Resize the MIG to the target number of VMs."""
    logger.info("Resizing MIG '%s' in %s/%s to %d VMs",
                MIG_NAME, MIG_PROJECT, MIG_REGION, target_size)
    try:
        request = compute_v1.ResizeRegionInstanceGroupManagerRequest(
            project=MIG_PROJECT,
            region=MIG_REGION,
            instance_group_manager=MIG_NAME,
            size=target_size,
        )
        await asyncio.to_thread(mig_client.resize, request=request)
        logger.info("MIG resize to %d successful", target_size)
    except Exception:
        logger.exception("Failed to resize MIG to %d", target_size)


async def main() -> None:
    """Infinite autoscaler loop.

    Polls RabbitMQ every POLL_INTERVAL_SECS, calculates target size,
    and resizes MIG if needed with a 60-second cooldown between mutations.
    """
    logger.info(
        "Worker Autoscaler starting — "
        "min=%d, max=%d, capacity=%d, interval=%ds, cooldown=%ds",
        MIN_WORKERS, MAX_WORKERS, WORKER_FRAGMENT_CAPACITY,
        POLL_INTERVAL_SECS, SCALING_COOLDOWN_SECS,
    )

    # Initialize MIG client in a thread so blocking credential discovery
    # does not deadlock the asyncio event loop inside a GKE pod.
    mig_client = await asyncio.to_thread(
        compute_v1.RegionInstanceGroupManagersClient,
    )
    last_resize_at = 0.0

    while True:
        loop_start = asyncio.get_event_loop().time()

        try:
            messages_ready = await _get_queue_depth()
            target_size = _calculate_target(messages_ready)
            current_size = await _get_current_size(mig_client)

            if target_size == current_size:
                logger.info(
                    "Queue depth: %d, current=%d, target=%d — no change",
                    messages_ready, current_size, target_size,
                )
            elif loop_start - last_resize_at < SCALING_COOLDOWN_SECS:
                logger.info(
                    "Scaling cooldown active (%.1fs remaining) — "
                    "skipping resize (target=%d, current=%d)",
                    SCALING_COOLDOWN_SECS - (loop_start - last_resize_at),
                    target_size, current_size,
                )
            else:
                await _resize_mig(mig_client, target_size)
                last_resize_at = loop_start

        except Exception:
            logger.exception("Autoscaler loop error — will retry on next poll")

        elapsed = asyncio.get_event_loop().time() - loop_start
        sleep_time = max(0.0, POLL_INTERVAL_SECS - elapsed)
        await asyncio.sleep(sleep_time)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(main())
