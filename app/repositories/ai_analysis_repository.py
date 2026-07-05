"""
Module: AI Analysis Repository
Purpose: All database INSERT operations for persisting AI analysis results
         to the Meeting_Reports, Transcripts, and Signals tables via Supabase.

Design notes
------------
* This class is intentionally free of business/parsing logic. It receives
  pre-validated, pre-mapped record objects from the service layer and executes
  the corresponding Supabase insert calls.
* All public methods raise ``RepositoryError`` on any DB failure so the calling
  service can handle errors uniformly without importing Supabase internals.
* ``save_meeting_report`` uses upsert (on meeting_id conflict) so re-running
  the pipeline for the same meeting safely overwrites stale data rather than
  creating duplicate rows.
* Transcripts and Signals are inserted in bulk (one call each) to minimise
  round-trips.
"""
from __future__ import annotations

import logging
from typing import Any

from supabase import Client

from app.models.ai_analysis_models import (
    MeetingReportRecord,
    SignalRecord,
    TranscriptRecord,
)

logger = logging.getLogger(__name__)


class RepositoryError(Exception):
    """Raised when a Supabase operation fails in the repository layer."""


class AIAnalysisRepository:
    """
    Persists AI analysis results to the database.

    Usage::

        repo = AIAnalysisRepository(supabase_client)
        report_id   = repo.save_meeting_report(report_record)
        segment_ids = repo.save_transcripts(transcript_records)
        repo.save_signals(signal_records)
    """

    def __init__(self, supabase: Client) -> None:
        self._client = supabase

    # ------------------------------------------------------------------
    # Meeting_Reports
    # ------------------------------------------------------------------

    def save_meeting_report(self, record: MeetingReportRecord) -> str:
        """
        Upsert a row in ``Meeting_Reports`` for the given meeting.

        Returns the UUID of the inserted/updated row.

        Raises:
            RepositoryError: On any Supabase error.
        """
        payload = _build_report_payload(record)

        logger.debug(
            "ai_analysis_repository: upserting Meeting_Reports  meeting_id=%s",
            record.meeting_id,
        )

        try:
            response = (
                self._client.table("Meeting_Reports")
                .upsert(payload, on_conflict="meeting_id")
                .execute()
            )
        except Exception as exc:
            raise RepositoryError(
                f"Failed to upsert Meeting_Reports for meeting_id={record.meeting_id}: {exc}"
            ) from exc

        if not response.data:
            raise RepositoryError(
                f"Meeting_Reports upsert returned no data for meeting_id={record.meeting_id}"
            )

        row_id: str = response.data[0]["id"]
        logger.info(
            "ai_analysis_repository: Meeting_Reports saved  id=%s  meeting_id=%s",
            row_id,
            record.meeting_id,
        )
        return row_id

    # ------------------------------------------------------------------
    # Transcripts
    # ------------------------------------------------------------------

    def save_transcripts(self, records: list[TranscriptRecord]) -> list[str]:
        """
        Bulk-insert all transcript segments for a meeting into ``Transcripts``.

        Returns an ordered list of the inserted row UUIDs (same order as input).

        Raises:
            RepositoryError: On any Supabase error.
        """
        if not records:
            logger.debug("ai_analysis_repository: no transcript segments to insert.")
            return []

        payloads: list[dict[str, Any]] = [
            {
                "meeting_id":   r.meeting_id,
                "start_time":   r.start_time,
                "end_time":     r.end_time,
                "text_segment": r.text_segment,
                "speaker":      r.speaker,
                **({"sentiment": r.sentiment} if r.sentiment else {}),
            }
            for r in records
        ]

        logger.debug(
            "ai_analysis_repository: inserting %d transcript segments  meeting_id=%s",
            len(payloads),
            records[0].meeting_id,
        )

        try:
            response = (
                self._client.table("Transcripts")
                .insert(payloads)
                .execute()
            )
        except Exception as exc:
            raise RepositoryError(
                f"Failed to insert Transcripts for meeting_id={records[0].meeting_id}: {exc}"
            ) from exc

        if not response.data:
            raise RepositoryError(
                f"Transcripts insert returned no data for meeting_id={records[0].meeting_id}"
            )

        inserted_ids = [row["id"] for row in response.data]
        logger.info(
            "ai_analysis_repository: %d transcript segment(s) saved  meeting_id=%s",
            len(inserted_ids),
            records[0].meeting_id,
        )
        return inserted_ids

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def save_signals(self, records: list[SignalRecord]) -> list[str]:
        """
        Bulk-insert all risk/opportunity keyword signals into ``Signals``.

        Returns a list of the inserted row UUIDs.

        Raises:
            RepositoryError: On any Supabase error.
        """
        if not records:
            logger.debug("ai_analysis_repository: no signals to insert.")
            return []

        payloads: list[dict[str, Any]] = [
            {
                "meeting_id":   r.meeting_id,
                "transcript_id": r.transcript_id,
                "signal_type":  r.signal_type,
                "keyword":      r.keyword,
            }
            for r in records
        ]

        logger.debug(
            "ai_analysis_repository: inserting %d signal(s)  meeting_id=%s",
            len(payloads),
            records[0].meeting_id,
        )

        try:
            response = (
                self._client.table("Signals")
                .insert(payloads)
                .execute()
            )
        except Exception as exc:
            raise RepositoryError(
                f"Failed to insert Signals for meeting_id={records[0].meeting_id}: {exc}"
            ) from exc

        if not response.data:
            raise RepositoryError(
                f"Signals insert returned no data for meeting_id={records[0].meeting_id}"
            )

        inserted_ids = [row["id"] for row in response.data]
        logger.info(
            "ai_analysis_repository: %d signal(s) saved  meeting_id=%s",
            len(inserted_ids),
            records[0].meeting_id,
        )
        return inserted_ids

    # ------------------------------------------------------------------
    # Meeting status helpers
    # ------------------------------------------------------------------

    def update_meeting_status(
        self,
        meeting_id: str,
        status: str,
        rejection_reason: str | None = None,
        duration_seconds: int | None = None,
    ) -> None:
        """
        Update the ``status`` (and optionally ``rejection_reason`` /
        ``duration_seconds``) of a Meetings row.

        Raises:
            RepositoryError: On any Supabase error.
        """
        payload: dict[str, Any] = {"status": status}
        if rejection_reason is not None:
            payload["rejection_reason"] = rejection_reason
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds

        try:
            self._client.table("Meetings").update(payload).eq("id", meeting_id).execute()
        except Exception as exc:
            raise RepositoryError(
                f"Failed to update Meetings status for meeting_id={meeting_id}: {exc}"
            ) from exc

        logger.info(
            "ai_analysis_repository: Meeting status updated  meeting_id=%s  status=%s",
            meeting_id,
            status,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_report_payload(record: MeetingReportRecord) -> dict[str, Any]:
    """
    Convert a ``MeetingReportRecord`` into a Supabase-ready dict,
    omitting keys whose value is ``None`` to avoid accidentally
    overwriting nullable columns with explicit NULLs on upsert.
    """
    raw: dict[str, Any] = {
        "meeting_id":                record.meeting_id,
        "decision_maker_identified": record.decision_maker_identified,
    }

    optional_fields = {
        "total_score":       record.total_score,
        "grade":             record.grade,
        "talk_ratio":        record.talk_ratio,
        "listening_score":   record.listening_score,
        "discovery_score":   record.discovery_score,
        "objection_score":   record.objection_score,
        "next_steps_score":  record.next_steps_score,
        "closing_score":     record.closing_score,
        "ai_summary":        record.ai_summary,
        "opening_script":    record.opening_script,
        "competitors_summary": record.competitors_summary,
    }

    for key, value in optional_fields.items():
        if value is not None:
            raw[key] = value

    return raw
