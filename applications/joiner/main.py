"""
Joiner Service — FastAPI application entry point.

No REST API beyond health checks. The service is driven entirely by
RabbitMQ consumption: it listens for fragment.result messages from
the sobel.results fanout exchange, tracks completion in Redis, and
reassembles the final image when all 16 fragments arrive.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ..shared.config import load_settings
from ..shared.rabbitmq import RabbitMQManager
from ..shared.redis_client import RedisClient
from ..shared.gcs_client import GCSClient
from .consumer import start_consumer

logger = logging.getLogger(__name__)

_state: dict[str, Any] = {}


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

    # Start consumer for sobel.results -> results.joiner
    consumer_task = await start_consumer(rabbitmq, redis, gcs, settings)
    _state["consumer_task"] = consumer_task

    logger.info("Joiner service started")

    yield

    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass

    await redis.close()
    await rabbitmq.close()
    logger.info("Joiner service stopped")


app = FastAPI(
    title="Sobel Joiner",
    description="Fragment joiner — consumes fragment.results, reassembles final image",
    lifespan=lifespan,
)


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
