"""
Module: Media Upload API
Purpose: Exposes the endpoint that accepts video/audio uploads (up to 500MB),
         validates them, persists them to the configured storage backend, and
         returns a file ID and retrievable URL.
"""
from __future__ import annotations

import logging
import os
import tempfile
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from config.setting import Settings, get_settings
from pipeline.processors.media_validator import (
    ValidationError,
    validate_extension,
    validate_magic_bytes,
    validate_size,
)
from services.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(tags=["uploads"])

_READ_CHUNK_BYTES = 1024 * 1024  # 1 MB
_MAGIC_HEADER_BYTES = 2048       # bytes inspected for content-type detection


async def _stream_to_tempfile(
    file: UploadFile, max_size: int
) -> tuple[str, int, bytes]:
    """Stream an upload to a temp file in fixed-size chunks, enforcing the size
    cap as we read so an oversized file is never fully buffered.

    Args:
        file: The incoming upload.
        max_size: Maximum permitted size in bytes.

    Returns:
        A tuple of (temp_path, total_bytes, header_bytes). The caller owns the
        temp file and is responsible for deleting it.

    Raises:
        ValidationError: If the stream exceeds ``max_size``.
    """
    tmp = tempfile.NamedTemporaryFile(delete=False)
    total = 0
    header = b""
    try:
        while chunk := await file.read(_READ_CHUNK_BYTES):
            if not header:
                header = chunk[:_MAGIC_HEADER_BYTES]
            total += len(chunk)
            if total > max_size:
                raise ValidationError(
                    f"File exceeds the maximum size of {max_size // (1024 * 1024)} MB."
                )
            tmp.write(chunk)
    finally:
        tmp.close()
    return tmp.name, total, header


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_media(
    file: UploadFile = File(...),  # noqa: B008 - FastAPI DI pattern
    settings: Settings = Depends(get_settings),  # noqa: B008 - FastAPI DI pattern
) -> dict:
    # Validate the extension up front (cheap, before reading the body).
    try:
        ext = validate_extension(file.filename)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    file_id = f"{uuid.uuid4().hex}.{ext}"
    temp_path: str | None = None
    try:
        temp_path, total, header = await _stream_to_tempfile(
            file, settings.MAX_UPLOAD_SIZE
        )

        # Content validation: real byte count + magic-byte sniffing.
        validate_size(total)
        detected_mime = validate_magic_bytes(header)

        # Persist via the configured backend.
        with open(temp_path, "rb") as handle:
            stored = get_storage().save(
                file_id, handle, detected_mime or file.content_type
            )
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)

    logger.info(
        "Stored upload file_id=%s size=%d backend=%s",
        file_id, stored.size, stored.backend,
    )
    return {
        "file_id": stored.file_id,
        "url": stored.url,
        "size": stored.size,
        "content_type": detected_mime or file.content_type,
        "storage": stored.backend,
    }
