"""
Module: AI Analysis Service
Purpose: Parses the raw outputs from the pipeline (transcription, AI insights,
         scoring) and saves the fully mapped results to the database via
         AIAnalysisRepository.

This is the single public entry-point called by the pipeline results dispatcher.
It keeps all parsing and business-logic decisions separate from the database
access layer and the routing/controller layer.

Mapping summary
---------------
Meeting_Reports
  total_score               ← ScoringResult.final_score (rounded to int)
  grade                     ← ScoringResult.grade
  talk_ratio                ← ScoringResult.pillar_scores.talk_ratio (int)
  listening_score           ← InsightsResult.meeting_summary.customer_engagement_score
  discovery_score           ← ScoringResult.pillar_scores.discovery (int)
  objection_score           ← ScoringResult.pillar_scores.objection_handling (int)
  next_steps_score          ← ScoringResult.pillar_scores.next_steps (int)
  closing_score             ← ScoringResult.pillar_scores.closing (int)
  ai_summary                ← InsightsResult.meeting_summary.summary
  opening_script            ← First opening script in InsightsResult.opening_scripts_next_call
  decision_maker_identified ← True if "internal_champion" in opportunities
                               AND "missing_decision_maker" NOT in risks
  competitors_summary       ← All risk entries where category == "competitor_mention"

Transcripts
  start_time   ← TranscriptSegment.start
  end_time     ← TranscriptSegment.end
  text_segment ← TranscriptSegment.text
  speaker      ← TranscriptSegment.speaker
  sentiment    ← Nearest sentiment_trajectory entry resolved by timestamp

Signals
  signal_type  ← "risk" | "opportunity"
  keyword      ← KeywordEntry.keyword
  transcript_id ← Transcript row ID whose time range contains the keyword timestamp
"""
from __future__ import annotations

import logging
from typing import Any

from supabase import Client

from app.models.ai_analysis_models import (
    AIInsightsPayload,
    MeetingReportRecord,
    MeetingSummaryAI,
    SignalRecord,
    ScoringPayload,
    TranscriptPayload,
    TranscriptRecord,
)
from app.repositories.ai_analysis_repository import AIAnalysisRepository, RepositoryError

logger = logging.getLogger(__name__)

# Competitor-related categories as returned by the AI keyword detector.
_COMPETITOR_CATEGORIES = frozenset({"competitor_mention"})


class AIAnalysisServiceError(Exception):
    """Raised when the service layer cannot complete the save operation."""


