"""
Google Cloud Storage async wrapper for upload/download operations.

Uses asyncio.to_thread to run synchronous google-cloud-storage calls
without blocking the event loop.

When GCS_SERVICE_ACCOUNT_KEY is "{}" (local dev), falls back to a local
filesystem backend under /tmp/sobel-storage/.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import tempfile
from datetime import timedelta

from google.cloud import storage

from .config import Settings

__all__ = ["GCSClient"]

logger = logging.getLogger(__name__)

LOCAL_ROOT = pathlib.Path("/tmp/sobel-storage")


class GCSClient:
    """Async GCS client for Sobel workflow blob operations."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: storage.Client | None = None
        self._tmp_key_file: tempfile.NamedTemporaryFile | None = None
        self._local_root: pathlib.Path | None = None

    async def connect(self) -> None:
        """Initialize the GCS client.

        Creates a temporary service account key file from the JSON string.
        google-cloud-storage reads credentials from a file path; the env var
        GOOGLE_APPLICATION_CREDENTIALS points to this temp file.

        When GCS_SERVICE_ACCOUNT_KEY is "{}" (local dev), uses a local
        filesystem backend instead of connecting to Google Cloud.
        """
        key_json = self._settings.gcs_service_account_key

        if not key_json or key_json.strip() in ("", "{}"):
            # Running on GCE with Application Default Credentials — just use them.
            # The service account attached to the VM should have the needed
            # permissions; we don't pre-verify because bucket-level checks
            # require extra IAM roles the SA may not have.
            def _init_adc() -> storage.Client:
                return storage.Client()

            self._client = await asyncio.to_thread(_init_adc)
            logger.info("GCS client initialized via ADC (upload=%s, result=%s)",
                        self._settings.gcs_upload_bucket,
                        self._settings.gcs_result_bucket)
            return

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _blob(self, bucket_name: str, blob_name: str) -> storage.Blob:
        bucket = self._client.bucket(bucket_name)
        return bucket.blob(blob_name)

    def _local_path(self, bucket: str, blob_name: str) -> pathlib.Path:
        return self._local_root / bucket / blob_name

    def _local_uri(self, bucket: str, blob_name: str) -> str:
        return f"local://{bucket}/{blob_name}"

    # ------------------------------------------------------------------
    # Public operations — each delegates to GCS or local
    # ------------------------------------------------------------------

    async def upload_bytes(
        self,
        bucket: str,
        blob_name: str,
        data: bytes,
        content_type: str = "image/png",
    ) -> str:
        if self._local_root is not None:
            return await self._local_upload_bytes(bucket, blob_name, data)

        def _upload() -> str:
            blob = self._blob(bucket, blob_name)
            blob.content_type = content_type
            blob.upload_from_string(data, content_type=content_type)
            return f"gs://{bucket}/{blob_name}"

        uri = await asyncio.to_thread(_upload)
        logger.debug("Uploaded to %s (%d bytes)", uri, len(data))
        return uri

    async def _local_upload_bytes(self, bucket: str, blob_name: str, data: bytes) -> str:
        local_path = self._local_path(bucket, blob_name)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        def _write() -> str:
            local_path.write_bytes(data)
            return self._local_uri(bucket, blob_name)

        uri = await asyncio.to_thread(_write)
        logger.debug("Local upload to %s (%d bytes)", local_path, len(data))
        return uri

    async def download_bytes(self, bucket: str, blob_name: str) -> bytes:
        if self._local_root is not None:
            return await self._local_download_bytes(bucket, blob_name)

        def _download() -> bytes:
            blob = self._blob(bucket, blob_name)
            return blob.download_as_bytes()

        data = await asyncio.to_thread(_download)
        logger.debug("Downloaded gs://%s/%s (%d bytes)", bucket, blob_name, len(data))
        return data

    async def _local_download_bytes(self, bucket: str, blob_name: str) -> bytes:
        local_path = self._local_path(bucket, blob_name)

        def _read() -> bytes:
            return local_path.read_bytes()

        data = await asyncio.to_thread(_read)
        logger.debug("Local download from %s (%d bytes)", local_path, len(data))
        return data

    async def upload_file(
        self,
        bucket: str,
        blob_name: str,
        file_path: str,
        content_type: str = "image/png",
    ) -> str:
        if self._local_root is not None:
            return await self._local_upload_file(bucket, blob_name, file_path)

        def _upload() -> str:
            blob = self._blob(bucket, blob_name)
            blob.content_type = content_type
            blob.upload_from_filename(file_path, content_type=content_type)
            return f"gs://{bucket}/{blob_name}"

        uri = await asyncio.to_thread(_upload)
        logger.debug("Uploaded file %s to %s", file_path, uri)
        return uri

    async def _local_upload_file(self, bucket: str, blob_name: str, file_path: str) -> str:
        local_path = self._local_path(bucket, blob_name)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        import shutil
        def _copy() -> str:
            shutil.copy2(file_path, local_path)
            return self._local_uri(bucket, blob_name)

        uri = await asyncio.to_thread(_copy)
        logger.debug("Local copy %s -> %s", file_path, local_path)
        return uri

    async def exists(self, bucket: str, blob_name: str) -> bool:
        if self._local_root is not None:
            return await asyncio.to_thread(
                lambda: self._local_path(bucket, blob_name).exists()
            )

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
        if self._local_root is not None:
            # Return a URL the backend's /storage endpoint can serve
            return f"http://backend:8000/storage/{bucket}/{blob_name}"

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
