"""
Module: Background Tasks
Purpose: Defines Celery tasks for the media pipeline:
  1. ``media.extract_audio``  — converts the uploaded file to 16kHz mono WAV and
     splits long recordings into chunks.
  2. ``media.transcribe``     — transcribes each chunk via the Whisper API and
     assigns ``rep`` / ``client`` speaker labels via GPT-4o diarization.
"""
from __future__ import annotations

import logging
import os

from config.setting import get_settings
from pipeline.processors.audio_extractor import extract_audio_chunks
# from services.ai_models.ffmpeg_processor import AudioChunk
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

# @celery_app.task(name="media.transcribe", bind=True, max_retries=1)
# def transcribe_audio_task(self, manifest: dict) -> dict:
#     """
#     Transcribe + diarize all audio chunks produced by ``extract_audio_task``.
#     Args:
#         manifest: The dict returned by ``extract_audio_task``, containing:
#                   ``file_id``, ``work_dir``, ``chunk_count``, ``chunks``.
#     Returns:
#         A ``TranscriptResult.to_dict()`` — the full JSON transcript with
#         speaker-labelled segments and absolute timestamps.
#     """
#     # Import here to avoid loading the OpenAI client in every Celery worker
#     # process that might never run this task.
#     from pipeline.processors.speech_transcriber import transcribe
#     file_id: str = manifest["file_id"]
#     logger.info("Starting transcription for file_id=%s", file_id)
#     # Re-hydrate AudioChunk dataclasses from the serialised manifest dict.
#     chunks: list[AudioChunk] = [
#         AudioChunk(
#             index=c["index"],
#             path=c["path"],
#             start_seconds=c["start_seconds"],
#             duration_seconds=c["duration_seconds"],
#         )
#         for c in manifest["chunks"]
#     ]
#     try:
#         result = transcribe(file_id=file_id, chunks=chunks)
#     except Exception as exc:  # noqa: BLE001 - surface as Celery retry/failure
#         logger.warning("Transcription failed for file_id=%s: %s", file_id, exc)
#         raise self.retry(exc=exc, countdown=30) from exc
#     logger.info(
#         "Transcription complete for file_id=%s: %d segment(s)",
#         file_id, len(result.segments),
#     )
#     return result.to_dict()
