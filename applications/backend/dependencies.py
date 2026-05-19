"""
Dependency injection helpers for the backend service.

Extracted from main.py to break a circular import:
  main.py → routes.py → main.py
"""

from __future__ import annotations

from typing import Any

from ..shared.config import Settings
from ..shared.gcs_client import GCSClient
from ..shared.rabbitmq import RabbitMQManager
from ..shared.redis_client import RedisClient


# Module-level state (populated by main.py during lifespan startup)
_state: dict[str, Any] = {}


def get_rabbitmq() -> RabbitMQManager:
    return _state["rabbitmq"]


def get_redis() -> RedisClient:
    return _state["redis"]


def get_gcs() -> GCSClient:
    return _state["gcs"]


def get_settings() -> Settings:
    return _state["settings"]
