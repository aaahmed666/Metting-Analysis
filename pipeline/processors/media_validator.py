"""
Module: Processor Step 1 - Media Validator
Purpose: Validates an uploaded file before processing: extension allow-listing,
         maximum size enforcement, and content-type verification via magic bytes.
"""
from __future__ import annotations

import logging

from config.setting import get_settings

logger = logging.getLogger(__name__)

try:
    import magic  # python-magic; requires libmagic
    _HAS_MAGIC = True
except ImportError:  # pragma: no cover - libmagic may be absent
    _HAS_MAGIC = False


class ValidationError(Exception):
    """Raised when an uploaded file fails validation."""


def get_extension(filename: str | None) -> str:
    """Return the lowercased extension of ``filename`` without the dot."""
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[1].lower()


def validate_extension(filename: str | None) -> str:
    """Validate the file extension against the configured allow-list.

    Returns:
        The validated, lowercased extension.

    Raises:
        ValidationError: If the extension is missing or not allowed.
    """
    settings = get_settings()
    ext = get_extension(filename)
    if ext not in settings.ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(settings.ALLOWED_EXTENSIONS))
        raise ValidationError(
            f"Unsupported file extension '.{ext or '?'}'. Allowed: {allowed}"
        )
    return ext


def validate_size(size: int) -> None:
    """Validate the real byte count of an upload.

    Raises:
        ValidationError: If the file is empty or exceeds the configured limit.
    """
    settings = get_settings()
    if size <= 0:
        raise ValidationError("Empty file.")
    if size > settings.MAX_UPLOAD_SIZE:
        limit_mb = settings.MAX_UPLOAD_SIZE // (1024 * 1024)
        raise ValidationError(f"File exceeds the maximum size of {limit_mb} MB.")


def validate_magic_bytes(head: bytes) -> str | None:
    """Inspect leading bytes to confirm the declared type is really audio/video.

    Returns:
        The detected MIME type, or ``None`` when sniffing is skipped because
        libmagic is unavailable and ``REQUIRE_MAGIC`` is disabled.

    Raises:
        ValidationError: If the content is not an allowed audio/video type, or
            if libmagic is unavailable while ``REQUIRE_MAGIC`` is enabled.
    """
    settings = get_settings()
    if not _HAS_MAGIC:
        if settings.REQUIRE_MAGIC:
            raise ValidationError(
                "Content-type verification is required but libmagic is not "
                "installed on this host."
            )
        logger.warning(
            "libmagic unavailable; skipping content-type verification. "
            "Set REQUIRE_MAGIC=true to enforce it in production."
        )
        return None
    if not head:
        raise ValidationError("Could not read file header for content verification.")

    detected = magic.from_buffer(head, mime=True)
    if not detected.startswith(settings.ALLOWED_MIME_PREFIXES):
        raise ValidationError(
            f"File content ('{detected}') is not a recognized audio/video format."
        )
    return detected
