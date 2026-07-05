"""
Module: Escalation Repository
Purpose: All database queries needed to detect escalation conditions
         across team members, and to persist / retrieve records in the
         Alerts table.

Escalation levels
─────────────────
yellow  — no meetings in N days  OR  latest score below threshold
orange  — significant score drop  OR  repeated deal losses
red     — N consecutive losses   OR  SLA breach (Qualified / Proposal)

Tables used
───────────
Meetings        : id, user_id, status, meeting_date
Meeting_Reports : meeting_id, total_score
Deals           : id, user_id, deal_stage, stage_update_at, last_contact_date
Alerts          : id, user_id, deal_id, meeting_id, alert_level, message,
                  is_resolved, created_at
Rules_And_SLAs  : id, org_id, rule_category, conditions (jsonb), alert_level
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import Client


class EscalationRepository:

    def __init__(self, supabase: Client) -> None:
        self.client = supabase

    # ─────────────────────────────────────────────────────────────
    # Yellow — No meetings in the last N days
    # ─────────────────────────────────────────────────────────────

    def get_reps_with_no_recent_meetings(
        self, member_ids: list[str], days: int = 7
    ) -> list[str]:
        """
        Returns user IDs of reps who have NO meetings in the last `days` days.
        """
        if not member_ids:
            return []

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        response = (
            self.client.table("Meetings")
            .select("user_id")
            .in_("user_id", member_ids)
            .gte("meeting_date", cutoff)
            .execute()
        )

        active_ids = {row["user_id"] for row in (response.data or [])}
        return [uid for uid in member_ids if uid not in active_ids]

    # ─────────────────────────────────────────────────────────────
    # Yellow — Latest completed meeting score below threshold
    # ─────────────────────────────────────────────────────────────

    def get_reps_with_low_score(
        self, member_ids: list[str], threshold: float = 60.0
    ) -> dict[str, float]:
        """
        Returns {user_id: score} for reps whose latest completed meeting
        total_score is below `threshold`.
        """
        if not member_ids:
            return {}

        meetings_resp = (
            self.client.table("Meetings")
            .select("id, user_id, meeting_date")
            .in_("user_id", member_ids)
            .eq("status", "completed")
            .order("meeting_date", desc=True)
            .execute()
        )
        meetings = meetings_resp.data or []
        if not meetings:
            return {}

        # Keep only the latest meeting per user
        latest: dict[str, str] = {}
        for m in meetings:
            if m["user_id"] not in latest:
                latest[m["user_id"]] = m["id"]

        reports_resp = (
            self.client.table("Meeting_Reports")
            .select("meeting_id, total_score")
            .in_("meeting_id", list(latest.values()))
            .execute()
        )
        score_map: dict[str, float] = {
            r["meeting_id"]: float(r["total_score"])
            for r in (reports_resp.data or [])
            if r.get("total_score") is not None
        }

        return {
            uid: score_map[mid]
            for uid, mid in latest.items()
            if mid in score_map and score_map[mid] < threshold
        }

    # ─────────────────────────────────────────────────────────────
    # Orange — Significant score drop (avg last N vs avg prev N)
    # ─────────────────────────────────────────────────────────────

    def get_reps_with_score_drop(
        self,
        member_ids: list[str],
        drop_threshold: float = 15.0,
        meetings_count: int = 3,
    ) -> dict[str, dict]:
        """
        Returns reps whose average score dropped by >= `drop_threshold` points
        comparing their last `meetings_count` completed meetings vs the same
        number before that.
        Result: {user_id: {avg_recent, avg_previous, drop}}
        """
        if not member_ids:
            return {}

        window = meetings_count * 2  # need 2x to compare

        meetings_resp = (
            self.client.table("Meetings")
            .select("id, user_id, meeting_date")
            .in_("user_id", member_ids)
            .eq("status", "completed")
            .order("meeting_date", desc=True)
            .execute()
        )
        meetings = meetings_resp.data or []

        by_user: dict[str, list[str]] = {}
        for m in meetings:
            by_user.setdefault(m["user_id"], []).append(m["id"])

        candidates = {
            uid: ids[:window]
            for uid, ids in by_user.items()
            if len(ids) >= window
        }
        if not candidates:
            return {}

        all_ids = [mid for ids in candidates.values() for mid in ids]
        reports_resp = (
            self.client.table("Meeting_Reports")
            .select("meeting_id, total_score")
            .in_("meeting_id", all_ids)
            .execute()
        )
        score_map: dict[str, float] = {
            r["meeting_id"]: float(r["total_score"])
            for r in (reports_resp.data or [])
            if r.get("total_score") is not None
        }

        result: dict[str, dict] = {}
        for uid, ids in candidates.items():
            last_n = [score_map[m] for m in ids[:meetings_count] if m in score_map]
            prev_n = [score_map[m] for m in ids[meetings_count:window] if m in score_map]

            if len(last_n) < 2 or len(prev_n) < 2:
                continue

            avg_last = sum(last_n) / len(last_n)
            avg_prev = sum(prev_n) / len(prev_n)
            drop     = avg_prev - avg_last

            if drop >= drop_threshold:
                result[uid] = {
                    "avg_recent":   round(avg_last, 1),
                    "avg_previous": round(avg_prev, 1),
                    "drop":         round(drop, 1),
                }

        return result

    # ─────────────────────────────────────────────────────────────
    # Orange — Repeated deal losses
    # ─────────────────────────────────────────────────────────────

    def get_reps_with_repeated_losses(
        self, member_ids: list[str], min_losses: int = 2, days: int = 30
    ) -> dict[str, int]:
        """
        Returns {user_id: loss_count} for reps with >= `min_losses`
        "Closed Lost" deals in the last `days` days.
        """
        if not member_ids:
            return {}

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        response = (
            self.client.table("Deals")
            .select("user_id")
            .in_("user_id", member_ids)
            .eq("deal_stage", "lost")
            .gte("stage_updated_at", cutoff)
            .execute()
        )

        counts: dict[str, int] = {}
        for row in (response.data or []):
            uid = row["user_id"]
            counts[uid] = counts.get(uid, 0) + 1

        return {uid: cnt for uid, cnt in counts.items() if cnt >= min_losses}

    # ─────────────────────────────────────────────────────────────
    # Red — Consecutive deal losses
    # ─────────────────────────────────────────────────────────────

    def get_reps_with_consecutive_losses(
        self, member_ids: list[str], consecutive: int = 3
    ) -> list[str]:
        """
        Returns user IDs of reps whose last `consecutive` closed deals
        are ALL "Closed Lost".
        """
        if not member_ids:
            return []

        response = (
            self.client.table("Deals")
            .select("user_id, deal_stage, stage_updated_at")
            .in_("user_id", member_ids)
            .in_("deal_stage", ["lost", "won"])
            .order("stage_updated_at", desc=True)
            .execute()
        )

        by_user: dict[str, list[str]] = {}
        for d in (response.data or []):
            by_user.setdefault(d["user_id"], []).append(d["deal_stage"])

        return [
            uid
            for uid, stages in by_user.items()
            if len(stages) >= consecutive
            and all(s == "lost" for s in stages[:consecutive])
        ]

    # ─────────────────────────────────────────────────────────────
    # Red — SLA breach
    # ─────────────────────────────────────────────────────────────

    def get_reps_with_sla_breach(
        self,
        member_ids: list[str],
        qualified_days: int = 7,
        proposal_days: int = 14,
    ) -> dict[str, list[str]]:
        """
        Returns {user_id: [breach_reasons]} for reps with active SLA violations.
        - Qualified deals not updated for `qualified_days`+ days
        - Proposal deals with no contact for `proposal_days`+ days
        """
        if not member_ids:
            return {}

        qualified_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=qualified_days)
        ).isoformat()
        proposal_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=proposal_days)
        ).isoformat()

        q_resp = (
            self.client.table("Deals")
            .select("user_id")
            .in_("user_id", member_ids)
            .eq("deal_stage", "qualified")
            .lt("stage_updated_at", qualified_cutoff)
            .execute()
        )

        p_resp = (
            self.client.table("Deals")
            .select("user_id")
            .in_("user_id", member_ids)
            .eq("deal_stage", "proposal")
            .lt("last_contact_date", proposal_cutoff)
            .execute()
        )

        result: dict[str, list[str]] = {}

        for row in (q_resp.data or []):
            uid = row["user_id"]
            reasons = result.setdefault(uid, [])
            msg = f"Qualified deal not updated for {qualified_days}+ days"
            if msg not in reasons:
                reasons.append(msg)

        for row in (p_resp.data or []):
            uid = row["user_id"]
            reasons = result.setdefault(uid, [])
            msg = f"Proposal with no contact for {proposal_days}+ days"
            if msg not in reasons:
                reasons.append(msg)

        return result

    # ─────────────────────────────────────────────────────────────
    # Alerts table — CRUD
    # ─────────────────────────────────────────────────────────────

    def has_active_alert(self, user_id: str, alert_level: str) -> bool:
        """
        Returns True if the user already has an unresolved alert at this level.
        Prevents duplicate alert records on repeated evaluations.
        """
        response = (
            self.client.table("Alerts")
            .select("id")
            .eq("user_id", user_id)
            .eq("alert_level", alert_level)
            .eq("is_resolved", False)
            .maybe_single()
            .execute()
        )
        return bool(response and response.data)

    def save_alert(
        self,
        user_id: str,
        alert_level: str,
        message: str,
        deal_id: Optional[str] = None,
        meeting_id: Optional[str] = None,
    ) -> dict:
        """Inserts a new alert record into the Alerts table."""
        payload: dict = {
            "user_id":     user_id,
            "alert_level": alert_level,
            "message":     message,
            "is_resolved": False,
        }
        if deal_id:
            payload["deal_id"] = deal_id
        if meeting_id:
            payload["meeting_id"] = meeting_id

        response = (
            self.client.table("Alerts")
            .insert(payload)
            .execute()
        )
        return response.data[0] if response.data else {}

    def get_active_alerts(self, member_ids: list[str]) -> list[dict]:
        """Returns all unresolved alerts for the given member IDs."""
        if not member_ids:
            return []

        response = (
            self.client.table("Alerts")
            .select("*, Users(full_name, email)")
            .in_("user_id", member_ids)
            .eq("is_resolved", False)
            .order("created_at", desc=True)
            .execute()
        )
        return response.data or []

    def resolve_alert(self, alert_id: str) -> dict:
        """Marks an alert as resolved."""
        response = (
            self.client.table("Alerts")
            .update({"is_resolved": True})
            .eq("id", alert_id)
            .execute()
        )
        return response.data[0] if response.data else {}
