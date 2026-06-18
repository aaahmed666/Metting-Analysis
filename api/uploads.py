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

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from config.setting import get_settings
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
    """
    Stream an upload to a temp file in fixed-size chunks, enforcing the size
    cap as we read so an oversized file never gets fully buffered.

    Returns (temp_path, total_bytes, header_bytes). The caller owns the temp
    file and is responsible for deleting it.
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
async def upload_media(file: UploadFile = File(...)):  # noqa: B008 - FastAPI DI pattern
    settings = get_settings()

    # Validate the extension up front (cheap, before reading the body).
    try:
        ext = validate_extension(file.filename)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    file_id = f"{uuid.uuid4().hex}.{ext}"
    temp_path = None
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


@router.get("/media/{file_id}")
async def get_media(file_id: str):
    """Serve locally-stored files (S3 uploads return absolute URLs instead)."""
    settings = get_settings()
    if settings.STORAGE_BACKEND != "local":
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not served by this backend.")

    safe_name = os.path.basename(file_id)  # guard against path traversal
    path = os.path.join(settings.LOCAL_STORAGE_DIR, safe_name)
    if not os.path.exists(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="File not found.")
    return FileResponse(path)