class AIAnalysisService:
    """
    Orchestrates parsing and persistence of AI analysis results.

    Usage::

        service = AIAnalysisService(supabase_client)
        service.save_analysis(
            meeting_id=meeting_id,
            transcript=transcript_result.to_dict(),
            insights=insights_result.to_dict(),
            scoring=scoring_result.to_dict(),
        )
    """

    def __init__(self, supabase: Client) -> None:
        self._repo = AIAnalysisRepository(supabase)

    # ------------------------------------------------------------------
    # Public entry-point
    # ------------------------------------------------------------------

    def save_analysis(
        self,
        meeting_id:   str,
        transcript:   dict[str, Any],
        insights:     dict[str, Any],
        scoring:      dict[str, Any],
    ) -> dict[str, Any]:
        """
        Parse and persist all pipeline results for a completed meeting.

        Args:
            meeting_id: UUID of the Meetings row this analysis belongs to.
            transcript: ``TranscriptResult.to_dict()`` from the transcription step.
            insights:   ``InsightsResult.to_dict()`` from the insights-generation step.
            scoring:    ``ScoringResult.to_dict()`` from the scoring-engine step.

        Returns:
            A summary dict: {report_id, transcript_count, signal_count, meeting_id}

        Raises:
            AIAnalysisServiceError: On validation or DB errors.
        """
        logger.info("ai_analysis_service: starting save  meeting_id=%s", meeting_id)

        # --- 1. Validate inputs -------------------------------------------
        try:
            transcript_payload = TranscriptPayload(**transcript)
            insights_payload   = AIInsightsPayload(**insights)
            scoring_payload    = ScoringPayload(**scoring)
        except Exception as exc:
            raise AIAnalysisServiceError(
                f"Validation error for meeting_id={meeting_id}: {exc}"
            ) from exc

        # --- 2. Build and save Meeting_Reports row ------------------------
        report_record = _build_report_record(
            meeting_id, insights_payload, scoring_payload
        )
        try:
            report_id = self._repo.save_meeting_report(report_record)
        except RepositoryError as exc:
            raise AIAnalysisServiceError(str(exc)) from exc

        # --- 3. Build and save Transcripts rows ---------------------------
        transcript_records = _build_transcript_records(
            meeting_id, transcript_payload, insights_payload
        )
        try:
            segment_ids = self._repo.save_transcripts(transcript_records)
        except RepositoryError as exc:
            raise AIAnalysisServiceError(str(exc)) from exc

        # segment_ids[i] corresponds to transcript_payload.segments[i]
        id_by_index = {
            seg.id: db_id
            for seg, db_id in zip(transcript_payload.segments, segment_ids)
        }

        # --- 4. Build and save Signals rows -------------------------------
        signal_records = _build_signal_records(
            meeting_id, insights_payload, transcript_payload, id_by_index
        )
        try:
            signal_ids = self._repo.save_signals(signal_records)
        except RepositoryError as exc:
            raise AIAnalysisServiceError(str(exc)) from exc

        # --- 5. Update Meeting status to completed ------------------------
        try:
            self._repo.update_meeting_status(
                meeting_id,
                status="completed",
                duration_seconds=int(transcript_payload.duration_seconds),
            )
        except RepositoryError as exc:
            # Non-fatal — log and continue
            logger.error(
                "ai_analysis_service: failed to update meeting status  "
                "meeting_id=%s  error=%s",
                meeting_id,
                exc,
            )

        result = {
            "meeting_id":       meeting_id,
            "report_id":        report_id,
            "transcript_count": len(segment_ids),
            "signal_count":     len(signal_ids),
        }
        logger.info(
            "ai_analysis_service: save complete  meeting_id=%s  "
            "transcripts=%d  signals=%d",
            meeting_id,
            len(segment_ids),
            len(signal_ids),
        )
        return result


# ---------------------------------------------------------------------------
# Private mapping helpers
# ---------------------------------------------------------------------------

def _build_report_record(
    meeting_id: str,
    insights:   AIInsightsPayload,
    scoring:    ScoringPayload,
) -> MeetingReportRecord:
    """Map scoring + insights into the Meeting_Reports schema."""

    summary: MeetingSummaryAI = insights.meeting_summary
    pillars = scoring.pillar_scores

    # --- Opening script: use first entry; blank string → None -------------
    opening_script: str | None = None
    if insights.opening_scripts_next_call:
        script_text = insights.opening_scripts_next_call[0].script
        opening_script = script_text.strip() or None

    # --- Decision-maker flag ------------------------------------------
    risk_categories    = {r.category.lower() for r in insights.keyword_detection.risks}
    opp_categories     = {o.category.lower() for o in insights.keyword_detection.opportunities}
    decision_maker_identified = (
        "internal_champion" in opp_categories
        and "missing_decision_maker" not in risk_categories
    )

    # --- Competitors summary: all risk entries flagged as competitor ------
    competitor_entries: list[dict[str, Any]] = [
        {
            "keyword":     r.keyword,
            "quote":       r.quote,
            "confidence":  r.confidence,
            "explanation": r.explanation,
            "timestamp":   r.timestamp,
        }
        for r in insights.keyword_detection.risks
        if r.category.lower() in _COMPETITOR_CATEGORIES
    ]
    competitors_summary = competitor_entries or None

    return MeetingReportRecord(
        meeting_id                = meeting_id,
        total_score               = round(scoring.final_score),
        grade                     = scoring.grade.value,
        talk_ratio                = round(pillars.talk_ratio),
        listening_score           = summary.customer_engagement_score,
        discovery_score           = round(pillars.discovery),
        objection_score           = round(pillars.objection_handling),
        next_steps_score          = round(pillars.next_steps),
        closing_score             = round(pillars.closing),
        ai_summary                = summary.summary.strip() or None,
        opening_script            = opening_script,
        decision_maker_identified = decision_maker_identified,
        competitors_summary       = competitors_summary,
    )


