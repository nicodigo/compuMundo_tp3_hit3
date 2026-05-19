"""
Frontend Service — FastAPI application entry point.

Serves the static web UI (HTML/JS/CSS), provides a Server-Sent Events
endpoint for real-time processing progress, and consumes fragment.result
messages from the sobel.results fanout exchange for SSE push.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import aio_pika
from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..shared.config import load_settings
from ..shared.rabbitmq import QueueName, RabbitMQManager
from ..shared.redis_client import RedisClient
from ..shared.gcs_client import GCSClient

logger = logging.getLogger(__name__)

_state: dict[str, Any] = {}

# SSE channel registry: image_id -> list of asyncio.Queue
_sse_channels: dict[str, list[asyncio.Queue]] = {}
_sse_channels_lock = asyncio.Lock()


async def register_sse(image_id: str, queue: asyncio.Queue) -> None:
    async with _sse_channels_lock:
        _sse_channels.setdefault(image_id, []).append(queue)


async def unregister_sse(image_id: str, queue: asyncio.Queue) -> None:
    async with _sse_channels_lock:
        channels = _sse_channels.get(image_id, [])
        if queue in channels:
            channels.remove(queue)
        if not channels:
            _sse_channels.pop(image_id, None)


async def broadcast_sse(image_id: str, data: dict[str, Any]) -> None:
    """Push a fragment.result update to all SSE listeners for an image."""
    async with _sse_channels_lock:
        channels = list(_sse_channels.get(image_id, []))

    if not channels:
        return

    payload = f"data: {json.dumps(data)}\n\n"
    for q in channels:
        await q.put(payload)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()

    rabbitmq = RabbitMQManager(settings)
    redis = RedisClient(settings)
    gcs = GCSClient(settings)

    await rabbitmq.connect()
    await redis.connect()
    await gcs.connect()

    _state["rabbitmq"] = rabbitmq
    _state["redis"] = redis
    _state["gcs"] = gcs
    _state["settings"] = settings

    # Start consumer on sobel.results -> results.dashboard for SSE fanout
    consumer_task = await start_dashboard_consumer(rabbitmq)
    _state["consumer_task"] = consumer_task

    logger.info("Frontend service started")

    yield

    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass

    await redis.close()
    await rabbitmq.close()
    logger.info("Frontend service stopped")


app = FastAPI(
    title="Sobel Frontend",
    description="Web UI with real-time SSE progress for Sobel image processing",
    lifespan=lifespan,
)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/ready")
async def ready():
    try:
        redis = _state.get("redis")
        if redis and await redis.ping():
            return {"status": "ready"}
        return JSONResponse(status_code=503, content={"status": "not ready"})
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not ready"})


@app.get("/config")
async def config():
    """Return frontend configuration for app.js."""
    return {
        "backend_url": os.environ.get(
            "BACKEND_URL",
            "http://backend.apps.svc.cluster.local:8000",
        ),
    }


@app.get("/events/{image_id}")
async def sse_stream(image_id: str):
    """Server-Sent Events endpoint for real-time fragment progress."""

    queue: asyncio.Queue = asyncio.Queue()
    await register_sse(image_id, queue)

    async def event_generator():
        try:
            while True:
                data = await queue.get()
                yield data
        except asyncio.CancelledError:
            pass
        finally:
            await unregister_sse(image_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/")
async def index():
    """Redirect to static index.html."""
    return RedirectResponse(url="/static/index.html")


async def start_dashboard_consumer(
    rabbitmq: RabbitMQManager,
) -> asyncio.Task:
    """Consume fragment.result from sobel.results fanout
    and broadcast to SSE listeners."""

    async def callback(message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process():
            try:
                payload = json.loads(message.body)
                image_id = payload.get("image_id", "")
                if image_id:
                    await broadcast_sse(image_id, payload)
            except Exception:
                logger.exception("Failed to broadcast fragment.result to SSE")

    task = await rabbitmq.consume(
        queue_name=QueueName.RESULTS_DASHBOARD,
        callback=callback,
        prefetch_count=16,
    )
    return task
