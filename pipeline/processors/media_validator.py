"""
Module: Processor Step 1 - Media Validator
Purpose: Validates the uploaded file before processing. Checks for correct
         extensions, maximum file size limits, and verifies file integrity (magic bytes).
"""
from __future__ import annotations

from config.setting import get_settings

try:
    import magic  # python-magic; requires libmagic
    _HAS_MAGIC = True
except Exception:  # pragma: no cover - libmagic may be absent
    _HAS_MAGIC = False


class ValidationError(Exception):
    """Raised when an uploaded file fails validation."""


def get_extension(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[1].lower()


def validate_extension(filename: str | None) -> str:
    settings = get_settings()
    ext = get_extension(filename)
    if ext not in settings.ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(settings.ALLOWED_EXTENSIONS))
        raise ValidationError(
            f"Unsupported file extension '.{ext or '?'}'. Allowed: {allowed}"
        )
    return ext


def validate_size(size: int) -> None:
    settings = get_settings()
    if size <= 0:
        raise ValidationError("Empty file.")
    if size > settings.MAX_UPLOAD_SIZE:
        limit_mb = settings.MAX_UPLOAD_SIZE // (1024 * 1024)
        raise ValidationError(f"File exceeds the maximum size of {limit_mb} MB.")


def validate_magic_bytes(head: bytes) -> str | None:
    """
    Inspect the leading bytes to confirm the declared type is really
    audio/video. Returns the detected MIME type, or None if libmagic is
    unavailable (in which case this check is skipped).
    """
    if not _HAS_MAGIC or not head:
        return None
    settings = get_settings()
    detected = magic.from_buffer(head, mime=True)
    if not detected.startswith(settings.ALLOWED_MIME_PREFIXES):
        raise ValidationError(
            f"File content ('{detected}') is not a recognized audio/video format."
        )
    return detected
