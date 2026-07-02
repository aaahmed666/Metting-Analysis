"""
Module: Background Tasks

Purpose: Defines Celery tasks for the media pipeline:
  1. ``media.extract_audio``    — converts the uploaded file to 16kHz mono WAV
     and splits long recordings into chunks.
  2. ``media.transcribe``       — transcribes each chunk via the Whisper API and
     assigns ``rep`` / ``client`` speaker labels via GPT-4o diarization.
  3. ``zoom.download_recording`` — downloads a Zoom cloud recording file from
     the URL provided in the ``recording.completed`` webhook, then saves it to
     S3 and returns a manifest ready for the extraction step.
"""
from __future__ import annotations

import logging
import os

from config.setting import get_settings
from pipeline.processors.audio_extractor import extract_audio_chunks
from services.ai_models.ffmpeg_processor import AudioChunk
from workers.celery_app import celery_app

import tempfile
import httpx
from services.storage import get_storage

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
