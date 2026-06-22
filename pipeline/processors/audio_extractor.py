"""
Module: Processor Step 2 - Audio Extractor
Purpose: Wraps the ffmpeg service to extract and format the audio track
         from the validated media file, preparing it for AI transcription.
"""
from __future__ import annotations

from services.ai_models.ffmpeg_processor import AudioChunk, extract_and_chunk


def extract_audio_chunks(src_path: str, work_dir: str) -> list[AudioChunk]:
    """
    Convert the validated media file to 16kHz mono WAV and split it into
    5-minute chunks when it runs longer than 10 minutes.

    Returns the ordered list of chunks (a single chunk for short files),
    each pointing at a WAV file on disk ready for the AI transcription step.
    """
    return extract_and_chunk(src_path, work_dir)