def _build_transcript_records(
    meeting_id:  str,
    transcript:  TranscriptPayload,
    insights:    AIInsightsPayload,
) -> list[TranscriptRecord]:
    """
    Map every transcript segment to a TranscriptRecord, resolving the
    sentiment for each segment from the AI's sentiment_trajectory.
    """
    return [
        TranscriptRecord(
            meeting_id   = meeting_id,
            start_time   = seg.start,
            end_time     = seg.end,
            text_segment = seg.text,
            speaker      = seg.speaker.value,
            sentiment    = _resolve_sentiment(
                seg.start, seg.end, insights
            ),
        )
        for seg in transcript.segments
    ]


def _resolve_sentiment(
    start:    float,
    end:      float,
    insights: AIInsightsPayload,
) -> str | None:
    """
    Find the sentiment label for a transcript segment by locating the
    sentiment_trajectory entry whose midpoint is closest to the segment's
    midpoint.

    Returns one of: "positive", "neutral", "negative", or None when
    the trajectory is empty or the timestamp cannot be parsed.
    """
    if not insights.sentiment_trajectory:
        return None

    mid = (start + end) / 2.0
    best_entry = None
    best_dist  = float("inf")

    for entry in insights.sentiment_trajectory:
        # Timestamps from the AI may be formatted as "MM:SS" or seconds
        ts_seconds = _parse_timestamp(entry.timestamp)
        if ts_seconds is None:
            continue
        dist = abs(ts_seconds - mid)
        if dist < best_dist:
            best_dist  = dist
            best_entry = entry

    if best_entry is None:
        return None

    sentiment_raw = best_entry.sentiment.lower()
    valid = {"positive", "neutral", "negative"}
    return sentiment_raw if sentiment_raw in valid else None


def _parse_timestamp(ts: str) -> float | None:
    """
    Convert a timestamp string to seconds.

    Handles:
    * "MM:SS"      → e.g. "02:30" → 150.0
    * "HH:MM:SS"   → e.g. "00:02:30" → 150.0
    * Numeric str  → e.g. "150.0" → 150.0
    """
    ts = ts.strip()
    parts = ts.split(":")
    try:
        if len(parts) == 2:   # MM:SS
            return float(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:   # HH:MM:SS
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        return float(ts)      # plain seconds
    except ValueError:
        return None


def _build_signal_records(
    meeting_id:   str,
    insights:     AIInsightsPayload,
    transcript:   TranscriptPayload,
    id_by_index:  dict[int, str],
) -> list[SignalRecord]:
    """
    Convert every keyword in keyword_detection into a SignalRecord,
    linking it to the nearest Transcript row by timestamp.
    """
    records: list[SignalRecord] = []

    def _make_signals(keywords: list, signal_type: str) -> None:
        for kw in keywords:
            ts_seconds = _parse_timestamp(kw.timestamp)
            db_transcript_id = _find_nearest_transcript_id(
                ts_seconds, transcript, id_by_index
            )
            if db_transcript_id is None:
                logger.debug(
                    "ai_analysis_service: no matching transcript for signal  "
                    "keyword=%s  timestamp=%s",
                    kw.keyword,
                    kw.timestamp,
                )
                continue

            records.append(
                SignalRecord(
                    meeting_id    = meeting_id,
                    transcript_id = db_transcript_id,
                    signal_type   = signal_type,
                    keyword       = kw.keyword,
                )
            )

    _make_signals(insights.keyword_detection.risks, "risk")
    _make_signals(insights.keyword_detection.opportunities, "opportunity")
    return records


def _find_nearest_transcript_id(
    ts_seconds:  float | None,
    transcript:  TranscriptPayload,
    id_by_index: dict[int, str],
) -> str | None:
    """
    Return the Supabase UUID of the transcript segment that contains
    (or is closest to) the given timestamp in seconds.
    """
    if not transcript.segments:
        return None

    # Default to the first segment when the timestamp cannot be resolved
    if ts_seconds is None:
        first = transcript.segments[0]
        return id_by_index.get(first.id)

    best_seg  = None
    best_dist = float("inf")

    for seg in transcript.segments:
        # Prefer a segment that contains the timestamp
        if seg.start <= ts_seconds <= seg.end:
            return id_by_index.get(seg.id)
        # Otherwise, distance to the nearest endpoint
        dist = min(abs(ts_seconds - seg.start), abs(ts_seconds - seg.end))
        if dist < best_dist:
            best_dist = dist
            best_seg  = seg

    if best_seg is None:
        return None
    return id_by_index.get(best_seg.id)
