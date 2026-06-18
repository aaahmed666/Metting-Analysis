"""
Module: FFmpeg Processor
Purpose: Handles media file manipulation using FFmpeg. Responsible for extracting
         audio, converting it to 16kHz Mono format (optimal for Whisper), and
         splitting large files into smaller chunks for parallel processing.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)

# Whisper-optimal audio format.
TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1

# Chunking policy: only split files longer than this, into chunks of this size.
CHUNK_THRESHOLD_SECONDS = 10 * 60  # 10 minutes
CHUNK_LENGTH_SECONDS = 5 * 60      # 5 minutes


class FFmpegError(Exception):
    """Raised when an ffmpeg/ffprobe invocation fails or is unavailable."""


@dataclass(frozen=True)
class AudioChunk:
    """A single segment of normalized audio ready for transcription."""

    index: int
    path: str
    start_seconds: float
    duration_seconds: float

    def to_dict(self) -> dict:
        return asdict(self)


def _run(cmd: list[str]) -> str:
    """Run an ffmpeg/ffprobe command, raising FFmpegError on any failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise FFmpegError(
            f"'{cmd[0]}' is not installed or not on PATH."
        ) from exc
    if proc.returncode != 0:
        raise FFmpegError(
            f"{cmd[0]} failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}"
        )
    return proc.stdout


def probe_duration(path: str) -> float:
    """Return media duration in seconds using ffprobe."""
    out = _run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", path,
    ])
    try:
        return float(json.loads(out)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise FFmpegError(f"Could not read duration for {path}: {exc}") from exc


def extract_wav(src_path: str, dest_path: str) -> str:
    """
    Convert any audio/video input to a single WAV file at 16kHz mono, 16-bit PCM.
    Strips the video stream if present. Returns dest_path.
    """
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    logger.info("Extracting audio: %s -> %s", src_path, dest_path)
    _run([
        "ffmpeg", "-y",
        "-i", src_path,
        "-vn",                            # drop video stream
        "-ac", str(TARGET_CHANNELS),      # mono
        "-ar", str(TARGET_SAMPLE_RATE),   # 16kHz
        "-c:a", "pcm_s16le",              # 16-bit PCM WAV
        "-f", "wav",
        dest_path,
    ])
    if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
        raise FFmpegError(f"WAV extraction produced no output: {dest_path}")
    return dest_path


def split_wav(wav_path: str, out_dir: str) -> list[AudioChunk]:
    """
    Split a 16kHz mono WAV into fixed-length chunks using stream copy
    (fast, no re-encode). Files at or under the threshold are returned as a
    single chunk unchanged.
    """
    os.makedirs(out_dir, exist_ok=True)
    duration = probe_duration(wav_path)

    if duration <= CHUNK_THRESHOLD_SECONDS:
        logger.info("Audio is %.1fs (<= threshold); no split needed.", duration)
        return [AudioChunk(0, wav_path, 0.0, duration)]

    base = os.path.splitext(os.path.basename(wav_path))[0]
    pattern = os.path.join(out_dir, f"{base}_chunk_%03d.wav")
    logger.info(
        "Audio is %.1fs; splitting into %ds chunks.", duration, CHUNK_LENGTH_SECONDS
    )

    _run([
        "ffmpeg", "-y",
        "-i", wav_path,
        "-f", "segment",
        "-segment_time", str(CHUNK_LENGTH_SECONDS),
        "-c", "copy",
        pattern,
    ])

    chunks: list[AudioChunk] = []
    index = 0
    while True:
        chunk_path = os.path.join(out_dir, f"{base}_chunk_{index:03d}.wav")
        if not os.path.exists(chunk_path):
            break
        chunks.append(
            AudioChunk(
                index=index,
                path=chunk_path,
                start_seconds=index * CHUNK_LENGTH_SECONDS,
                duration_seconds=probe_duration(chunk_path),
            )
        )
        index += 1

    if not chunks:
        raise FFmpegError(f"Segmentation produced no chunks for {wav_path}")
    logger.info("Produced %d chunk(s).", len(chunks))
    return chunks


def extract_and_chunk(src_path: str, work_dir: str) -> list[AudioChunk]:
    """Full step: input media -> normalized WAV -> list of chunk(s) ready for AI."""
    os.makedirs(work_dir, exist_ok=True)
    wav_path = os.path.join(work_dir, "audio_16k_mono.wav")
    extract_wav(src_path, wav_path)
    return split_wav(wav_path, work_dir)
