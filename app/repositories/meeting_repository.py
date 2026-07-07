"""
Module: Meeting Repository
Purpose: Handles all database operations on the Meetings table for the
         sales-rep upload flow: creating pending records, fetching by ID
         with ownership checks, and updating the file URL after S3 upload.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from supabase import Client

logger = logging.getLogger(__name__)


class MeetingRepositoryError(Exception):
    """Raised when a Meetings table operation fails."""


class MeetingRepository:
    """
    Encapsulates all Supabase interactions with the Meetings table.
    Called exclusively from the service layer — never directly from routes.
    """

    def __init__(self, supabase: Client) -> None:
        self._client = supabase

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_meeting(
        self,
        deal_id:      str,
        user_id:      str,
        source:       str        = "upload",
        meeting_date: Optional[str] = None,
        file_url:     Optional[str] = None,
    ) -> dict:
        """
        Insert a new Meetings row with status ``pending``.

        Args:
            deal_id:      FK → Deals.id
            user_id:      FK → Users.id (the uploading rep)
            source:       meeting_source_enum value (defaults to "upload")
            meeting_date: ISO-8601 timestamp; defaults to now (UTC)
            file_url:     Pre-signed or public URL for the stored media file

        Returns:
            The newly created Meetings row as a dict.

        Raises:
            MeetingRepositoryError: On any Supabase error.
        """
        payload: dict = {
            "deal_id":      deal_id,
            "user_id":      user_id,
            "source":       source,
            "status":       "pending",
            "meeting_date": meeting_date or datetime.now(timezone.utc).isoformat(),
        }
        if file_url:
            payload["file_url"] = file_url

        try:
            response = self._client.table("Meetings").insert(payload).execute()
        except Exception as exc:
            raise MeetingRepositoryError(
                f"Failed to create meeting for deal_id={deal_id}: {exc}"
            ) from exc

        if not response.data:
            raise MeetingRepositoryError(
                f"Meetings insert returned no data for deal_id={deal_id}"
            )

        row = response.data[0]
        logger.info(
            "meeting_repository: created  meeting_id=%s  deal_id=%s  user_id=%s",
            row["id"],
            deal_id,
            user_id,
        )
        return row

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_meeting_by_id(self, meeting_id: str, user_id: str) -> Optional[dict]:
        """
        Fetch a meeting row, enforcing ownership (user_id must match).

        Returns None when not found or the caller does not own the meeting.
        """
        try:
            response = (
                self._client.table("Meetings")
                .select("*")
                .eq("id", meeting_id)
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
            return response.data if response else None
        except Exception as exc:
            raise MeetingRepositoryError(
                f"Failed to fetch meeting_id={meeting_id}: {exc}"
            ) from exc

    def get_meeting_for_deal(self, deal_id: str, user_id: str) -> Optional[dict]:
        """
        Return the most-recent meeting for a deal that belongs to the given rep.
        Useful to check whether a deal already has a meeting in flight.
        """
        try:
            response = (
                self._client.table("Meetings")
                .select("id, status, meeting_date")
                .eq("deal_id", deal_id)
                .eq("user_id", user_id)
                .order("meeting_date", desc=True)
                .limit(1)
                .execute()
            )
            data = response.data or []
            return data[0] if data else None
        except Exception as exc:
            raise MeetingRepositoryError(
                f"Failed to fetch meeting for deal_id={deal_id}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update_file_url(self, meeting_id: str, file_url: str) -> None:
        """Set the ``file_url`` after the media has been persisted to S3."""
        try:
            self._client.table("Meetings").update(
                {"file_url": file_url}
            ).eq("id", meeting_id).execute()
        except Exception as exc:
            raise MeetingRepositoryError(
                f"Failed to update file_url for meeting_id={meeting_id}: {exc}"
            ) from exc
        logger.debug(
            "meeting_repository: file_url updated  meeting_id=%s", meeting_id
        )

    def get_deal_by_id(self, deal_id: str, user_id: str) -> Optional[dict]:
        """
        Verify the deal exists and belongs to the calling rep.
        Returns the Deals row or None.
        """
        try:
            response = (
                self._client.table("Deals")
                .select("id, org_id, user_id, client_name, deal_stage")
                .eq("id", deal_id)
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
            return response.data if response else None
        except Exception as exc:
            raise MeetingRepositoryError(
                f"Failed to fetch deal_id={deal_id}: {exc}"
            ) from exc

    def list_user_meetings(self, user_id: str) -> list[dict]:
        """
        Fetch all meetings for a given user (sales rep) joined with their report info if available.
        """
        try:
            response = (
                self._client.table("Meetings")
                .select("id, status, rejection_reason, duration_seconds, meeting_date, source, deal_id, Meeting_Reports(total_score, grade, talk_ratio)")
                .eq("user_id", user_id)
                .order("meeting_date", desc=True)
                .execute()
            )
            rows = response.data or []
            
            result = []
            for row in rows:
                reports = row.pop("Meeting_Reports", None)
                report = reports[0] if reports and isinstance(reports, list) else (reports or {})
                row["report"] = report
                result.append(row)
            return result
        except Exception as exc:
            raise MeetingRepositoryError(
                f"Failed to list meetings for user_id={user_id}: {exc}"
            ) from exc

    def get_meeting_report(self, meeting_id: str, user_id: str) -> Optional[dict]:
        """
        Fetch the detailed report for a meeting, checking ownership.
        """
        try:
            meeting = self.get_meeting_by_id(meeting_id, user_id)
            if not meeting:
                return None
            
            response = (
                self._client.table("Meeting_Reports")
                .select("*")
                .eq("meeting_id", meeting_id)
                .maybe_single()
                .execute()
            )
            return response.data if response else None
        except Exception as exc:
            raise MeetingRepositoryError(
                f"Failed to fetch report for meeting_id={meeting_id}: {exc}"
            ) from exc

    def get_team_comparison_stats(self, user_id: str) -> dict:
        """
        Compare the user's average score and pillar scores with their team's averages.
        """
        try:
            user_resp = (
                self._client.table("Users")
                .select("team_id")
                .eq("id", user_id)
                .maybe_single()
                .execute()
            )
            user_data = user_resp.data if user_resp else None
            if not user_data or not user_data.get("team_id"):
                user_stats = self._calculate_averages_for_meetings([user_id])
                return {
                    "has_team": False,
                    "user_averages": user_stats,
                    "team_averages": {
                        "total_score": None,
                        "discovery": None,
                        "objection": None,
                        "closing": None,
                        "listening": None,
                        "next_steps": None,
                    }
                }
            
            team_id = user_data["team_id"]

            team_users_resp = (
                self._client.table("Users")
                .select("id")
                .eq("team_id", team_id)
                .eq("is_active", True)
                .execute()
            )
            team_user_ids = [u["id"] for u in (team_users_resp.data or [])]

            user_stats = self._calculate_averages_for_meetings([user_id])
            team_stats = self._calculate_averages_for_meetings(team_user_ids)

            return {
                "has_team": True,
                "team_id": team_id,
                "user_averages": user_stats,
                "team_averages": team_stats,
            }
        except Exception as exc:
            raise MeetingRepositoryError(
                f"Failed to get team comparison for user_id={user_id}: {exc}"
            ) from exc

    def _calculate_averages_for_meetings(self, user_ids: list[str]) -> dict:
        """Helper to calculate average scores for meetings owned by a set of user IDs."""
        if not user_ids:
            return {
                "total_score": None,
                "discovery": None,
                "objection": None,
                "closing": None,
                "listening": None,
                "next_steps": None,
            }

        meetings_resp = (
            self._client.table("Meetings")
            .select("id")
            .in_("user_id", user_ids)
            .eq("status", "completed")
            .execute()
        )
        meeting_ids = [m["id"] for m in (meetings_resp.data or [])]
        if not meeting_ids:
            return {
                "total_score": None,
                "discovery": None,
                "objection": None,
                "closing": None,
                "listening": None,
                "next_steps": None,
            }

        reports_resp = (
            self._client.table("Meeting_Reports")
            .select(
                "total_score, discovery_score, objection_score, closing_score, listening_score, next_steps_score"
            )
            .in_("meeting_id", meeting_ids)
            .execute()
        )
        reports = reports_resp.data or []
        if not reports:
            return {
                "total_score": None,
                "discovery": None,
                "objection": None,
                "closing": None,
                "listening": None,
                "next_steps": None,
            }

        fields = {
            "total_score": "total_score",
            "discovery": "discovery_score",
            "objection": "objection_score",
            "closing": "closing_score",
            "listening": "listening_score",
            "next_steps": "next_steps_score",
        }
        sums = {k: 0.0 for k in fields}
        counts = {k: 0 for k in fields}

        for r in reports:
            for key, col in fields.items():
                val = r.get(col)
                if val is not None:
                    sums[key] += float(val)
                    counts[key] += 1

        return {
            key: (round(sums[key] / counts[key], 1) if counts[key] else None)
            for key in fields
        }


