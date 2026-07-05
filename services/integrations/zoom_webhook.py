"""
Module: Zoom Webhook Service
Purpose: Domain logic for processing Zoom webhook events. Handles signature
         verification, URL validation challenge responses, and recording payload
         parsing — all independent of the HTTP transport layer so each function
         can be unit-tested without starting a server.

Design notes
------------
* Signature verification operates on the **raw request bytes**, never on
  re-serialised JSON.  Any whitespace difference in the body string will break
  the HMAC, so we accept ``bytes`` and decode them ourselves.
* ``parse_recording_event`` silently skips files that are not yet ``completed``
  or whose type is not in our processable set — the caller never sees them.
* Recording files are sorted by ``_RECORDING_TYPE_PRIORITY`` so the best view
  (shared_screen_with_speaker_view) always comes first in the queue.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# File types we can feed into the audio-extraction pipeline.
_DOWNLOADABLE_FILE_TYPES: frozenset[str] = frozenset({"MP4", "M4A", "M4V", "MP3"})

# Lower priority number = better.  Unknown types receive 99 (lowest priority).
_RECORDING_TYPE_PRIORITY: dict[str, int] = {
    "shared_screen_with_speaker_view": 0,
    "speaker_view": 1,
    "gallery_view": 2,
    "shared_screen": 3,
    "audio_only": 4,
}

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecordingFile:
    """A single downloadable recording file from a Zoom cloud recording."""
    id: str
    file_type: str           # e.g. "MP4", "M4A"
    file_extension: str      # may differ from file_type in edge cases
    file_size: int           # bytes
    download_url: str
    recording_type: str      # e.g. "shared_screen_with_speaker_view"
    priority: int            # derived; lower = more preferred

@dataclass(frozen=True)
class ZoomRecordingEvent:
    """
    Typed representation of a validated ``recording.completed`` webhook payload.
    
    Only files that passed the status + file-type filters are present in
    ``recording_files``; they are sorted best-first by recording type.
    """
    meeting_uuid: str
    topic: str
    start_time: str           # ISO-8601 string as provided by Zoom
    duration_minutes: int
    download_token: str       # Bearer token valid for 24 hours from delivery
    recording_files: list[RecordingFile] = field(default_factory=list)

# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def verify_signature(
    secret: str,
    timestamp: str,
    raw_body: bytes,
    signature: str,
) -> bool:
    """
    Verify a Zoom webhook delivery using HMAC-SHA256.

    Zoom constructs the signed message as::

        f"v0:{x-zm-request-timestamp}:{raw_body_string}"
    
    and signs it with the Webhook Secret Token.
    
    Args:
        secret:    The Zoom Webhook Secret Token (from Marketplace).
        timestamp: Value of the ``x-zm-request-timestamp`` header.
        raw_body:  The exact bytes Zoom sent — NOT re-serialised JSON.
        signature: Value of the ``x-zm-signature`` header to compare against.
    
    Returns:
        ``True`` when the computed digest matches the provided signature.
        Uses ``hmac.compare_digest`` to prevent timing attacks.
    """
    if not timestamp or not signature:
        logger.debug("verify_signature: missing timestamp or signature header")
        return False
    
    message = f"v0:{timestamp}:{raw_body.decode('utf-8')}"
    computed_hash = hmac.new(
        key=secret.encode("utf-8"),
        msg=message.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    expected_signature = f"v0={computed_hash}"
    
    return hmac.compare_digest(expected_signature, signature)

# ---------------------------------------------------------------------------
# URL validation challenge
# ---------------------------------------------------------------------------

def build_url_validation_response(plain_token: str, secret: str) -> dict:
    """
    Build the response body for Zoom's endpoint URL validation handshake.
    
    Zoom sends a ``plainToken`` when you first configure (or update) a webhook
    endpoint.  The expected response is the token alongside its HMAC-SHA256
    hash, using the Secret Token as the key.
    
    Args:
        plain_token: The ``plainToken`` value from the validation payload.
        secret:      The Zoom Webhook Secret Token.
    
    Returns:
        ``{"plainToken": "...", "encryptedToken": "..."}``
    """
    encrypted = hmac.new(
        key=secret.encode("utf-8"),
        msg=plain_token.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    logger.debug("URL validation challenge responded for token prefix=%s", plain_token[:6])
    return {"plainToken": plain_token, "encryptedToken": encrypted}

# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

def parse_recording_event(payload: dict) -> ZoomRecordingEvent:
    """
    Parse a ``recording.completed`` webhook payload into a typed domain object.
    
    Filtering rules (applied before returning):
    - Only files with ``status == "completed"`` are included.
    - Only file types in ``{MP4, M4A, M4V, MP3}`` are included.
    - Files are sorted by recording-type priority (best view first).
    
    Args:
        payload: The raw JSON payload dict from the Zoom webhook.
    
    Returns:
        A ``ZoomRecordingEvent`` instance.  ``recording_files`` may be empty
        if no files survived the filters.
    """
    event_payload = payload.get("payload", {})
    obj = event_payload.get("object", {})
    download_token = event_payload.get("download_token", "")
    
    recording_files: list[RecordingFile] = []
    
    for raw_file in obj.get("recording_files", []):
        file_id = raw_file.get("id", "")
        file_type = raw_file.get("file_type", "").upper()
        file_status = raw_file.get("status", "")
        
        if file_status != "completed":
            logger.debug(
                "Skipping recording file  id=%s  reason=status(%s)", file_id, file_status
            )
            continue
        
        if file_type not in _DOWNLOADABLE_FILE_TYPES:
            logger.debug(
                "Skipping recording file  id=%s  reason=unsupported_type(%s)",
                file_id, file_type,
            )
            continue
        
        recording_type = raw_file.get("recording_type", "unknown")
        priority = _RECORDING_TYPE_PRIORITY.get(recording_type, 99)
        
        recording_files.append(
            RecordingFile(
                id=file_id,
                file_type=file_type,
                file_extension=raw_file.get("file_extension", file_type),
                file_size=int(raw_file.get("file_size", 0)),
                download_url=raw_file.get("download_url", ""),
                recording_type=recording_type,
                priority=priority,
            )
        )
        
    # Best recording type first so the most useful file is processed first.
    recording_files.sort(key=lambda f: f.priority)
    
    logger.info(
        "Parsed recording.completed  meeting_uuid=%s  topic=%r  "
        "total_files=%d  downloadable=%d",
        obj.get("uuid", ""),
        obj.get("topic", ""),
        len(obj.get("recording_files", [])),
        len(recording_files),
    )
    
    return ZoomRecordingEvent(
        meeting_uuid=obj.get("uuid", ""),
        topic=obj.get("topic", "Untitled Meeting"),
        start_time=obj.get("start_time", ""),
        duration_minutes=int(obj.get("duration", 0)),
        download_token=download_token,
        recording_files=recording_files,
    )
