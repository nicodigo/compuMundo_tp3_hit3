"""
Google Cloud Storage async wrapper for upload/download operations.

Uses asyncio.to_thread to run synchronous google-cloud-storage calls
without blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from datetime import timedelta

from google.cloud import storage

from .config import Settings

__all__ = ["GCSClient"]

logger = logging.getLogger(__name__)


class GCSClient:
    """Async GCS client for Sobel workflow blob operations."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: storage.Client | None = None
        self._tmp_key_file: tempfile.NamedTemporaryFile | None = None

    async def connect(self) -> None:
        """Initialize the GCS client.

        Creates a temporary service account key file from the JSON string.
        google-cloud-storage reads credentials from a file path; the env var
        GOOGLE_APPLICATION_CREDENTIALS points to this temp file.
        """
        key_json = self._settings.gcs_service_account_key

        def _init() -> storage.Client:
            self._tmp_key_file = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                delete=False,
            )
            self._tmp_key_file.write(key_json)
            self._tmp_key_file.flush()
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self._tmp_key_file.name
            client = storage.Client()
            logger.info("GCS client initialized (upload=%s, result=%s)",
                        self._settings.gcs_upload_bucket,
                        self._settings.gcs_result_bucket)
            return client

        self._client = await asyncio.to_thread(_init)

    async def close(self) -> None:
        """Clean up the temporary service account key file."""
        if self._tmp_key_file:
            path = self._tmp_key_file.name
            self._tmp_key_file.close()
            try:
                os.unlink(path)
            except OSError:
                pass
            self._tmp_key_file = None
            if "GOOGLE_APPLICATION_CREDENTIALS" in os.environ:
                del os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
            logger.debug("GCS temporary credentials cleaned up")

    def _blob(self, bucket_name: str, blob_name: str) -> storage.Blob:
        bucket = self._client.bucket(bucket_name)
        return bucket.blob(blob_name)

    async def upload_bytes(
        self,
        bucket: str,
        blob_name: str,
        data: bytes,
        content_type: str = "image/png",
    ) -> str:
        def _upload() -> str:
            blob = self._blob(bucket, blob_name)
            blob.content_type = content_type
            blob.upload_from_string(data, content_type=content_type)
            return f"gs://{bucket}/{blob_name}"

        uri = await asyncio.to_thread(_upload)
        logger.debug("Uploaded to %s (%d bytes)", uri, len(data))
        return uri

    async def download_bytes(self, bucket: str, blob_name: str) -> bytes:
        def _download() -> bytes:
            blob = self._blob(bucket, blob_name)
            return blob.download_as_bytes()

        data = await asyncio.to_thread(_download)
        logger.debug("Downloaded gs://%s/%s (%d bytes)", bucket, blob_name, len(data))
        return data

    async def upload_file(
        self,
        bucket: str,
        blob_name: str,
        file_path: str,
        content_type: str = "image/png",
    ) -> str:
        def _upload() -> str:
            blob = self._blob(bucket, blob_name)
            blob.content_type = content_type
            blob.upload_from_filename(file_path, content_type=content_type)
            return f"gs://{bucket}/{blob_name}"

        uri = await asyncio.to_thread(_upload)
        logger.debug("Uploaded file %s to %s", file_path, uri)
        return uri

    async def exists(self, bucket: str, blob_name: str) -> bool:
        def _exists() -> bool:
            blob = self._blob(bucket, blob_name)
            return blob.exists()

        return await asyncio.to_thread(_exists)

    async def generate_signed_url(
        self,
        bucket: str,
        blob_name: str,
        expiration_minutes: int = 15,
    ) -> str:
        def _sign() -> str:
            blob = self._blob(bucket, blob_name)
            url = blob.generate_signed_url(
                expiration=timedelta(minutes=expiration_minutes),
                method="GET",
            )
            return url

        url = await asyncio.to_thread(_sign)
        logger.debug("Signed URL generated for gs://%s/%s (expires in %dm)",
                     bucket, blob_name, expiration_minutes)
        return url
