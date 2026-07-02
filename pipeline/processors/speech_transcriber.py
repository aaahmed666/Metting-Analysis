"""
Module: Processor Step 3 - Speech Transcriber
Purpose: Wraps the Whisper AI service to generate a full, time-stamped text
         transcript with identified speakers from the extracted audio.

This module is intentionally thin. All transcription + diarization logic lives
in ``services.audio.stt_whisper.WhisperTranscriber`` so it can be tested and
reused independently of the pipeline.
"""

from __future__ import annotations
import logging
from services.ai_models.ffmpeg_processor import AudioChunk
from services.audio.stt_whisper import TranscriptResult, WhisperTranscriber
logger = logging.getLogger(__name__)

# Module-level singleton — the OpenAI client is thread-safe and should be
# created once per process, not once per task invocation.

_transcriber: WhisperTranscriber | None = None

def _get_transcriber() -> WhisperTranscriber:
    global _transcriber
    if _transcriber is None:
        _transcriber = WhisperTranscriber()
    return _transcriber

def transcribe(file_id: str, chunks: list[AudioChunk]) -> TranscriptResult:
    """
    Transcribe and diarize a list of audio chunks for a given file.
    This is the single entry-point called by the pipeline orchestrator and
    the Celery task layer.
    Args:
        file_id: Identifier of the original uploaded media file.
        chunks:  Ordered ``AudioChunk`` list from the audio-extraction step.
    Returns:
        A ``TranscriptResult`` with absolute timestamps and ``rep`` / ``client``
        speaker labels for every segment.
    """
    logger.info("speech_transcriber: starting  file_id=%s  chunks=%d", file_id, len(chunks))
    result = _get_transcriber().transcribe_chunks(file_id=file_id, chunks=chunks)
    logger.info(
        "speech_transcriber: done  file_id=%s  segments=%d",
        file_id, len(result.segments),
    )
    return result
