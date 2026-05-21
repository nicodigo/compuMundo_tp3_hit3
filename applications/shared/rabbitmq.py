"""
RabbitMQ connection and topology manager.

Uses aio_pika with connect_robust for automatic reconnection.
Declares the full exchange/queue/binding topology on startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from enum import Enum
from typing import Any, Callable, Coroutine

import aio_pika
from aio_pika.abc import (
    AbstractRobustConnection,
    AbstractRobustChannel,
    AbstractExchange,
    AbstractQueue,
    AbstractIncomingMessage,
)

from .config import Settings

__all__ = [
    "RabbitMQManager",
    "ExchangeName",
    "QueueName",
    "RoutingKey",
]

logger = logging.getLogger(__name__)


class ExchangeName(str, Enum):
    IMAGES = "sobel.images"
    FRAGMENTS = "sobel.fragments"
    FRAGMENTS_DLX = "sobel.fragments.dlx"
    RESULTS = "sobel.results"
    FINAL = "sobel.final"


class QueueName(str, Enum):
    IMAGES_NEW = "images.new"
    FRAGMENTS_PENDING = "fragments.pending"
    FRAGMENTS_DEAD = "fragments.dead"
    RESULTS_JOINER = "results.joiner"
    RESULTS_DASHBOARD = "results.dashboard"
    IMAGES_COMPLETED = "images.completed"


class RoutingKey(str, Enum):
    IMAGES_NEW = "images.new"
    FRAGMENTS_PENDING = "fragments.pending"
    FRAGMENTS_DEAD = "fragments.dead"
    IMAGES_COMPLETED = "images.completed"


class RabbitMQManager:
    """Manages a single RabbitMQ connection and its channel.

    Call connect() during lifespan startup, close() during shutdown.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractRobustChannel | None = None
        self._exchanges: dict[str, AbstractExchange] = {}
        self._queues: dict[str, AbstractQueue] = {}

    async def connect(self) -> None:
        """Connect with exponential backoff (1-2-4-8-16-30s, +/-25% jitter)."""
        url = self._settings.rabbitmq_url
        delay = 1.0
        max_delay = 30.0
        attempt = 0

        while True:
            attempt += 1
            try:
                self._connection = await aio_pika.connect_robust(url)
                logger.info("RabbitMQ connected on attempt %d", attempt)
                self._channel = await self._connection.channel()
                await self.declare_topology()
                return
            except (aio_pika.exceptions.AMQPConnectionError,
                    aio_pika.exceptions.ChannelNotFoundEntity,
                    OSError) as exc:
                jitter = delay * random.uniform(-0.25, 0.25)
                actual = delay + jitter
                logger.warning(
                    "RabbitMQ connection attempt %d failed: %s. Retrying in %.1fs",
                    attempt,
                    exc,
                    actual,
                )
                await asyncio.sleep(actual)
                delay = min(delay * 2.0, max_delay)

    async def close(self) -> None:
        """Graceful shutdown. Close channel, then connection."""
        if self._channel and not self._channel.is_closed:
            await self._channel.close()
        if self._connection and not self._connection.is_closed:
            await self._connection.close()
        logger.info("RabbitMQ connection closed")

    async def declare_topology(self) -> None:
        """Declare all exchanges, queues, and bindings. Idempotent."""
        if not self._channel:
            raise RuntimeError("Channel not initialized. Call connect() first.")

        channel = self._channel

        # -- Exchanges (all durable, non-auto-delete) --
        self._exchanges["images"] = await channel.declare_exchange(
            ExchangeName.IMAGES.value,
            type=aio_pika.ExchangeType.DIRECT,
            durable=True,
            auto_delete=False,
        )
        self._exchanges["fragments"] = await channel.declare_exchange(
            ExchangeName.FRAGMENTS.value,
            type=aio_pika.ExchangeType.DIRECT,
            durable=True,
            auto_delete=False,
        )
        self._exchanges["fragments_dlx"] = await channel.declare_exchange(
            ExchangeName.FRAGMENTS_DLX.value,
            type=aio_pika.ExchangeType.DIRECT,
            durable=True,
            auto_delete=False,
        )
        self._exchanges["results"] = await channel.declare_exchange(
            ExchangeName.RESULTS.value,
            type=aio_pika.ExchangeType.FANOUT,
            durable=True,
            auto_delete=False,
        )
        self._exchanges["final"] = await channel.declare_exchange(
            ExchangeName.FINAL.value,
            type=aio_pika.ExchangeType.DIRECT,
            durable=True,
            auto_delete=False,
        )

        # Give the broker a moment to commit the exchanges.
        await asyncio.sleep(1.0)

        # -- Queues --
        # images.new
        q_images_new = await channel.declare_queue(
            QueueName.IMAGES_NEW,
            durable=True,
            auto_delete=False,
        )
        self._queues["images.new"] = q_images_new

        # fragments.pending (DLX + TTL + priority)
        q_fragments_pending = await channel.declare_queue(
            QueueName.FRAGMENTS_PENDING,
            durable=True,
            auto_delete=False,
            arguments={
                "x-dead-letter-exchange": ExchangeName.FRAGMENTS_DLX.value,
                "x-message-ttl": self._settings.fragment_ttl_ms,
                "x-max-priority": 10,
            },
        )
        self._queues["fragments.pending"] = q_fragments_pending

        # fragments.dead (DLQ target)
        q_fragments_dead = await channel.declare_queue(
            QueueName.FRAGMENTS_DEAD,
            durable=True,
            auto_delete=False,
        )
        self._queues["fragments.dead"] = q_fragments_dead

        # results.joiner (auto-delete)
        q_results_joiner = await channel.declare_queue(
            QueueName.RESULTS_JOINER,
            durable=False,
            auto_delete=True,
        )
        self._queues["results.joiner"] = q_results_joiner

        # results.dashboard (auto-delete)
        q_results_dashboard = await channel.declare_queue(
            QueueName.RESULTS_DASHBOARD,
            durable=False,
            auto_delete=True,
        )
        self._queues["results.dashboard"] = q_results_dashboard

        # images.completed
        q_images_completed = await channel.declare_queue(
            QueueName.IMAGES_COMPLETED,
            durable=True,
            auto_delete=False,
        )
        self._queues["images.completed"] = q_images_completed

        # -- Bindings --
        await q_images_new.bind(ExchangeName.IMAGES.value, routing_key=RoutingKey.IMAGES_NEW.value)
        await q_fragments_pending.bind(
            ExchangeName.FRAGMENTS.value, routing_key=RoutingKey.FRAGMENTS_PENDING.value
        )
        await q_fragments_dead.bind(
            ExchangeName.FRAGMENTS_DLX.value, routing_key=RoutingKey.FRAGMENTS_DEAD.value
        )
        await q_results_joiner.bind(ExchangeName.RESULTS.value)
        await q_results_dashboard.bind(ExchangeName.RESULTS.value)
        await q_images_completed.bind(
            ExchangeName.FINAL.value, routing_key=RoutingKey.IMAGES_COMPLETED.value
        )

        logger.info("RabbitMQ topology declared: %d exchanges, %d queues, %d bindings",
                     len(self._exchanges), len(self._queues), 6)

    async def publish(
        self,
        exchange: ExchangeName | str,
        routing_key: str,
        message: dict[str, Any],
        *,
        delivery_mode: int = 2,
        content_type: str = "application/json",
    ) -> None:
        """Serialize message as JSON and publish.

        Uses publisher confirms.
        """
        if isinstance(exchange, str):
            exchange = ExchangeName(exchange)

        if not self._channel:
            raise RuntimeError("Channel not initialized. Call connect() first.")

        exchange_obj = await self._channel.declare_exchange(
            exchange.value,
            type=aio_pika.ExchangeType.DIRECT if exchange != ExchangeName.RESULTS
            else aio_pika.ExchangeType.FANOUT,
            durable=True,
        )

        body = json.dumps(message, default=str).encode("utf-8")
        await exchange_obj.publish(
            aio_pika.Message(
                body=body,
                delivery_mode=delivery_mode,
                content_type=content_type,
                content_encoding="utf-8",
            ),
            routing_key=routing_key.value if isinstance(routing_key, RoutingKey) else routing_key,
            mandatory=False,
        )

    async def consume(
        self,
        queue_name: QueueName | str,
        callback: Callable[[AbstractIncomingMessage], Coroutine[Any, Any, None]],
        *,
        prefetch_count: int = 1,
        auto_ack: bool = False,
    ) -> asyncio.Task:
        """Start consuming a queue. Returns an asyncio Task that runs forever.

        The callback receives the raw IncomingMessage and must call
        message.ack() / message.nack() as appropriate.
        """
        if isinstance(queue_name, str):
            queue_name = QueueName(queue_name)

        if queue_name.value not in self._queues:
            raise ValueError(f"Queue '{queue_name.value}' not declared in topology. "
                             f"Call declare_topology() first.")

        queue = self._queues[queue_name.value]

        async def _wrapper(message: AbstractIncomingMessage) -> None:
            try:
                await callback(message)
            except aio_pika.exceptions.MessageProcessError:
                # Message already acked/nacked by the callback via message.process()
                pass
            except Exception:
                logger.exception("Unhandled error in consumer callback for queue %s",
                                 queue_name.value)

        async def _run() -> None:
            # Set QoS prefetch before consuming (aio_pika requires it on the channel)
            if not auto_ack:
                await self._channel.set_qos(prefetch_count=prefetch_count)
            await queue.consume(_wrapper, no_ack=auto_ack)
            # Keep the task alive
            while True:
                await asyncio.sleep(3600)

        task = asyncio.create_task(_run())
        logger.info("Consumer started for queue '%s' (prefetch=%d, auto_ack=%s)",
                    queue_name.value, prefetch_count, auto_ack)
        return task
