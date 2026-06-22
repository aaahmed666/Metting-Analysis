"""
Module: Background Tasks
Purpose: Defines Celery tasks for the media pipeline. The audio-extraction task
         converts an uploaded file to 16kHz mono WAV and splits long recordings
         into chunks, returning chunk metadata for downstream parallel AI steps.
"""
from __future__ import annotations

import logging
import os

from config.setting import get_settings
from pipeline.processors.audio_extractor import extract_audio_chunks
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
