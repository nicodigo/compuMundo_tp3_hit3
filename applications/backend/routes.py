"""
Backend REST API routes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from ..shared.config import Settings
from ..shared.gcs_client import GCSClient
from ..shared.rabbitmq import ExchangeName, RabbitMQManager, RoutingKey
from ..shared.redis_client import RedisClient
from .main import get_gcs, get_rabbitmq, get_redis, get_settings
from .models import (
    ErrorResponse,
    ImageResultResponse,
    ImageStatusResponse,
    ImageUploadResponse,
)

router = APIRouter(tags=["images"])

# PNG magic bytes
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB


@router.post(
    "/images",
    response_model=ImageUploadResponse,
    status_code=201,
    responses={400: {"model": ErrorResponse}, 413: {"model": ErrorResponse}},
)
async def upload_image(
    file: UploadFile = File(...),
    redis: RedisClient = Depends(get_redis),
    rabbitmq: RabbitMQManager = Depends(get_rabbitmq),
    gcs: GCSClient = Depends(get_gcs),
    settings: Settings = Depends(get_settings),
):
    """Upload a PNG image for Sobel edge detection."""

    # Validate content type
    content_type = file.content_type or ""
    if "image/png" not in content_type and "png" not in content_type:
        raise HTTPException(
            status_code=400,
            detail=f"Only PNG images are accepted. Content-Type: {file.content_type}",
        )

    # Read bytes
    data = await file.read()
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum size: {MAX_UPLOAD_SIZE // (1024*1024)}MB",
        )

    # Validate PNG magic bytes
    if len(data) < len(PNG_MAGIC) or data[:8] != PNG_MAGIC:
        raise HTTPException(
            status_code=400,
            detail="File is not a valid PNG image (invalid magic bytes)",
        )

    # Generate image ID and GCS path
    image_id = str(uuid.uuid4())
    blob_name = f"{image_id}.png"
    filename = file.filename or "untitled.png"

    # Upload to GCS
    gcs_path = await gcs.upload_bytes(
        bucket=settings.gcs_upload_bucket,
        blob_name=blob_name,
        data=data,
    )

    # Write Redis metadata
    created_at = datetime.now(timezone.utc)
    await redis.set_image_meta(
        image_id=image_id,
        meta={
            "filename": filename,
            "gcs_path": gcs_path,
            "status": "uploaded",
            "total_fragments": settings.fragment_grid_size ** 2,
            "created_at": created_at.isoformat(),
        },
    )

    # Publish image.new event
    await rabbitmq.publish(
        exchange=ExchangeName.IMAGES,
        routing_key=RoutingKey.IMAGES_NEW,
        message={
            "image_id": image_id,
            "filename": filename,
            "gcs_path": gcs_path,
            "total_fragments": settings.fragment_grid_size ** 2,
            "timestamp": created_at.isoformat(),
        },
    )

    return ImageUploadResponse(
        image_id=image_id,
        filename=filename,
        gcs_path=gcs_path,
        total_fragments=settings.fragment_grid_size ** 2,
        status="uploaded",
        created_at=created_at,
    )


@router.get(
    "/images/{image_id}/status",
    response_model=ImageStatusResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_image_status(
    image_id: str,
    redis: RedisClient = Depends(get_redis),
):
    """Get the current processing status of an image."""
    meta = await redis.get_image_meta(image_id)
    if meta is None:
        raise HTTPException(
            status_code=404,
            detail=f"Image '{image_id}' not found",
        )

    fragments_completed = await redis.get_fragment_count(image_id)
    total_fragments = int(meta.get("total_fragments", 16))
    processing_time_ms = meta.get("processing_time_ms")
    if processing_time_ms is not None:
        processing_time_ms = int(processing_time_ms)

    return ImageStatusResponse(
        image_id=image_id,
        filename=meta.get("filename", ""),
        status=meta.get("status", "unknown"),
        fragments_completed=fragments_completed,
        total_fragments=total_fragments,
        result_gcs_path=meta.get("result_gcs_path"),
        processing_time_ms=processing_time_ms,
    )


@router.get(
    "/images/{image_id}/result",
    response_model=ImageResultResponse,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
async def get_image_result(
    image_id: str,
    redis: RedisClient = Depends(get_redis),
    gcs: GCSClient = Depends(get_gcs),
    settings: Settings = Depends(get_settings),
):
    """Get a signed download URL for a completed result image."""
    meta = await redis.get_image_meta(image_id)
    if meta is None:
        raise HTTPException(
            status_code=404,
            detail=f"Image '{image_id}' not found",
        )

    status = meta.get("status", "")
    if status != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Image '{image_id}' status is '{status}', not 'completed'. "
                   f"Wait for processing to finish.",
        )

    result_gcs_path = meta.get("result_gcs_path", "")
    # Extract blob name from gs://bucket/blob_name
    if result_gcs_path.startswith("gs://"):
        parts = result_gcs_path[5:].split("/", 1)
        blob_name = parts[1] if len(parts) > 1 else ""
    else:
        # Assume it's just a blob name within the result bucket
        blob_name = result_gcs_path

    signed_url = await gcs.generate_signed_url(
        bucket=settings.gcs_result_bucket,
        blob_name=blob_name,
        expiration_minutes=15,
    )

    return ImageResultResponse(
        image_id=image_id,
        result_gcs_path=result_gcs_path,
        signed_url=signed_url,
        expires_in_minutes=15,
    )
