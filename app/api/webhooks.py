"""
Module: Zoom Webhook Endpoint
Purpose: Receives all Zoom webhook events, validates their authenticity via
         HMAC-SHA256 signature verification, and routes each event to the
         appropriate handler.

Critical implementation detail
-------------------------------
FastAPI's ``Request.body()`` must be awaited to retrieve the raw bytes
**before** any JSON parsing.  The signature is computed over those exact bytes.
Re-serialising the parsed payload would produce a different string and break
the HMAC comparison.
"""

from __future__ import annotations

import json
import logging
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from config.setting import get_settings
from services.integrations.zoom_webhook import (
    ZoomRecordingEvent,
    build_url_validation_response,
    parse_recording_event,
    verify_signature,
)
from workers.tasks import download_recording_task

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhooks"])

# Zoom event type constants — kept here to avoid magic strings in handlers.
_EVENT_URL_VALIDATION = "endpoint.url_validation"
_EVENT_RECORDING_COMPLETED = "recording.completed"
_EVENT_MEETING_ENDED = "meeting.ended"

# MIME type mapping for file extensions we accept.
_MIME_BY_EXT: dict[str, str] = {
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
}

# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/zoom", status_code=status.HTTP_200_OK)
async def zoom_webhook(request: Request) -> JSONResponse:
    """
    Central entry-point for all Zoom webhook events.
    
    Zoom requires a response within **3 seconds** — all heavy work is
    delegated to Celery tasks before we return.  The endpoint guarantees:
    
    * URL validation events get the HMAC challenge response Zoom expects.
    * Every other event is signature-verified before being processed.
    * Unknown event types are acknowledged with ``{"status": "ok"}`` so Zoom
      does not retry them unnecessarily.
    """
    settings = get_settings()
   
    # ── 1. Read raw bytes FIRST (signature is over these exact bytes) ────────
    raw_body: bytes = await request.body()
    timestamp: str = request.headers.get("x-zm-request-timestamp", "")
    received_signature: str = request.headers.get("x-zm-signature", "")
    # ── 2. Parse JSON to inspect the event type ──────────────────────────────
    try:
        payload: dict = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.warning("Zoom webhook: received non-JSON body")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Invalid JSON body"},
        )
    
    event_type: str = payload.get("event", "")
    
    # ── 3. URL validation: skip signature check, return HMAC challenge ───────
    #
    # Zoom sends this event when the webhook is first configured or its URL is
    # changed.  At this moment the secret is already set on our side, so we
    # can compute the challenge without the usual timestamp/body signature.
    if event_type == _EVENT_URL_VALIDATION:
        plain_token: str = payload.get("payload", {}).get("plainToken", "")
        logger.info("Zoom URL validation challenge received")
        response_body = build_url_validation_response(
            plain_token=plain_token,
            secret=settings.ZOOM_WEBHOOK_SECRET_TOKEN.get_secret_value(),
        )
        return JSONResponse(content=response_body)
    
    # ── 4. Signature verification (all other events) ─────────────────────────
    secret = settings.ZOOM_WEBHOOK_SECRET_TOKEN.get_secret_value()
    if not verify_signature(
        secret=secret,
        timestamp=timestamp,
        raw_body=raw_body,
        signature=received_signature,
    ):
        logger.warning(
            "Zoom webhook signature invalid  event=%s  timestamp=%s",
            event_type,
            timestamp,
        )
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"error": "Invalid signature"},
        )
    
    logger.info("Zoom event received  event=%s", event_type)
    
    # ── 5. Event routing ──────────────────────────────────────────────────────
    if event_type == _EVENT_RECORDING_COMPLETED:
        return await _handle_recording_completed(payload)
    
    if event_type == _EVENT_MEETING_ENDED:
        meeting_id = (
            payload.get("payload", {}).get("object", {}).get("id", "unknown")
        )
        logger.info(
            "Meeting ended acknowledged  meeting_id=%s  "
            "(recording not yet available — waiting for recording.completed)",
            meeting_id,
        )
        return JSONResponse(content={"status": "ok", "event": event_type})
    
    # Safe default: acknowledge any other Zoom event without taking action.
    logger.debug("Unhandled Zoom event type acknowledged  event=%s", event_type)
    return JSONResponse(content={"status": "ok", "event": event_type})

#  ---------------------------------------------------------------------------
# Private handlers
# ---------------------------------------------------------------------------

async def _handle_recording_completed(payload: dict) -> JSONResponse:
    """
    Parse the ``recording.completed`` payload and enqueue one Celery download
    task for each downloadable recording file.
    
    Returns immediately after enqueuing — the actual download is asynchronous.
    """
    recording_event: ZoomRecordingEvent = parse_recording_event(payload)
    
    if not recording_event.recording_files:
        logger.info(
            "recording.completed for meeting_uuid=%s had no downloadable files "
            "(all were filtered out by status or file-type)",
            recording_event.meeting_uuid,
        )
        return JSONResponse(content={"status": "ok", "tasks_queued": 0})
    
    tasks_queued = 0
    for rec_file in recording_event.recording_files:
        # Build the file_id we'll use throughout our system.
        file_id = f"{rec_file.id}.{rec_file.file_extension.lower()}"
        content_type = _MIME_BY_EXT.get(rec_file.file_extension.lower(), "application/octet-stream")
        
        task_payload = {
            "file_id": file_id,
            "download_url": rec_file.download_url,
            "download_token": recording_event.download_token,
            "content_type": content_type,
            "meeting_metadata": {
                "uuid": recording_event.meeting_uuid,
                "topic": recording_event.topic,
                "start_time": recording_event.start_time,
                "duration_minutes": recording_event.duration_minutes,
                "recording_type": rec_file.recording_type,
                "file_size_bytes": rec_file.file_size,
            },
        }
        
        download_recording_task.delay(task_payload)
        
        logger.info(
            "Queued download task  file_id=%s  recording_type=%s  size_bytes=%d",
            file_id,
            rec_file.recording_type,
            rec_file.file_size,
        )
        tasks_queued += 1
    
    return JSONResponse(
        content={
            "status": "ok",
            "tasks_queued": tasks_queued,
            "meeting_uuid": recording_event.meeting_uuid,
            "topic": recording_event.topic,
        }
    )
