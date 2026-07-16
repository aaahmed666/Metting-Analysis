"""
Module: Rep Meeting Upload Endpoint
Route:  POST /api/v1/rep/meetings/upload
        GET  /api/v1/rep/meetings/{meeting_id}

Purpose: Allows an authenticated sales representative to:
  1. Upload a video (mp4, mov, avi, mkv, webm) or audio (mp3, wav, m4a, ogg)
     recording linked to an existing Deal.
  2. Receive an immediate response with meeting_id and status "processing".
  3. Poll the GET endpoint to track analysis progress.

Flow
----
  POST /rep/meetings/upload (multipart/form-data)
    ├── Validate JWT → must be sales_rep or manager role
    ├── Validate file type (extension + magic bytes)
    ├── Validate file size (≤ MAX_UPLOAD_SIZE_MB)
    ├── Verify the deal exists and belongs to the calling rep
    ├── Create Meetings row  (status = "pending")
    ├── Stream file to S3   → StoredFile.url saved to Meetings.file_url
    ├── Mark Meetings as    status = "processing"
    └── Dispatch Celery task  meeting.run_analysis
          ├── Downloads file from S3 to local temp path
          ├── Runs orchestrator.run_pipeline() Steps 2-8
          │     Step 2: ffmpeg extracts audio (handles video → audio)
          │     Step 3: Whisper transcription + diarization
          │     Step 6: Gemini insights
          │     Step 7: 5-pillar scoring
          │     Step 8: saves Meeting_Reports, Transcripts, Signals
          └── Cleans up temp files
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from supabase import Client

from app.core.dependencies import get_current_user, get_supabase_admin_client
from app.models.meeting_models import MeetingStatusResponse, MeetingUploadResponse
from app.repositories.meeting_repository import MeetingRepository, MeetingRepositoryError
from services.storage.base import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rep/meetings", tags=["Rep – Meeting Upload"])

# ---------------------------------------------------------------------------
# File validation constants
# ---------------------------------------------------------------------------

#: Extensions accepted by the endpoint (checked case-insensitively)
ALLOWED_VIDEO_EXTENSIONS = frozenset({
    "mp4", "mov", "avi", "mkv", "webm",
})
ALLOWED_AUDIO_EXTENSIONS = frozenset({
    "mp3", "wav", "m4a", "ogg", "aac", "flac",
})
ALLOWED_EXTENSIONS = ALLOWED_VIDEO_EXTENSIONS | ALLOWED_AUDIO_EXTENSIONS

#: Magic-byte signatures for quick binary validation (offset 0 unless noted)
_MAGIC = {
    b"\x1aE\xdf\xa3":  "mkv/webm",   # Matroska/WebM
    b"ftyp":           "mp4/mov",     # checked at offset 4
    b"RIFF":           "avi/wav",
    b"\x00\x00\x00\x20ftyp": "mp4",
    b"ID3":            "mp3",
    b"OggS":           "ogg",
}

MAX_UPLOAD_MB = 500
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

CONTENT_TYPE_MAP = {
    "mp4":  "video/mp4",
    "mov":  "video/quicktime",
    "avi":  "video/x-msvideo",
    "mkv":  "video/x-matroska",
    "webm": "video/webm",
    "mp3":  "audio/mpeg",
    "wav":  "audio/wav",
    "m4a":  "audio/mp4",
    "ogg":  "audio/ogg",
    "aac":  "audio/aac",
    "flac": "audio/flac",
}


# ---------------------------------------------------------------------------
# Dependency: require sales_rep or manager
# ---------------------------------------------------------------------------

async def _require_sales_rep(current_user: dict = Depends(get_current_user)) -> dict:
    role = current_user.get("role", "")
    if role not in {"sales_rep", "manager", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only sales representatives can upload meeting recordings.",
        )
    return current_user


# ---------------------------------------------------------------------------
# POST /rep/meetings/upload
# ---------------------------------------------------------------------------

@router.post(
    "/upload",
    response_model=MeetingUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a meeting recording for AI analysis",
    description=(
        "Upload a video (mp4, mov, avi, mkv, webm) or audio (mp3, wav, m4a, ogg, aac, flac) "
        "recording. If a video file is provided the audio track is automatically extracted "
        "before transcription. Returns immediately; analysis runs asynchronously."
    ),
)
async def upload_meeting(
    file:         UploadFile     = File(...,  description="Video or audio file (max 500 MB)"),
    deal_id:      str            = Form(...,  description="UUID of the deal this meeting belongs to"),
    meeting_date: Optional[str]  = Form(None, description="ISO-8601 datetime of the meeting (defaults to now)"),
    current_user: dict           = Depends(_require_sales_rep),
    supabase:     Client         = Depends(get_supabase_admin_client),
) -> MeetingUploadResponse:
    """
    Upload a video or audio meeting recording and trigger the analysis pipeline.

    - **file**: The raw media file (multipart upload)
    - **deal_id**: Links the analysis results to an existing Deal
    - **meeting_date**: Optional; defaults to the current UTC timestamp
    """
    user_id  = current_user["user_id"]
    repo     = MeetingRepository(supabase)
    meeting_id: str | None = None      # track for rollback in error path

    # ── Step 1: Extension validation ─────────────────────────────────────
    original_filename = file.filename or "unknown"
    ext = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else ""

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type '.{ext}'. "
                f"Accepted: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            ),
        )

    # ── Step 2: File size guard ──────────────────────────────────────────────
    file.file.seek(0, 2)
    file_size = file.file.tell()
    file.file.seek(0)

    if file_size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {MAX_UPLOAD_MB} MB limit.",
        )
    if file_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    # ── Step 3: Magic-byte validation ────────────────────────────────────
    header = await file.read(12)
    _validate_magic_bytes(header, ext)
    await file.seek(0)

    # ── Step 4: Verify the deal exists and belongs to the rep ────────────
    try:
        deal = repo.get_deal_by_id(deal_id=deal_id, user_id=user_id)
    except MeetingRepositoryError as exc:
        logger.error("upload_meeting: deal lookup failed  deal_id=%s  error=%s", deal_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to verify deal ownership.",
        ) from exc

    if not deal:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deal '{deal_id}' not found or you do not have access to it.",
        )

    # ── Step 5: Create the Meetings row (status = pending) ────────────────
    try:
        meeting_row = repo.create_meeting(
            deal_id=deal_id,
            user_id=user_id,
            source="upload",
            meeting_date=meeting_date,
        )
        meeting_id = meeting_row["id"]
    except MeetingRepositoryError as exc:
        logger.error(
            "upload_meeting: failed to create meeting  deal_id=%s  error=%s", deal_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create meeting record.",
        ) from exc

    # ── Step 6: Upload media to S3 ────────────────────────────────────────
    file_id   = f"{meeting_id}.{ext}"
    s3_key    = f"uploads/{file_id}"
    content_type = CONTENT_TYPE_MAP.get(ext)

    try:
        import os
        import tempfile
        import shutil
        from starlette.concurrency import run_in_threadpool
        
        storage   = get_storage()
        
        def _upload_s3():
            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp:
                shutil.copyfileobj(file.file, tmp)
                tmp_path = tmp.name
            try:
                return storage.save_file(
                    file_id=file_id,
                    filepath=tmp_path,
                    content_type=content_type,
                )
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                    
        stored    = await run_in_threadpool(_upload_s3)
        file_url  = stored.url
    except Exception as exc:
        logger.error(
            "upload_meeting: S3 upload failed  meeting_id=%s  error=%s", meeting_id, exc
        )
        # Mark the pending meeting as rejected so it doesn't stay dangling
        _safe_reject(repo, meeting_id, "File upload to storage failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to store the uploaded file. Please try again.",
        ) from exc

    # ── Step 7: Persist file_url + mark processing ───────────────────────
    try:
        repo.update_file_url(meeting_id, file_url)
        supabase.table("Meetings").update({"status": "processing"}).eq("id", meeting_id).execute()
    except Exception as exc:
        logger.error(
            "upload_meeting: failed to update meeting record  meeting_id=%s  error=%s",
            meeting_id, exc,
        )
        # Non-fatal for the response — pipeline will still run

    # ── Step 8: Dispatch the async analysis pipeline ─────────────────────
    from workers.tasks import run_analysis_pipeline_task
    run_analysis_pipeline_task.delay({
        "meeting_id": meeting_id,
        "s3_key":     s3_key,
        "file_ext":   ext,
    })

    logger.info(
        "upload_meeting: pipeline dispatched  meeting_id=%s  deal_id=%s  "
        "file=%s  size=%d bytes",
        meeting_id, deal_id, original_filename, file_size,
    )

    return MeetingUploadResponse(
        success=True,
        meeting_id=meeting_id,
        deal_id=deal_id,
        status="processing",
        file_url=file_url,
        message=(
            "Your recording has been uploaded and is now being analysed. "
            "Use the meeting_id to poll for results."
        ),
    )


# ---------------------------------------------------------------------------
# GET /rep/meetings
# ---------------------------------------------------------------------------

@router.get(
    "",
    summary="List representative's meetings",
    description="Retrieve a list of all meetings owned by the authenticated representative, including summary scores.",
)
async def list_meetings(
    current_user: dict = Depends(_require_sales_rep),
    supabase: Client = Depends(get_supabase_admin_client),
):
    user_id = current_user["user_id"]
    repo = MeetingRepository(supabase)
    try:
        meetings = repo.list_user_meetings(user_id)
        return {
            "success": True,
            "total": len(meetings),
            "meetings": meetings,
        }
    except Exception as exc:
        logger.error("list_meetings: failed for user=%s: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve meetings list.",
        )


# ---------------------------------------------------------------------------
# GET /rep/meetings/team-comparison
# ---------------------------------------------------------------------------

@router.get(
    "/team-comparison",
    summary="Compare user scores to team averages",
    description="Compare the representative's overall and pillar scores to their team's averages.",
)
async def get_team_comparison(
    current_user: dict = Depends(_require_sales_rep),
    supabase: Client = Depends(get_supabase_admin_client),
):
    user_id = current_user["user_id"]
    repo = MeetingRepository(supabase)
    try:
        stats = repo.get_team_comparison_stats(user_id)
        return {
            "success": True,
            **stats,
        }
    except Exception as exc:
        logger.error("get_team_comparison: failed for user=%s: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve team comparison statistics.",
        )


# ---------------------------------------------------------------------------
# GET /rep/meetings/{meeting_id}  — status polling
# ---------------------------------------------------------------------------

@router.get(
    "/{meeting_id}",
    response_model=MeetingStatusResponse,
    summary="Get meeting analysis status",
    description="Poll the analysis status and final results for an uploaded meeting.",
)
async def get_meeting_status(
    meeting_id:   str,
    current_user: dict    = Depends(_require_sales_rep),
    supabase:     Client  = Depends(get_supabase_admin_client),
) -> MeetingStatusResponse:
    """
    Returns the current processing status of a meeting.

    Possible ``status`` values:
    - **pending** — uploaded but not yet picked up by the worker
    - **processing** — pipeline is running
    - **completed** — analysis saved; Meeting_Reports row is available
    - **rejected** — pipeline failed; see ``rejection_reason``
    """
    user_id = current_user["user_id"]
    repo    = MeetingRepository(supabase)

    try:
        meeting = repo.get_meeting_by_id(meeting_id=meeting_id, user_id=user_id)
    except MeetingRepositoryError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch meeting.",
        ) from exc

    if not meeting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Meeting '{meeting_id}' not found or you do not have access.",
        )

    return MeetingStatusResponse(
        meeting_id       = meeting["id"],
        deal_id          = meeting["deal_id"],
        status           = meeting["status"],
        file_url         = meeting.get("file_url"),
        meeting_date     = meeting.get("meeting_date"),
        duration_seconds = meeting.get("duration_seconds"),
        rejection_reason = meeting.get("rejection_reason"),
    )


# ---------------------------------------------------------------------------
# GET /rep/meetings/{meeting_id}/report
# ---------------------------------------------------------------------------

@router.get(
    "/{meeting_id}/report",
    summary="Get detailed meeting report",
    description="Retrieve the full AI-generated report and pillar scores for a specific meeting.",
)
async def get_report(
    meeting_id: str,
    current_user: dict = Depends(_require_sales_rep),
    supabase: Client = Depends(get_supabase_admin_client),
):
    user_id = current_user["user_id"]
    repo = MeetingRepository(supabase)
    try:
        report = repo.get_meeting_report(meeting_id, user_id)
    except Exception as exc:
        logger.error("get_report: lookup failed meeting_id=%s user=%s: %s", meeting_id, user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve meeting report.",
        )

    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found or you do not have access.",
        )

    return {
        "success": True,
        "report": report,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------



def _validate_magic_bytes(header: bytes, ext: str) -> None:
    """
    Lightweight binary-format check to prevent extension spoofing.
    Raises HTTP 415 if the file header does not match the declared extension.

    Only enforces the check when we have a known signature for the extension;
    unknown formats pass through without error.
    """
    is_video = ext in ALLOWED_VIDEO_EXTENSIONS

    # mp4 / mov: "ftyp" atom at byte offset 4
    if ext in {"mp4", "mov", "m4a"}:
        if len(header) >= 8 and header[4:8] not in {b"ftyp", b"moov", b"mdat"}:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"File content does not match '.{ext}' format.",
            )
        return

    # mkv / webm: EBML magic
    if ext in {"mkv", "webm"}:
        if not header.startswith(b"\x1aE\xdf\xa3"):
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"File content does not match '.{ext}' format.",
            )
        return

    # wav / avi: RIFF header
    if ext in {"wav", "avi"}:
        if not header.startswith(b"RIFF"):
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"File content does not match '.{ext}' format.",
            )
        return

    # mp3: ID3 tag
    if ext == "mp3":
        if not header.startswith(b"ID3") and not header[:2] in {b"\xff\xfb", b"\xff\xfa"}:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="File content does not match '.mp3' format.",
            )
        return

    # ogg
    if ext == "ogg":
        if not header.startswith(b"OggS"):
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="File content does not match '.ogg' format.",
            )
        return
    # flac, aac — no strict check needed (pass through)


def _safe_reject(repo: MeetingRepository, meeting_id: str, reason: str) -> None:
    """Best-effort: mark the meeting as rejected. Never raises."""
    try:
        from app.repositories.ai_analysis_repository import AIAnalysisRepository
        from app.core.dependencies import get_supabase_admin_client
        ai_repo = AIAnalysisRepository(get_supabase_admin_client())
        ai_repo.update_meeting_status(meeting_id, status="rejected", rejection_reason=reason)
    except Exception as exc:
        logger.error(
            "_safe_reject: could not mark meeting rejected  meeting_id=%s  error=%s",
            meeting_id, exc,
        )
