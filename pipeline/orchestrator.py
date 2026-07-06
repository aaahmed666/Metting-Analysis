"""
Module: Pipeline Orchestrator
Purpose: The central manager of the entire analysis workflow.
         Executes all pipeline processors in the correct sequential order,
         passes data between steps, and handles global error recovery.

Pipeline stages (in order)
---------------------------
Step 1 : media_validator   — validate extension, size, magic bytes
Step 2 : audio_extractor   — convert to 16 kHz mono WAV, split into chunks
Step 3 : speech_transcriber — transcribe + diarize chunks via Whisper / GPT-4o
Step 4 : acoustic_analyzer  — (stub) injects acoustic signal features
Step 5 : context_verifier   — (stub) LLM check: real sales meeting?
Step 6 : insights_generator — Gemini: sentiment, keywords, opening scripts
Step 7 : scoring_engine     — weighted 5-pillar score + grade
Step 8 : results_dispatcher — save all results to DB

Public entry-point
------------------
  ``run_pipeline(meeting_id, file_path, supabase)``

On any un-recoverable error the meeting status is updated to ``rejected``
with the reason, and the exception is re-raised so the Celery task layer
can handle retries.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from supabase import Client

from app.repositories.ai_analysis_repository import AIAnalysisRepository, RepositoryError
from pipeline.processors.audio_extractor    import extract_audio_chunks
from pipeline.processors.insights_generator import generate
from pipeline.processors.scoring_engine     import score
from pipeline.processors.speech_transcriber import transcribe
from pipeline.processors.results_dispatcher import dispatch

logger = logging.getLogger(__name__)


class PipelineError(Exception):
    """
    Raised when a pipeline step fails in a way that cannot be recovered.
    Carries a human-readable ``rejection_reason`` for the Meetings table.
    """
    def __init__(self, message: str, rejection_reason: str) -> None:
        super().__init__(message)
        self.rejection_reason = rejection_reason


def run_pipeline(
    meeting_id: str,
    file_path:  str,
    supabase:   Client,
    work_dir:   str | None = None,
) -> dict[str, Any]:
    """
    Execute the full 8-step analysis pipeline for a single meeting file.

    Args:
        meeting_id: UUID of the Meetings row to associate results with.
        file_path:  Absolute path to the downloaded media file (mp4, wav, etc.).
        supabase:   Authenticated Supabase admin client.
        work_dir:   Directory for temporary audio files. Defaults to
                    ``/tmp/media_processing/<meeting_id>``.

    Returns:
        The persistence summary returned by Step 8 (results_dispatcher):
        {meeting_id, report_id, transcript_count, signal_count}

    Raises:
        PipelineError: On any unrecoverable step failure.
    """
    from config.setting import get_settings
    settings  = get_settings()
    _work_dir = work_dir or os.path.join(settings.PROCESSING_DIR, meeting_id)

    repo = AIAnalysisRepository(supabase)

    logger.info(
        "orchestrator: pipeline started  meeting_id=%s  file=%s",
        meeting_id,
        file_path,
    )

    # ── Step 2: Audio extraction ─────────────────────────────────────────
    logger.info("orchestrator: [Step 2] audio extraction  meeting_id=%s", meeting_id)
    try:
        chunks = extract_audio_chunks(file_path, _work_dir)
    except Exception as exc:
        _reject(repo, meeting_id, "Audio extraction failed")
        raise PipelineError(
            f"Step 2 audio_extractor failed: {exc}",
            rejection_reason="Audio extraction failed",
        ) from exc

    # ── Step 3: Speech transcription + diarization ───────────────────────
    logger.info(
        "orchestrator: [Step 3] speech transcription  meeting_id=%s  chunks=%d",
        meeting_id,
        len(chunks),
    )
    try:
        transcript_result = transcribe(file_id=meeting_id, chunks=chunks)
    except Exception as exc:
        _reject(repo, meeting_id, "Transcription failed")
        raise PipelineError(
            f"Step 3 speech_transcriber failed: {exc}",
            rejection_reason="Transcription failed",
        ) from exc

    # Guard: ensure we actually got segments
    if not transcript_result.segments:
        _reject(repo, meeting_id, "No speech detected in the recording")
        raise PipelineError(
            "Step 3 produced 0 transcript segments",
            rejection_reason="No speech detected in the recording",
        )

    # ── Step 4: Acoustic analysis (stub — pass-through) ──────────────────
    logger.info("orchestrator: [Step 4] acoustic analysis (stub)  meeting_id=%s", meeting_id)
    # acoustic_analyzer.analyze(transcript_result)  ← implement when ready

    # ── Step 5: Context verification (stub — pass-through) ───────────────
    logger.info("orchestrator: [Step 5] context verification (stub)  meeting_id=%s", meeting_id)
    # context_verifier.verify(transcript_result)    ← implement when ready

    # Build the plain text string the LLM will analyse
    transcript_text = _build_transcript_text(transcript_result)

    # ── Step 6: AI insights generation ───────────────────────────────────
    logger.info("orchestrator: [Step 6] insights generation  meeting_id=%s", meeting_id)
    try:
        insights_result = generate(file_id=meeting_id, transcript=transcript_text)
    except Exception as exc:
        _reject(repo, meeting_id, "AI insights generation failed")
        raise PipelineError(
            f"Step 6 insights_generator failed: {exc}",
            rejection_reason="AI insights generation failed",
        ) from exc

    # ── Step 7: Scoring engine ────────────────────────────────────────────
    logger.info("orchestrator: [Step 7] scoring  meeting_id=%s", meeting_id)
    try:
        scoring_result = _run_scoring(insights_result)
    except Exception as exc:
        _reject(repo, meeting_id, "Scoring failed")
        raise PipelineError(
            f"Step 7 scoring_engine failed: {exc}",
            rejection_reason="Scoring failed",
        ) from exc

    # ── Step 8: Persist all results ───────────────────────────────────────
    logger.info("orchestrator: [Step 8] saving results  meeting_id=%s", meeting_id)
    try:
        summary = dispatch(
            meeting_id = meeting_id,
            transcript = transcript_result.to_dict(),
            insights   = insights_result.to_dict(),
            scoring    = scoring_result.to_dict(),
            supabase   = supabase,
        )
    except Exception as exc:
        _reject(repo, meeting_id, "Failed to save results")
        raise PipelineError(
            f"Step 8 results_dispatcher failed: {exc}",
            rejection_reason="Failed to save results",
        ) from exc

    logger.info(
        "orchestrator: pipeline complete  meeting_id=%s  score=%.2f  grade=%s",
        meeting_id,
        scoring_result.final_score,
        scoring_result.grade,
    )
    return summary


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _build_transcript_text(transcript_result) -> str:
    """
    Serialise all transcript segments into a single formatted string suitable
    for LLM analysis.

    Format per segment:
        [MM:SS] SPEAKER: text
    """
    lines: list[str] = []
    for seg in transcript_result.segments:
        minutes = int(seg.start // 60)
        seconds = int(seg.start % 60)
        speaker = seg.speaker.upper() if hasattr(seg.speaker, "upper") else str(seg.speaker).upper()
        lines.append(f"[{minutes:02d}:{seconds:02d}] {speaker}: {seg.text.strip()}")
    return "\n".join(lines)


def _run_scoring(insights_result) -> Any:
    """
    Extract pillar scores from insights and run the scoring engine.

    Score derivation
    ----------------
    discovery          ← likelihood_to_close_score (0-100)
    objection_handling ← 100 − (risk count × 10), floored at 0
    talk_ratio         ← rep share of segments (clamped 0-100)
    next_steps         ← recommended_next_actions count × 20, capped at 100
    closing            ← customer_engagement_score (0-100)
    """
    summary  = insights_result.meeting_summary
    kd       = insights_result.keyword_detection

    discovery          = float(summary.get("likelihood_to_close_score", 50))
    objection_handling = max(0.0, 100.0 - len(kd.get("risks", [])) * 10)
    next_steps         = min(100.0, len(insights_result.recommended_next_actions) * 20)
    closing            = float(summary.get("customer_engagement_score", 50))

    # talk_ratio: derive from meeting_summary if available, else default 50
    talk_ratio = float(
        summary.get("talk_ratio_score", 50)  # future AI field
        if isinstance(summary, dict) and "talk_ratio_score" in summary
        else 50
    )

    return score(
        discovery          = discovery,
        objection_handling = objection_handling,
        talk_ratio         = talk_ratio,
        next_steps         = next_steps,
        closing            = closing,
    )


def _reject(repo: AIAnalysisRepository, meeting_id: str, reason: str) -> None:
    """Set the meeting status to ``rejected`` with the given reason (best-effort)."""
    try:
        repo.update_meeting_status(
            meeting_id,
            status="rejected",
            rejection_reason=reason,
        )
    except RepositoryError as exc:
        logger.error(
            "orchestrator: failed to update meeting to rejected  "
            "meeting_id=%s  error=%s",
            meeting_id,
            exc,
        )