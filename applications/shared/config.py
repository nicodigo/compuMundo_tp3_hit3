"""
Shared application configuration.

All values are loaded from environment variables set by the Kubernetes
ConfigMap (sobel-config) and Secrets.
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if value is None:
        raise KeyError(f"Required environment variable '{key}' is not set")
    return value


@dataclass(frozen=True)
class Settings:
    # RabbitMQ
    rabbitmq_host: str = field(default_factory=lambda: _require_env("RABBITMQ_HOST"))
    rabbitmq_port: int = field(default_factory=lambda: int(_require_env("RABBITMQ_PORT")))
    rabbitmq_vhost: str = field(default_factory=lambda: _require_env("RABBITMQ_VHOST"))
    rabbitmq_user: str = field(
        default_factory=lambda: os.environ.get("RABBITMQ_DEFAULT_USER", "guest")
    )
    rabbitmq_password: str = field(default_factory=lambda: _require_env("RABBITMQ_PASSWORD"))

    # Redis (optional for workers that don't need Redis)
    redis_host: str = field(default_factory=lambda: os.environ.get("REDIS_HOST", "localhost"))
    redis_port: int = field(default_factory=lambda: int(os.environ.get("REDIS_PORT", "6379")))
    redis_db: int = field(default_factory=lambda: int(os.environ.get("REDIS_DB", "0")))
    redis_password: str = field(default_factory=lambda: os.environ.get("REDIS_PASSWORD", ""))

    # GCS
    gcs_upload_bucket: str = field(default_factory=lambda: _require_env("GCS_UPLOAD_BUCKET"))
    gcs_result_bucket: str = field(default_factory=lambda: _require_env("GCS_RESULT_BUCKET"))
    gcs_service_account_key: str = field(
        default_factory=lambda: os.environ.get("GCS_SERVICE_ACCOUNT_KEY", "")
    )

    # Processing
    fragment_grid_size: int = field(
        default_factory=lambda: int(_require_env("FRAGMENT_GRID_SIZE"))
    )
    fragment_ttl_ms: int = field(
        default_factory=lambda: int(_require_env("FRAGMENT_TTL_MS"))
    )
    max_retries: int = field(default_factory=lambda: int(_require_env("MAX_RETRIES")))
    log_level: str = field(default_factory=lambda: _require_env("LOG_LEVEL"))

    @property
    def rabbitmq_url(self) -> str:
        return (
            f"amqp://{self.rabbitmq_user}:{self.rabbitmq_password}"
            f"@{self.rabbitmq_host}:{self.rabbitmq_port}/{self.rabbitmq_vhost}"
        )

    @property
    def redis_url(self) -> str:
        return (
            f"redis://:{self.redis_password}"
            f"@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        )


def load_settings() -> Settings:
    """Read all config from os.environ. Missing required keys raise KeyError."""
    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return settings
