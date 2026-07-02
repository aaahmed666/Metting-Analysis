"""
Module: Whisper Speech-to-Text Integration
Purpose: Interfaces with the Whisper AI model to transcribe audio files into text.
         Also handles speaker diarization to separate the sales rep's speech
         from the customer's speech.
Design notes
------------
* Transcription  — OpenAI ``whisper-1`` with ``verbose_json`` + segment-level
  timestamps. Segment granularity is the right trade-off: word-level timestamps
  cost more tokens and the segment boundary is sufficient for downstream scoring.
* Diarization    — A single structured GPT-4o call per chunk receives the raw
  segment list and returns ``rep`` / ``client`` labels.  This avoids the ~2 GB
  PyTorch dependency (pyannote.audio) while leveraging the model's understanding
  of sales-meeting conversational roles.
* Chunk merging  — Each segment's ``start`` / ``end`` are offset by the chunk's
  ``start_seconds`` so the final transcript has absolute timestamps regardless of
  how many 5-minute chunks the audio was split into.
"""

from __future__ import annotations
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal
from openai import OpenAI
from config.setting import get_settings
from services.ai_models.ffmpeg_processor import AudioChunk
logger = logging.getLogger(__name__)
# ---------------------------------------------------------------------------
# Public data models
# ---------------------------------------------------------------------------
SpeakerLabel = Literal["rep", "client", "unknown"]
@dataclass
class TranscriptSegment:
    """A single speaker turn inside the full transcript."""
    id: int
    start: float          # absolute seconds from the recording start
    end: float
    speaker: SpeakerLabel
    text: str
    def to_dict(self) -> dict:
        return asdict(self)
@dataclass
class TranscriptResult:
    """Complete, merged transcript for a single uploaded file."""
    file_id: str
    language: str | None
    duration_seconds: float
    chunk_count: int
    segments: list[TranscriptSegment] = field(default_factory=list)
    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "language": self.language,
            "duration_seconds": self.duration_seconds,
            "chunk_count": self.chunk_count,
            "segments": [seg.to_dict() for seg in self.segments],
        }
# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
_DIARIZE_SYSTEM_PROMPT = """\
You are an expert sales-call analyst. Your task is to assign a speaker label to
each segment of a sales-meeting transcript.
Rules:
1. Every segment must receive exactly one of: "rep" or "client".
2. "rep" is the sales representative — they typically introduce topics, ask
   discovery questions, handle objections, and pitch the product/service.
3. "client" is the prospect or customer — they ask clarifying questions,
   raise concerns, or respond to the rep's proposals.
4. Use conversational context, not just a single segment, to decide. Adjacent
   segments from the same speaker are common.
5. If you genuinely cannot determine the speaker, use "unknown".
6. Return ONLY a valid JSON array, one object per input segment, preserving the
   original order. Each object: {"id": <int>, "speaker": "<label>"}.
   Do NOT add any extra text outside the JSON array.
"""
@dataclass
class _RawSegment:
    """Intermediate container for a Whisper API segment before diarization."""
    id: int
    start: float
    end: float
    text: str
