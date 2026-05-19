"""
Pydantic models for the Backend REST API.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ImageUploadResponse(BaseModel):
    image_id: str
    filename: str
    gcs_path: str
    total_fragments: int
    status: str
    created_at: datetime


class ImageStatusResponse(BaseModel):
    image_id: str
    filename: str
    status: str  # "uploaded" | "processing" | "completed" | "failed"
    fragments_completed: int
    total_fragments: int
    result_gcs_path: str | None = None
    processing_time_ms: int | None = None


class ImageResultResponse(BaseModel):
    image_id: str
    result_gcs_path: str
    signed_url: str
    expires_in_minutes: int = 15


class ErrorResponse(BaseModel):
    detail: str
    image_id: str | None = None
