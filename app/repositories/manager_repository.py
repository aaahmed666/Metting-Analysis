"""
Module: Manager Repository
Purpose: Handles all database queries for the manager dashboard.
         Queries meetings, meeting reports, deals, and analytics for the manager's team.
"""
from __future__ import annotations

from typing import Optional
from supabase import Client


class ManagerRepository:
    """
    Handles all direct database interactions needed for the manager dashboard.
    Queries Meetings, Meeting_Reports, Deals, Users and other tables.
    """

    def __init__(self, supabase: Client) -> None:
        self.client = supabase

    # ─────────────────────────────────────────────────────────────
    # Shared helpers 
    # ─────────────────────────────────────────────────────────────

    def get_caller_org(self, user_id: str) -> str:
        """
        Returns the org_id of a user from the Users table.
        Raises ValueError if the user is not found.

        """
        from fastapi import HTTPException, status as http_status
        response = (
            self.client.table("Users")
            .select("org_id")
            .eq("id", user_id)
            .maybe_single()
            .execute()
        )
        data = response.data if response else None
        if not data:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Caller not found in Users table.",
            )
        return data["org_id"]

    def resolve_member_ids(self, user_id: str, role: str, org_id: str) -> list[str]:
        """
        Resolves which user IDs the caller is allowed to see meetings for.

        - admin  → all active users in the org (all teams)
        - manager → only their own team members

        """
        if role == "admin":
            response = (
                self.client.table("Users")
                .select("id")
                .eq("org_id", org_id)
                .eq("is_active", True)
                .execute()
            )
            return [row["id"] for row in (response.data or [])]
        else:
            return self.get_all_member_ids_for_manager(user_id, org_id)

    # ─────────────────────────────────────────────────────────────
    # Team member helpers
    # ─────────────────────────────────────────────────────────────

    def get_team_member_ids(self, team_id: str) -> list[str]:
        """
        Returns a list of user IDs for all active members of a given team.
        """
        response = (
            self.client.table("Users")
            .select("id")
            .eq("team_id", team_id)
            .eq("is_active", True)
            .execute()
        )
        return [row["id"] for row in (response.data or [])]

    def get_manager_team_ids(self, manager_id: str, org_id: str) -> list[str]:
        """
        Returns a list of team IDs that this manager manages within the org.
        """
        response = (
            self.client.table("Teams")
            .select("id")
            .eq("manager_id", manager_id)
            .eq("org_id", org_id)
            .execute()
        )
        return [row["id"] for row in (response.data or [])]

    def get_all_member_ids_for_manager(self, manager_id: str, org_id: str) -> list[str]:
        """
        Returns a flat list of all user IDs belonging to all teams managed by this manager.
        """
        team_ids = self.get_manager_team_ids(manager_id, org_id)
        all_member_ids: list[str] = []
        for team_id in team_ids:
            all_member_ids.extend(self.get_team_member_ids(team_id))
        return all_member_ids

    # ─────────────────────────────────────────────────────────────
    # Meetings
    # ─────────────────────────────────────────────────────────────

    def get_meetings_for_members(
        self,
        member_ids: list[str],
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """
        Returns all meetings for a given list of user IDs (team members).
        Optionally filtered by meeting status (e.g. 'done', 'pending', 'processing').
        Joins with Users table to include rep name and email.
        """
        if not member_ids:
            return []

        query = (
            self.client.table("Meetings")
            .select(
                "id, user_id, deal_id, source, status, rejection_reason, "
                "duration_seconds, meeting_date, file_url, "
                "Users(full_name, email, role)"
            )
            .in_("user_id", member_ids)
            .order("meeting_date", desc=True)
            .range(offset, offset + limit - 1)
        )

        if status:
            query = query.eq("status", status)

        response = query.execute()
        return response.data or []

    def get_meeting_with_report(self, meeting_id: str, member_ids: list[str]) -> Optional[dict]:
        """
        Returns a single meeting along with its full AI report from Meeting_Reports.
        Ensures the meeting belongs to one of the manager's team members (security check).
        """
        meeting_resp = (
            self.client.table("Meetings")
            .select(
                "id, user_id, deal_id, source, status, rejection_reason, "
                "duration_seconds, meeting_date, file_url, "
                "Users(full_name, email)"
            )
            .eq("id", meeting_id)
            .in_("user_id", member_ids)
            .maybe_single()
            .execute()
        )

        data = meeting_resp.data if meeting_resp else None
        if not data:
            return None

        meeting = data

        # Now fetch the report for this meeting
        report_resp = (
            self.client.table("Meeting_Reports")
            .select("*")
            .eq("meeting_id", meeting_id)
            .execute()
        )
        meeting["report"] = report_resp.data[0] if report_resp.data else None

        return meeting

    def count_meetings_for_members(
        self, member_ids: list[str], status: Optional[str] = None
    ) -> int:
        """
        Returns the total count of meetings for a list of member IDs.
        Used for pagination metadata.
        """
        if not member_ids:
            return 0

        query = (
            self.client.table("Meetings")
            .select("id", count="exact")
            .in_("user_id", member_ids)
        )
        if status:
            query = query.eq("status", status)

        response = query.execute()
        return response.count or 0

    # ─────────────────────────────────────────────────────────────
    # KPIs
    # ─────────────────────────────────────────────────────────────

    def get_team_kpis(self, member_ids: list[str]) -> dict:
        """
        Calculates team-wide KPI metrics from Meetings and Meeting_Reports.

        Returns:
            - total_meetings
            - completed_meetings (status=done)
            - meetings_by_status
            - avg_score
            - grade_distribution A/B/C/D
            - avg_scores : discovery، objection 
            - avg_talk_ratio 
        """
        if not member_ids:
            return self._empty_kpis()

        # get all meetings with status
        meetings_resp = (
            self.client.table("Meetings")
            .select("id, status, user_id")
            .in_("user_id", member_ids)
            .execute()
        )
        meetings = meetings_resp.data or []
        total_meetings = len(meetings)

        # meetings by status 
        meetings_by_status: dict[str, int] = {}
        completed_ids: list[str] = []
        for m in meetings:
            s = m.get("status", "unknown")
            meetings_by_status[s] = meetings_by_status.get(s, 0) + 1
            if s == "completed":
                completed_ids.append(m["id"])

        completed_meetings = len(completed_ids)

        # report for completed meetings
        if not completed_ids:
            return {
                "total_meetings": total_meetings,
                "completed_meetings": 0,
                "meetings_by_status": meetings_by_status,
                "avg_score": None,
                "grade_distribution": {},
                "avg_scores": {},
                "avg_talk_ratio": None,
            }

        reports_resp = (
            self.client.table("Meeting_Reports")
            .select(
                "meeting_id, total_score, grade, talk_ratio, "
                "discovery_score, objection_score, next_steps_score, "
                "closing_score, listening_score"
            )
            .in_("meeting_id", completed_ids)
            .execute()
        )
        reports = reports_resp.data or []

        if not reports:
            return {
                "total_meetings": total_meetings,
                "completed_meetings": completed_meetings,
                "meetings_by_status": meetings_by_status,
                "avg_score": None,
                "grade_distribution": {},
                "avg_scores": {},
                "avg_talk_ratio": None,
            }

        # get average scores and grade distribution
        score_fields = [
            "total_score", "talk_ratio", "discovery_score",
            "objection_score", "next_steps_score",
            "closing_score", "listening_score",
        ]
        sums: dict[str, float] = {f: 0.0 for f in score_fields}
        counts: dict[str, int] = {f: 0 for f in score_fields}
        grade_distribution: dict[str, int] = {}

        for r in reports:
            for field in score_fields:
                val = r.get(field)
                if val is not None:
                    sums[field] += float(val)
                    counts[field] += 1

            grade = r.get("grade")
            if grade:
                grade_distribution[grade] = grade_distribution.get(grade, 0) + 1

        def safe_avg(field: str) -> Optional[float]:
            return round(sums[field] / counts[field], 1) if counts[field] else None

        return {
            "total_meetings": total_meetings,
            "completed_meetings": completed_meetings,
            "meetings_by_status": meetings_by_status,
            "avg_score": safe_avg("total_score"),
            "grade_distribution": grade_distribution,
            "avg_talk_ratio": safe_avg("talk_ratio"),
            "avg_scores": {
                "discovery":  safe_avg("discovery_score"),
                "objection":  safe_avg("objection_score"),
                "next_steps": safe_avg("next_steps_score"),
                "closing":    safe_avg("closing_score"),
                "listening":  safe_avg("listening_score"),
            },
        }

    def get_team_members_info(self, member_ids: list[str]) -> list[dict]:
        """
        Returns basic profile info for a list of user IDs.
        Used by the leaderboard and KPIs to attach names to stats.
        """
        if not member_ids:
            return []
        response = (
            self.client.table("Users")
            .select("id, full_name, email, role")
            .in_("id", member_ids)
            .eq("is_active", True)
            .execute()
        )
        return response.data or []

    @staticmethod
    def _empty_kpis() -> dict:
        return {
            "total_meetings": 0,
            "completed_meetings": 0,
            "meetings_by_status": {},
            "avg_score": None,
            "grade_distribution": {},
            "avg_scores": {},
            "avg_talk_ratio": None,
        }

    def get_roi_stats(self, member_ids: list[str]) -> dict:
        """
        Calculate ROI dashboard statistics for the given team member IDs.
        """
        if not member_ids:
            return {
                "closed_won_deals": 0,
                "closed_won_value": 0.0,
                "closed_lost_deals": 0,
                "closed_lost_value": 0.0,
                "win_rate": 0.0,
                "total_deals_processed": 0,
                "total_completed_meetings": 0,
                "hours_saved": 0.0,
                "estimated_savings_usd": 0.0,
            }

        try:
            deals_resp = (
                self.client.table("Deals")
                .select("deal_stage, value")
                .in_("user_id", member_ids)
                .execute()
            )
            deals = deals_resp.data or []

            closed_won_count = 0
            closed_won_val = 0.0
            closed_lost_count = 0
            closed_lost_val = 0.0

            for d in deals:
                stage = (d.get("deal_stage") or "").lower().strip()
                val = float(d.get("value") or 0.0)
                if stage == "closed_won":
                    closed_won_count += 1
                    closed_won_val += val
                elif stage == "closed_lost":
                    closed_lost_count += 1
                    closed_lost_val += val

            total_closed = closed_won_count + closed_lost_count
            win_rate = round((closed_won_count / total_closed * 100), 1) if total_closed else 0.0

            meetings_count_resp = (
                self.client.table("Meetings")
                .select("id", count="exact")
                .in_("user_id", member_ids)
                .eq("status", "completed")
                .execute()
            )
            completed_meetings = meetings_count_resp.count or 0

            # Assume 30 minutes (0.5 hours) saved per meeting review, and $40/hour supervisor rate
            hours_saved = completed_meetings * 0.5
            savings_usd = hours_saved * 40.0

            return {
                "closed_won_deals": closed_won_count,
                "closed_won_value": closed_won_val,
                "closed_lost_deals": closed_lost_count,
                "closed_lost_value": closed_lost_val,
                "win_rate": win_rate,
                "total_deals_processed": len(deals),
                "total_completed_meetings": completed_meetings,
                "hours_saved": hours_saved,
                "estimated_savings_usd": savings_usd,
            }
        except Exception as exc:
            raise Exception(f"Failed to calculate ROI stats: {exc}")

    def get_meetings_export_data(self, member_ids: list[str]) -> list[dict]:
        """
        Fetch all meetings and reports data for exporting to CSV.
        """
        if not member_ids:
            return []

        try:
            meetings_resp = (
                self.client.table("Meetings")
                .select("id, meeting_date, status, source, duration_seconds, user_id, Users(full_name, email)")
                .in_("user_id", member_ids)
                .order("meeting_date", desc=True)
                .execute()
            )
            meetings = meetings_resp.data or []
            if not meetings:
                return []

            meeting_ids = [m["id"] for m in meetings]

            reports_resp = (
                self.client.table("Meeting_Reports")
                .select("meeting_id, total_score, grade, talk_ratio, discovery_score, objection_score, closing_score, listening_score, next_steps_score")
                .in_("meeting_id", meeting_ids)
                .execute()
            )
            reports_map = {r["meeting_id"]: r for r in (reports_resp.data or [])}

            export_rows = []
            for m in meetings:
                user_info = m.pop("Users", None) or {}
                # Handle single object unpack from Supabase relation
                if isinstance(user_info, list) and user_info:
                    user_info = user_info[0]
                rep_name = user_info.get("full_name", "N/A")
                rep_email = user_info.get("email", "N/A")

                report = reports_map.get(m["id"], {})

                row = {
                    "meeting_id": m["id"],
                    "meeting_date": m.get("meeting_date")[:16] if m.get("meeting_date") else "N/A",
                    "status": m.get("status"),
                    "source": m.get("source"),
                    "duration_minutes": round(m.get("duration_seconds", 0) / 60, 1) if m.get("duration_seconds") else 0.0,
                    "representative_name": rep_name,
                    "representative_email": rep_email,
                    "total_score": report.get("total_score", "N/A"),
                    "grade": report.get("grade", "N/A"),
                    "talk_ratio_pct": f"{report.get('talk_ratio', 0)}%" if report.get("talk_ratio") is not None else "N/A",
                    "discovery_score": report.get("discovery_score", "N/A"),
                    "objection_score": report.get("objection_score", "N/A"),
                    "closing_score": report.get("closing_score", "N/A"),
                    "listening_score": report.get("listening_score", "N/A"),
                    "next_steps_score": report.get("next_steps_score", "N/A"),
                }
                export_rows.append(row)

            return export_rows
        except Exception as exc:
            raise Exception(f"Failed to fetch meetings export data: {exc}")

