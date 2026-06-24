# """
# Unit tests for the Whisper transcription + GPT-4o diarization step.
# All OpenAI API calls are mocked — no network traffic, no credentials required.
# Tests verify:
#   1. Correct parsing of the Whisper ``verbose_json`` segment response.
#   2. Correct parsing of the GPT-4o diarization JSON response (both array and
#      dict-wrapped shapes).
#   3. Absolute-timestamp offset is applied correctly when merging multi-chunk
#      transcripts.
#   4. ``TranscriptResult.to_dict()`` produces the expected JSON structure.
#   5. ``unknown`` fallback is used when GPT-4o returns an unrecognised label.
# """

# from __future__ import annotations
# import json
# from dataclasses import dataclass
# from types import SimpleNamespace
# from unittest.mock import MagicMock, patch
# import pytest
# from services.ai_models.ffmpeg_processor import AudioChunk
# from services.audio.stt_whisper import (
#     TranscriptResult,
#     TranscriptSegment,
#     WhisperTranscriber,
#     _RawSegment,
# )
# # ---------------------------------------------------------------------------
# # Fixtures
# # ---------------------------------------------------------------------------
# def _make_chunk(
#     index: int,
#     path: str = "/fake/chunk.wav",
#     start_seconds: float = 0.0,
#     duration_seconds: float = 60.0,
# ) -> AudioChunk:
#     return AudioChunk(
#         index=index,
#         path=path,
#         start_seconds=start_seconds,
#         duration_seconds=duration_seconds,
#     )
# def _whisper_response(segments: list[dict], language: str = "en") -> SimpleNamespace:
#     """Build a fake object that mimics the OpenAI transcription response."""
#     seg_objs = [SimpleNamespace(**s) for s in segments]
#     return SimpleNamespace(segments=seg_objs, language=language)
# def _gpt_response(content: str) -> SimpleNamespace:
#     """Build a fake object that mimics a ChatCompletion response."""
#     choice = SimpleNamespace(message=SimpleNamespace(content=content))
#     return SimpleNamespace(choices=[choice])
# # ---------------------------------------------------------------------------
# # TranscriptResult serialisation
# # ---------------------------------------------------------------------------
# class TestTranscriptResult:
#     def test_to_dict_structure(self):
#         seg = TranscriptSegment(id=0, start=0.0, end=5.0, speaker="rep", text="Hello")
#         result = TranscriptResult(
#             file_id="file123",
#             language="en",
#             duration_seconds=60.0,
#             chunk_count=1,
#             segments=[seg],
#         )
#         d = result.to_dict()
#         assert d["file_id"] == "file123"
#         assert d["language"] == "en"
#         assert d["duration_seconds"] == 60.0
#         assert d["chunk_count"] == 1
#         assert len(d["segments"]) == 1
#         assert d["segments"][0]["speaker"] == "rep"
#         assert d["segments"][0]["text"] == "Hello"
#     def test_to_dict_empty_segments(self):
#         result = TranscriptResult(
#             file_id="x",
#             language=None,
#             duration_seconds=0.0,
#             chunk_count=0,
#         )
#         d = result.to_dict()
#         assert d["segments"] == []
#         assert d["language"] is None
# # ---------------------------------------------------------------------------
# # WhisperTranscriber._diarize_segments
# # ---------------------------------------------------------------------------
# class TestDiarizeSegments:
#     """Tests for the GPT-4o diarization step in isolation."""
#     def _transcriber_with_mock_client(self, gpt_content: str) -> WhisperTranscriber:
#         """Return a WhisperTranscriber whose OpenAI client is fully mocked."""
#         transcriber = object.__new__(WhisperTranscriber)
#         mock_client = MagicMock()
#         mock_client.chat.completions.create.return_value = _gpt_response(gpt_content)
#         transcriber._client = mock_client
#         transcriber._model = "whisper-1"
#         transcriber._language = None
#         return transcriber
#     def test_rep_and_client_labels(self):
#         gpt_content = json.dumps(
#             [{"id": 0, "speaker": "rep"}, {"id": 1, "speaker": "client"}]
#         )
#         transcriber = self._transcriber_with_mock_client(gpt_content)
#         raw = [
#             _RawSegment(id=0, start=0.0, end=3.0, text="Hi, I wanted to show you our product."),
#             _RawSegment(id=1, start=3.5, end=6.0, text="Sounds interesting, tell me more."),
#         ]
#         result = transcriber._diarize_segments(raw, "", chunk_index=0)
#         assert result[0].speaker == "rep"
#         assert result[1].speaker == "client"
#     def test_dict_wrapped_gpt_response(self):
#         """GPT-4o json_object mode may wrap the array in a dict key."""
#         gpt_content = json.dumps(
#             {"segments": [{"id": 0, "speaker": "client"}, {"id": 1, "speaker": "rep"}]}
#         )
#         transcriber = self._transcriber_with_mock_client(gpt_content)
#         raw = [
#             _RawSegment(id=0, start=0.0, end=2.0, text="What's the price?"),
#             _RawSegment(id=1, start=2.5, end=5.0, text="It's $500 per month."),
#         ]
#         result = transcriber._diarize_segments(raw, "", chunk_index=0)
#         assert result[0].speaker == "client"
#         assert result[1].speaker == "rep"
#     def test_unknown_fallback_for_unrecognised_label(self):
#         gpt_content = json.dumps([{"id": 0, "speaker": "narrator"}])
#         transcriber = self._transcriber_with_mock_client(gpt_content)
#         raw = [_RawSegment(id=0, start=0.0, end=2.0, text="Some text.")]
#         result = transcriber._diarize_segments(raw, "", chunk_index=0)
#         assert result[0].speaker == "unknown"
#     def test_missing_label_defaults_to_unknown(self):
#         """Segment id not present in GPT-4o response → unknown."""
#         gpt_content = json.dumps([{"id": 99, "speaker": "rep"}])  # wrong id
#         transcriber = self._transcriber_with_mock_client(gpt_content)
#         raw = [_RawSegment(id=0, start=0.0, end=2.0, text="Missing segment.")]
#         result = transcriber._diarize_segments(raw, "", chunk_index=0)
#         assert result[0].speaker == "unknown"
# # ---------------------------------------------------------------------------
# # WhisperTranscriber.transcribe_chunks — timestamp offset merging
# # ---------------------------------------------------------------------------
# class TestTimestampOffset:
#     """Verifies that chunk.start_seconds is correctly added to segment timestamps."""
#     @patch("services.audio.stt_whisper.Path")
#     def test_single_chunk_no_offset(self, mock_path):
#         mock_path.return_value.exists.return_value = True
#         mock_path.return_value.open.return_value.__enter__ = lambda s: MagicMock()
#         mock_path.return_value.open.return_value.__exit__ = MagicMock(return_value=False)
#         transcriber = object.__new__(WhisperTranscriber)
#         mock_client = MagicMock()
#         whisper_segs = [{"id": 0, "start": 1.0, "end": 3.0, "text": "Hello."}]
#         mock_client.audio.transcriptions.create.return_value = _whisper_response(
#             whisper_segs, language="en"
#         )
#         mock_client.chat.completions.create.return_value = _gpt_response(
#             json.dumps([{"id": 0, "speaker": "rep"}])
#         )
#         transcriber._client = mock_client
#         transcriber._model = "whisper-1"
#         transcriber._language = None
#         chunk = _make_chunk(index=0, start_seconds=0.0, duration_seconds=10.0)
#         result = transcriber.transcribe_chunks("file_abc", [chunk])
#         assert result.segments[0].start == 1.0
#         assert result.segments[0].end == 3.0
#     @patch("services.audio.stt_whisper.Path")
#     def test_second_chunk_offset_applied(self, mock_path):
#         """Segments from chunk index=1 (start_seconds=300) should have +300 offset."""
#         mock_path.return_value.exists.return_value = True
#         mock_path.return_value.open.return_value.__enter__ = lambda s: MagicMock()
#         mock_path.return_value.open.return_value.__exit__ = MagicMock(return_value=False)
#         transcriber = object.__new__(WhisperTranscriber)
#         mock_client = MagicMock()
#         def transcription_side_effect(*args, **kwargs):
#             return _whisper_response(
#                 [{"id": 0, "start": 5.0, "end": 10.0, "text": "Segment text."}],
#                 language="en",
#             )
#         mock_client.audio.transcriptions.create.side_effect = transcription_side_effect
#         mock_client.chat.completions.create.return_value = _gpt_response(
#             json.dumps([{"id": 0, "speaker": "client"}])
#         )
#         transcriber._client = mock_client
#         transcriber._model = "whisper-1"
#         transcriber._language = None
#         chunk = _make_chunk(index=1, start_seconds=300.0, duration_seconds=60.0)
#         result = transcriber.transcribe_chunks("file_abc", [chunk])
#         assert result.segments[0].start == 305.0   # 5.0 + 300.0
#         assert result.segments[0].end == 310.0    # 10.0 + 300.0
# # ---------------------------------------------------------------------------
# # transcribe() pipeline adapter smoke test
# # ---------------------------------------------------------------------------
# class TestPipelineAdapter:
#     @patch("pipeline.processors.speech_transcriber._transcriber", None)
#     @patch("pipeline.processors.speech_transcriber.WhisperTranscriber")
#     def test_transcribe_delegates_to_service(self, mock_cls):
#         from pipeline.processors.speech_transcriber import transcribe
#         mock_instance = MagicMock()
#         mock_instance.transcribe_chunks.return_value = TranscriptResult(
#             file_id="f1",
#             language="en",
#             duration_seconds=30.0,
#             chunk_count=1,
#             segments=[TranscriptSegment(0, 0.0, 5.0, "rep", "Hello.")],
#         )
#         mock_cls.return_value = mock_instance
#         chunk = _make_chunk(0)
#         result = transcribe("f1", [chunk])
#         mock_instance.transcribe_chunks.assert_called_once_with(file_id="f1", chunks=[chunk])
#         assert result.file_id == "f1"
#         assert result.segments[0].speaker == "rep"