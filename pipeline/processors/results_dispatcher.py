"""
Module: Processor Step 8 - Results Dispatcher
Purpose: The final step. Saves the complete analysis and scoring to the database,
         and triggers the CRM webhook and Email notification services.

This module is intentionally thin. All parsing, mapping, and DB-write logic
lives in ``app.services.ai_analysis_service.AIAnalysisService`` so it can be
tested and reused independently of the pipeline.

``dispatch`` is the single public entry-point called by the pipeline orchestrator.
It accepts the outputs of every preceding pipeline step and returns a persistence
summary dict.
"""
from __future__ import annotations

import logging
from typing import Any

from supabase import Client

from app.services.ai_analysis_service import AIAnalysisService, AIAnalysisServiceError

logger = logging.getLogger(__name__)


def dispatch(
    meeting_id: str,
    transcript: dict[str, Any],
    insights:   dict[str, Any],
    scoring:    dict[str, Any],
    supabase:   Client,
) -> dict[str, Any]:
    """
    Persist the full AI analysis results for a meeting to the database.

    This is the single entry-point called by the pipeline orchestrator and is
    responsible for delegating to ``AIAnalysisService``.

    Args:
        meeting_id: UUID of the Meetings row this analysis belongs to.
        transcript: ``TranscriptResult.to_dict()`` from the transcription step.
        insights:   ``InsightsResult.to_dict()`` from the insights-generation step.
        scoring:    ``ScoringResult.to_dict()`` from the scoring-engine step.
        supabase:   Authenticated Supabase client (service-role key).

    Returns:
        A summary dict: {meeting_id, report_id, transcript_count, signal_count}

    Raises:
        AIAnalysisServiceError: Propagated unchanged when the service layer
            cannot persist results. The orchestrator handles retry / failure
            recording.
    """
    logger.info("results_dispatcher: starting  meeting_id=%s", meeting_id)

    service = AIAnalysisService(supabase)
    try:
        result = service.save_analysis(
            meeting_id=meeting_id,
            transcript=transcript,
            insights=insights,
            scoring=scoring,
        )
    except AIAnalysisServiceError:
        logger.exception(
            "results_dispatcher: save failed  meeting_id=%s", meeting_id
        )
        raise

    logger.info(
        "results_dispatcher: done  meeting_id=%s  report_id=%s  "
        "transcripts=%d  signals=%d",
        meeting_id,
        result.get("report_id"),
        result.get("transcript_count", 0),
        result.get("signal_count", 0),
    )
    return result