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
