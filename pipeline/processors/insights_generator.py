"""
Module: Processor Step 6 - AI Insights Generator
Purpose: Feeds the verified transcript to the Gemini LLM to extract deep
         sales-intelligence insights: sentiment trajectory, opening scripts,
         keyword detection (risks & opportunities), and recommended next actions.

Design notes
------------
* This module is intentionally thin.  All prompt templates live in
  ``prompts.sales_intelligence`` and all LLM I/O lives in
  ``services.audio.gemini_client.GeminiClient`` so each layer can be tested
  and evolved independently.
* ``generate`` is the single public entry-point called by the pipeline
  orchestrator.  It accepts the raw transcript text and returns a validated
  ``InsightsResult`` dataclass.
* JSON validation is strict: any field missing from the LLM response is
  replaced with a safe default rather than crashing the pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from prompts.sales_intelligence import (
    SALES_INTELLIGENCE_SYSTEM,
    build_user_message,
)
from services.audio.gemini_client import GeminiClient, LLMClientError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton — GeminiClient is stateless and thread-safe.
# ---------------------------------------------------------------------------
_client: GeminiClient | None = None


def _get_client() -> GeminiClient:
    global _client
    if _client is None:
        _client = GeminiClient()
    return _client


# ---------------------------------------------------------------------------
# Public result model
# ---------------------------------------------------------------------------

@dataclass
class InsightsResult:
    """
    Structured container for the full sales-intelligence analysis produced
    by the Gemini LLM.

    Every top-level key maps 1-to-1 with the JSON schema defined in
    ``prompts.sales_intelligence``.
    """

    file_id: str

    # Core summary block.
    meeting_summary: dict[str, Any] = field(default_factory=dict)

    # Ordered list of sentiment segments.
    sentiment_trajectory: list[dict[str, Any]] = field(default_factory=list)

    # Exactly 3 opening-script objects.
    opening_scripts_next_call: list[dict[str, Any]] = field(default_factory=list)

    # Keyword detection split into risks and opportunities.
    keyword_detection: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {"risks": [], "opportunities": []}
    )

    # Prioritised action list.
    recommended_next_actions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dictionary suitable for storage or API responses."""
        return {
            "file_id":                   self.file_id,
            "meeting_summary":           self.meeting_summary,
            "sentiment_trajectory":      self.sentiment_trajectory,
            "opening_scripts_next_call": self.opening_scripts_next_call,
            "keyword_detection":         self.keyword_detection,
            "recommended_next_actions":  self.recommended_next_actions,
        }


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

def generate(file_id: str, transcript: str) -> InsightsResult:
    """
    Run the full sales-intelligence analysis on a meeting transcript.

    This is the single entry-point called by the pipeline orchestrator.

    Args:
        file_id:    Identifier of the uploaded media file (used for logging).
        transcript: Full text of the meeting transcript, including timestamps
                    and speaker labels as produced by the speech-transcription
                    step.

    Returns:
        An ``InsightsResult`` populated with every analysis dimension.

    Raises:
        LLMClientError: Propagated unchanged when the Gemini API fails.
                        The orchestrator handles retry / failure recording.
    """
    if not transcript or not transcript.strip():
        logger.warning("insights_generator: empty transcript  file_id=%s", file_id)
        return InsightsResult(file_id=file_id)

    logger.info("insights_generator: starting analysis  file_id=%s", file_id)

    user_message = build_user_message(transcript)

    try:
        raw: dict[str, Any] = _get_client().complete(  # type: ignore[assignment]
            system=SALES_INTELLIGENCE_SYSTEM,
            user=user_message,
        )
    except LLMClientError:
        logger.exception("insights_generator: Gemini request failed  file_id=%s", file_id)
        raise

    result = _parse_llm_response(file_id, raw)

    logger.info(
        "insights_generator: done  file_id=%s  sentiment=%s  risks=%d  opportunities=%d",
        file_id,
        result.meeting_summary.get("overall_sentiment", "unknown"),
        len(result.keyword_detection.get("risks", [])),
        len(result.keyword_detection.get("opportunities", [])),
    )
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_llm_response(file_id: str, raw: dict[str, Any]) -> InsightsResult:
    """
    Convert the raw Gemini JSON payload into a validated ``InsightsResult``.

    Missing or malformed fields are replaced with safe defaults so a single
    bad LLM response never crashes the entire pipeline.

    Args:
        file_id: Used to populate the result and for warning messages.
        raw:     Parsed JSON dict returned by ``GeminiClient.complete``.

    Returns:
        A fully initialised ``InsightsResult``.
    """
    def _get_list(key: str) -> list:
        value = raw.get(key)
        if isinstance(value, list):
            return value
        logger.warning(
            "insights_generator: missing or invalid '%s' in response  file_id=%s",
            key, file_id,
        )
        return []

    def _get_dict(key: str) -> dict:
        value = raw.get(key)
        if isinstance(value, dict):
            return value
        logger.warning(
            "insights_generator: missing or invalid '%s' in response  file_id=%s",
            key, file_id,
        )
        return {}

    keyword_raw = _get_dict("keyword_detection")
    keyword_detection: dict[str, list] = {
        "risks": (
            keyword_raw.get("risks", [])
            if isinstance(keyword_raw.get("risks"), list)
            else []
        ),
        "opportunities": (
            keyword_raw.get("opportunities", [])
            if isinstance(keyword_raw.get("opportunities"), list)
            else []
        ),
    }

    return InsightsResult(
        file_id=file_id,
        meeting_summary=_get_dict("meeting_summary"),
        sentiment_trajectory=_get_list("sentiment_trajectory"),
        opening_scripts_next_call=_get_list("opening_scripts_next_call"),
        keyword_detection=keyword_detection,
        recommended_next_actions=_get_list("recommended_next_actions"),
    )