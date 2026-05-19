"""
DLQ Monitor Service — FastAPI application entry point.

Consumes dead-lettered fragment messages, inspects x-death headers,
and decides whether to retry (republish with delay) or declare
permanent failure.
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
from .consumer import start_dlq_consumer

logger = logging.getLogger(__name__)

_state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()

    rabbitmq = RabbitMQManager(settings)

    await rabbitmq.connect()

    _state["rabbitmq"] = rabbitmq
    _state["settings"] = settings

    # Start consumer for sobel.fragments.dlx -> fragments.dead
    consumer_task = await start_dlq_consumer(rabbitmq, settings)
    _state["consumer_task"] = consumer_task

    logger.info("DLQ Monitor service started")

    yield

    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass

    await rabbitmq.close()
    logger.info("DLQ Monitor service stopped")


app = FastAPI(
    title="Sobel DLQ Monitor",
    description="Dead-letter queue monitor — consumes fragments.dead, retries or logs permanent failures",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/ready")
async def ready():
    try:
        rabbitmq = _state.get("rabbitmq")
        if rabbitmq and rabbitmq._connection and not rabbitmq._connection.is_closed:
            return {"status": "ready"}
        return JSONResponse(status_code=503, content={"status": "not ready"})
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not ready"})
