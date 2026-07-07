"""
Module: Background Tasks

Purpose: Defines Celery tasks for the media pipeline:
  1. ``media.extract_audio``       — converts the uploaded file to 16kHz mono WAV
     and splits long recordings into chunks.
  2. ``media.transcribe``          — transcribes each chunk via the Whisper API and
     assigns ``rep`` / ``client`` speaker labels via GPT-4o diarization.
  3. ``zoom.download_recording``   — downloads a Zoom cloud recording file from
     the URL provided in the ``recording.completed`` webhook, then saves it to
     S3 and returns a manifest ready for the extraction step.
  4. ``meeting.run_analysis``      — end-to-end task for rep-uploaded files:
     downloads the file from S3, runs the full 8-step pipeline
     (audio extract → transcribe → insights → score → save), then cleans up.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile

import httpx

from config.setting import get_settings
from pipeline.processors.audio_extractor import extract_audio_chunks
from services.ai_models.ffmpeg_processor import AudioChunk
from services.storage import get_storage
from workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="media.extract_audio", bind=True, max_retries=2)
def extract_audio_task(self, file_id: str, src_path: str) -> dict:
    """
    Convert + chunk a single uploaded media file.

    Returns a manifest: the WAV chunk paths (ready for transcription) plus
    timing metadata, so downstream tasks can fan out per chunk.
    """
    settings = get_settings()
    work_dir = os.path.join(settings.PROCESSING_DIR, file_id)
    logger.info("Starting audio extraction for file_id=%s", file_id)

    try:
        chunks = extract_audio_chunks(src_path, work_dir)
    except Exception as exc:  # noqa: BLE001 - surface as a Celery retry/failure
        logger.warning("Extraction failed for file_id=%s: %s", file_id, exc)
        raise self.retry(exc=exc, countdown=10) from exc

    logger.info("Extraction complete for file_id=%s: %d chunk(s)", file_id, len(chunks))
    return {
        "file_id": file_id,
        "work_dir": work_dir,
        "chunk_count": len(chunks),
        "chunks": [chunk.to_dict() for chunk in chunks],
    }

@celery_app.task(name="media.transcribe", bind=True, max_retries=1)
def transcribe_audio_task(self, manifest: dict) -> dict:
    """
    Transcribe + diarize all audio chunks produced by ``extract_audio_task``.
    Args:
        manifest: The dict returned by ``extract_audio_task``, containing:
                  ``file_id``, ``work_dir``, ``chunk_count``, ``chunks``.
    Returns:
        A ``TranscriptResult.to_dict()`` — the full JSON transcript with
        speaker-labelled segments and absolute timestamps.
    """
    # Import here to avoid loading the OpenAI client in every Celery worker
    # process that might never run this task.
    from pipeline.processors.speech_transcriber import transcribe
    file_id: str = manifest["file_id"]
    logger.info("Starting transcription for file_id=%s", file_id)
    # Re-hydrate AudioChunk dataclasses from the serialised manifest dict.
    chunks: list[AudioChunk] = [
        AudioChunk(
            index=c["index"],
            path=c["path"],
            start_seconds=c["start_seconds"],
            duration_seconds=c["duration_seconds"],
        )
        for c in manifest["chunks"]
    ]
    try:
        result = transcribe(file_id=file_id, chunks=chunks)
    except Exception as exc:  # noqa: BLE001 - surface as Celery retry/failure
        logger.warning("Transcription failed for file_id=%s: %s", file_id, exc)
        raise self.retry(exc=exc, countdown=30) from exc
    logger.info(
        "Transcription complete for file_id=%s: %d segment(s)",
        file_id, len(result.segments),
    )
    return result.to_dict()


@celery_app.task(name="zoom.download_recording", bind=True, max_retries=3)
def download_recording_task(self, recording_info: dict) -> dict:
    """
    Download a Zoom cloud recording and persist it to S3 storage.
    
    This task is enqueued automatically when our ``/webhook/zoom`` endpoint
    receives a valid ``recording.completed`` event from Zoom.
    
    Args:
        recording_info: A dict containing:
            ``file_id``         — unique identifier for this file in our system.
            ``download_url``    — Zoom’s authenticated download URL.
            ``download_token``  — Bearer token (valid 24 h from webhook delivery).
            ``content_type``    — MIME type for S3 storage metadata.
            ``meeting_metadata`` — contextual dict (uuid, topic, start_time …).
    
    Returns:
        A manifest dict.  ``pipeline_ready`` is ``False`` until the downstream
        pipeline steps are fully operational; flip it to ``True`` and add a
        chain to ``extract_audio_task`` when ready.
    """

    file_id: str = recording_info["file_id"]
    download_url: str = recording_info["download_url"]
    download_token: str = recording_info["download_token"]
    content_type: str = recording_info.get("content_type", "application/octet-stream")
    meeting_metadata: dict = recording_info.get("meeting_metadata", {})
    
    logger.info(
        "Starting recording download  file_id=%s  topic=%r  size_bytes=%s",
        file_id,
        meeting_metadata.get("topic", "unknown"),
        meeting_metadata.get("file_size_bytes", "unknown"),
    )
    
    ext = file_id.rsplit(".", 1)[-1] if "." in file_id else "mp4"
    tmp_path: str | None = None
    
    try:
        # Stream the download to a temp file — never fully buffer in memory.
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp:
            tmp_path = tmp.name
            with httpx.Client(follow_redirects=True, timeout=300.0) as client:
                with client.stream(
                    "GET",
                    download_url,
                    headers={"Authorization": f"Bearer {download_token}"},
                ) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(chunk_size=8 * 1024 * 1024):
                        tmp.write(chunk)
        
        logger.info(
            "Download complete  file_id=%s  saved_to=%s", file_id, tmp_path
        )
        
        # Persist to S3 via the existing storage abstraction.
        with open(tmp_path, "rb") as handle:
            stored = get_storage().save(file_id, handle, content_type)
        
        logger.info(
            "Stored to S3  file_id=%s  backend=%s  size=%d  url=%s",
            file_id, stored.backend, stored.size, stored.url,
        )
    
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Download HTTP error  file_id=%s  status=%d  url=%s",
            file_id, exc.response.status_code, download_url,
        )
        raise self.retry(exc=exc, countdown=60) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("Download failed  file_id=%s  error=%s", file_id, exc)
        raise self.retry(exc=exc, countdown=60) from exc
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
            
    return {
        "file_id": stored.file_id,
        "url": stored.url,
        "size": stored.size,
        "meeting_metadata": meeting_metadata,
        # ----------------------------------------------------------------
        # Pipeline chaining is disabled while downstream steps are under
        # maintenance.  When ready, remove this flag and add:
        #   extract_audio_task.delay(stored.file_id, local_path)
        # ----------------------------------------------------------------
        "pipeline_ready": False,
    }


# ---------------------------------------------------------------------------
# Task 4: End-to-end analysis pipeline for rep-uploaded files
# ---------------------------------------------------------------------------

@celery_app.task(name="meeting.run_analysis", bind=True, max_retries=1)
def run_analysis_pipeline_task(self, payload: dict) -> dict:
    """
    End-to-end meeting analysis task for files uploaded by sales reps.

    Flow
    ----
    1. Download the media file from S3 into a local temp file.
    2. Run the full 8-step ``orchestrator.run_pipeline()`` which:
         Step 2 — extracts audio (ffmpeg strips video stream if present)
         Step 3 — transcribes + diarizes (Whisper / GPT-4o)
         Steps 4/5 — acoustic / context stubs
         Step 6 — AI insights (Gemini)
         Step 7 — 5-pillar scoring
         Step 8 — saves Meeting_Reports, Transcripts, Signals to DB
    3. Remove the temp file and work directory.

    Args:
        payload: dict with keys:
            ``meeting_id``  — UUID of the Meetings row
            ``s3_key``      — S3 object key  (e.g. "uploads/<file_id>")
            ``file_ext``    — lowercase extension used for the temp file suffix

    Returns:
        The persistence summary from the orchestrator:
        {meeting_id, report_id, transcript_count, signal_count}
    """
    settings = get_settings()
    meeting_id: str = payload["meeting_id"]
    s3_key:     str = payload["s3_key"]
    file_ext:   str = payload.get("file_ext", "mp4")

    logger.info(
        "run_analysis_pipeline_task: starting  meeting_id=%s  s3_key=%s",
        meeting_id,
        s3_key,
    )

    tmp_path: str | None  = None
    work_dir: str | None  = None

    try:
        # ── 1. Download from S3 to a local temp file ──────────────────────
        import boto3

        s3_client = boto3.client(
            "s3",
            region_name=settings.S3_REGION,
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=(
                settings.AWS_ACCESS_KEY_ID.get_secret_value()
                if settings.AWS_ACCESS_KEY_ID else None
            ),
            aws_secret_access_key=(
                settings.AWS_SECRET_ACCESS_KEY.get_secret_value()
                if settings.AWS_SECRET_ACCESS_KEY else None
            ),
        )

        with tempfile.NamedTemporaryFile(
            delete=False, suffix=f".{file_ext}"
        ) as tmp:
            tmp_path = tmp.name
            s3_client.download_fileobj(settings.S3_BUCKET, s3_key, tmp)

        logger.info(
            "run_analysis_pipeline_task: downloaded from S3  meeting_id=%s  "
            "local=%s  size=%d bytes",
            meeting_id,
            tmp_path,
            os.path.getsize(tmp_path),
        )

        # ── 2. Build working directory for audio chunks ────────────────────
        work_dir = os.path.join(settings.PROCESSING_DIR, meeting_id)
        os.makedirs(work_dir, exist_ok=True)

        # ── 3. Run the full pipeline ───────────────────────────────────────
        from supabase import create_client
        supabase = create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_KEY,
        )

        from pipeline.orchestrator import run_pipeline
        result = run_pipeline(
            meeting_id=meeting_id,
            file_path=tmp_path,
            supabase=supabase,
            work_dir=work_dir,
        )

        logger.info(
            "run_analysis_pipeline_task: complete  meeting_id=%s  "
            "report_id=%s  transcripts=%d  signals=%d",
            meeting_id,
            result.get("report_id"),
            result.get("transcript_count", 0),
            result.get("signal_count", 0),
        )

        try:
            meeting_resp = supabase.table("Meetings").select("user_id").eq("id", meeting_id).maybe_single().execute()
            if meeting_resp and meeting_resp.data:
                uid = meeting_resp.data.get("user_id")
                user_resp = supabase.table("Users").select("email, full_name").eq("id", uid).maybe_single().execute()
                if user_resp and user_resp.data:
                    email_to = user_resp.data.get("email")
                    rep_name = user_resp.data.get("full_name") or "Sales Rep"

                    report_data = None
                    report_id = result.get("report_id")
                    if report_id:
                        rep_data_resp = supabase.table("Meeting_Reports").select("total_score, grade, executive_summary").eq("id", report_id).maybe_single().execute()
                        report_data = rep_data_resp.data if rep_data_resp else None

                    from services.integrations.email_notifier import send_meeting_analysis_email
                    send_meeting_analysis_email(
                        email_to=email_to,
                        rep_name=rep_name,
                        meeting_id=meeting_id,
                        status="completed",
                        report_data=report_data,
                    )
        except Exception as mail_exc:
            logger.error("Failed to send success email for meeting_id=%s: %s", meeting_id, mail_exc)

        return result

    except Exception as exc:
        logger.warning(
            "run_analysis_pipeline_task: failed  meeting_id=%s  error=%s",
            meeting_id,
            exc,
        )
        if self.request.retries >= self.max_retries:
            try:
                from supabase import create_client
                supabase = create_client(
                    settings.SUPABASE_URL,
                    settings.SUPABASE_SERVICE_KEY,
                )
                meeting_resp = supabase.table("Meetings").select("user_id").eq("id", meeting_id).maybe_single().execute()
                if meeting_resp and meeting_resp.data:
                    uid = meeting_resp.data.get("user_id")
                    user_resp = supabase.table("Users").select("email, full_name").eq("id", uid).maybe_single().execute()
                    if user_resp and user_resp.data:
                        email_to = user_resp.data.get("email")
                        rep_name = user_resp.data.get("full_name") or "Sales Rep"
                        from services.integrations.email_notifier import send_meeting_analysis_email
                        send_meeting_analysis_email(
                            email_to=email_to,
                            rep_name=rep_name,
                            meeting_id=meeting_id,
                            status="failed",
                            rejection_reason=str(exc),
                        )
            except Exception as mail_exc:
                logger.error("Failed to send failure email for meeting_id=%s: %s", meeting_id, mail_exc)

        raise self.retry(exc=exc, countdown=30) from exc

    finally:
        # ── 4. Clean up temp files regardless of success/failure ──────────
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
            logger.debug(
                "run_analysis_pipeline_task: removed temp file  path=%s", tmp_path
            )
        if work_dir and os.path.isdir(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
            logger.debug(
                "run_analysis_pipeline_task: removed work dir  path=%s", work_dir
            )