# ---------------------------------------------------------------------------
# Main service class
# ---------------------------------------------------------------------------
class WhisperTranscriber:
    """
    Orchestrates transcription + diarization for a list of audio chunks.
    Usage::
        transcriber = WhisperTranscriber()
        result = transcriber.transcribe_chunks(file_id="abc123", chunks=chunks)
        print(result.to_dict())
    """
    def __init__(self) -> None:
        settings = get_settings()
        self._client = OpenAI(
            api_key=settings.OPENAI_API_KEY.get_secret_value()
        )
        self._model = settings.WHISPER_MODEL
        self._language = settings.WHISPER_LANGUAGE
    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------
    def transcribe_chunks(
        self,
        file_id: str,
        chunks: list[AudioChunk],
    ) -> TranscriptResult:
        """
        Transcribe + diarize every chunk and merge into one ``TranscriptResult``.
        Args:
            file_id: Identifier of the original uploaded file (used for logging).
            chunks:  Ordered list of ``AudioChunk`` objects produced by the
                     audio-extraction step.
        Returns:
            A ``TranscriptResult`` containing all speaker-labelled segments with
            absolute timestamps.
        """
        if not chunks:
            raise ValueError("chunks must not be empty")
        logger.info(
            "Starting transcription for file_id=%s  chunks=%d",
            file_id, len(chunks),
        )
        all_segments: list[TranscriptSegment] = []
        detected_language: str | None = None
        previous_context: str = ""   # tail of the previous chunk for continuity
        global_seg_id: int = 0
        for chunk in chunks:
            logger.debug("Transcribing chunk index=%d  path=%s", chunk.index, chunk.path)
            raw_segs, lang = self._transcribe_chunk(chunk)
            if lang and not detected_language:
                detected_language = lang
            if not raw_segs:
                logger.warning("Chunk index=%d produced no segments; skipping.", chunk.index)
                continue
            # Diarize this chunk's segments using conversational context.
            labelled = self._diarize_segments(raw_segs, previous_context, chunk.index)
            # Apply absolute-timestamp offset then collect.
            for seg in labelled:
                seg.id = global_seg_id
                seg.start = round(seg.start + chunk.start_seconds, 3)
                seg.end = round(seg.end + chunk.start_seconds, 3)
                all_segments.append(seg)
                global_seg_id += 1
            # Feed the last ~400 chars of this chunk as context for the next one.
            previous_context = " ".join(s.text for s in labelled)[-400:]
        total_duration = (
            chunks[-1].start_seconds + chunks[-1].duration_seconds
            if chunks
            else 0.0
        )
        result = TranscriptResult(
            file_id=file_id,
            language=detected_language,
            duration_seconds=round(total_duration, 3),
            chunk_count=len(chunks),
            segments=all_segments,
        )
        logger.info(
            "Transcription complete  file_id=%s  segments=%d  language=%s",
            file_id, len(all_segments), detected_language,
        )
        return result
    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------
    def _transcribe_chunk(
        self, chunk: AudioChunk
    ) -> tuple[list[_RawSegment], str | None]:
        """
        Call the Whisper API for a single audio chunk.
        Returns:
            A tuple of (raw_segments, detected_language).  ``raw_segments``
            timestamps are *relative to the chunk start* (as returned by Whisper).
        """
        path = Path(chunk.path)
        if not path.exists():
            raise FileNotFoundError(f"Audio chunk not found: {chunk.path}")
        kwargs: dict = {
            "model": self._model,
            "response_format": "verbose_json",
            "timestamp_granularities": ["segment"],
        }
        if self._language:
            kwargs["language"] = self._language
        with path.open("rb") as audio_file:
            response = self._client.audio.transcriptions.create(
                file=audio_file,
                **kwargs,
            )
        raw_segments: list[_RawSegment] = []
        for seg in getattr(response, "segments", []):
            raw_segments.append(
                _RawSegment(
                    id=int(seg.get("id", len(raw_segments))),
                    start=float(seg.get("start", 0.0)),
                    end=float(seg.get("end", 0.0)),
                    text=seg.get("text", "").strip(),
                )
            )
        detected_language: str | None = getattr(response, "language", None)
        logger.debug(
            "Chunk index=%d  Whisper returned %d segment(s)  lang=%s",
            chunk.index, len(raw_segments), detected_language,
        )
        return raw_segments, detected_language
    def _diarize_segments(
        self,
        segments: list[_RawSegment],
        previous_context: str,
        chunk_index: int,
    ) -> list[TranscriptSegment]:
        """
        Ask GPT-4o to assign ``rep`` / ``client`` labels to each segment.
        The model receives the transcript text plus the tail of the previous
        chunk as context so speaker continuity is maintained across chunks.
        Args:
            segments:         Raw Whisper segments for this chunk.
            previous_context: Last ~400 chars of text from the previous chunk.
            chunk_index:      Used only for logging.
        Returns:
            A list of ``TranscriptSegment`` objects in the same order as input,
            with ``start`` / ``end`` still relative to the chunk start.
        """
        # Build the user message.
        context_block = (
            f"[Previous context]\n{previous_context}\n\n" if previous_context else ""
        )
        segments_json = json.dumps(
            [{"id": s.id, "text": s.text} for s in segments],
            ensure_ascii=False,
        )
        user_message = (
            f"{context_block}"
            f"[Segments to label]\n{segments_json}"
        )
        logger.debug(
            "Requesting diarization from GPT-4o  chunk=%d  segments=%d",
            chunk_index, len(segments),
        )
        response = self._client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _DIARIZE_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0,   # deterministic; no creativity needed here
        )
        raw_json = response.choices[0].message.content or "[]"
        # The prompt asks for a top-level JSON array; GPT-4o's json_object mode
        # requires a dict, so the model wraps it — handle both shapes.
        parsed = json.loads(raw_json)
        if isinstance(parsed, dict):
            # Accept {"segments": [...]} or {"labels": [...]} wrappers.
            labels_list: list[dict] = next(
                (v for v in parsed.values() if isinstance(v, list)), []
            )
        else:
            labels_list = parsed  # already a list
        # Build a fast lookup: segment id → speaker label.
        label_map: dict[int, SpeakerLabel] = {}
        for item in labels_list:
            sid = int(item.get("id", -1))
            raw_speaker = str(item.get("speaker", "unknown")).lower().strip()
            speaker: SpeakerLabel = (
                raw_speaker if raw_speaker in ("rep", "client") else "unknown"  # type: ignore[assignment]
            )
            label_map[sid] = speaker
        # Merge labels back onto the raw segments.
        result: list[TranscriptSegment] = []
        for seg in segments:
            result.append(
                TranscriptSegment(
                    id=seg.id,
                    start=seg.start,
                    end=seg.end,
                    speaker=label_map.get(seg.id, "unknown"),
                    text=seg.text,
                )
            )
        logger.debug(
            "Diarization complete  chunk=%d  labels=%s",
            chunk_index,
            {s.id: s.speaker for s in result},
        )
        return result