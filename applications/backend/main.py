"""
Backend Service — FastAPI application entry point.

Provides the REST API for image upload, status polling, and result download.
Also consumes image.completed events from sobel.final to update Redis state.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ..shared.config import load_settings
from ..shared.rabbitmq import RabbitMQManager
from ..shared.redis_client import RedisClient
from ..shared.gcs_client import GCSClient
from .dependencies import _state
from .routes import router as api_router
from .consumer import start_completion_consumer

logger = logging.getLogger(__name__)


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

    # Start consumer for sobel.final -> images.completed
    consumer_task = await start_completion_consumer(rabbitmq, redis)
    _state["consumer_task"] = consumer_task

    logger.info("Backend service started")

    yield

    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass

    await redis.close()
    await rabbitmq.close()
    # gcs.close() is no-op for the client (tmp file cleanup)
    logger.info("Backend service stopped")


app = FastAPI(
    title="Sobel Backend",
    description="Image upload and status API for the Sobel distributed image processor",
    lifespan=lifespan,
)

app.include_router(api_router, prefix="/api")


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
