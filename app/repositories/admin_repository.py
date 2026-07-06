from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, status
from supabase import Client

logger = logging.getLogger(__name__)


class AdminRepository:
    def __init__(self, supabase: Client) -> None:
        self.client = supabase

    def get_org_id_for_admin(self, admin_user_id: str) -> str:
        response = (
            self.client.table("Users")
            .select("org_id")
            .eq("id", admin_user_id)
            .maybe_single()
            .execute()
        )
        data = response.data if response else None
        if not data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Admin user not found.",
            )
        return data["org_id"]

    def list_org_users(self, org_id: str) -> list[dict]:
        response = (
            self.client.table("Users")
            .select("id, full_name, email, role, is_active, created_at, team_id, team_info:Teams!Users_team_id_fkey(name)")
            .eq("org_id", org_id)
            .eq("is_active", True)
            .order("created_at", desc=False)
            .execute()
        )
        rows = response.data or []

        result = []
        for row in rows:
            team_obj = row.pop("team_info", None) or {}
            row["team_name"] = team_obj.get("name")
            result.append(row)

        return result

    def get_user_profile(self, user_id: str, org_id: str) -> dict:
        response = (
            self.client.table("Users")
            .select("id, full_name, email, role, is_active, created_at, team_id, org_id")
            .eq("id", user_id)
            .eq("org_id", org_id)
            .maybe_single()
            .execute()
        )
        data = response.data if response else None
        if not data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found in your organisation.",
            )
        return data

    def get_team_info(self, team_id: Optional[str], org_id: str) -> dict:
        if not team_id:
            return {"team_name": None, "manager_name": None, "org_name": None}

        team_resp = (
            self.client.table("Teams")
            .select("name, manager_id")
            .eq("id", team_id)
            .eq("org_id", org_id)
            .maybe_single()
            .execute()
        )
        team_data = team_resp.data if team_resp else None
        if not team_data:
            return {"team_name": None, "manager_name": None, "org_name": None}

        team_name  = team_data.get("name")
        manager_id = team_data.get("manager_id")

        manager_name = None
        if manager_id:
            mgr_resp = (
                self.client.table("Users")
                .select("full_name")
                .eq("id", manager_id)
                .maybe_single()
                .execute()
            )
            if mgr_resp and mgr_resp.data:
                manager_name = mgr_resp.data.get("full_name")

        return {
            "team_name":    team_name,
            "manager_name": manager_name,
            "org_name":     None,
        }

    def get_all_meetings(self, user_id: str) -> list[dict]:
        response = (
            self.client.table("Meetings")
            .select("id, status, rejection_reason, duration_seconds, meeting_date, source, deal_id")
            .eq("user_id", user_id)
            .order("meeting_date", desc=True)
            .execute()
        )
        return response.data or []

    def get_completed_meeting_reports(self, meeting_ids: list[str]) -> list[dict]:
        if not meeting_ids:
            return []

        response = (
            self.client.table("Meeting_Reports")
            .select(
                "meeting_id, total_score, grade, talk_ratio, "
                "discovery_score, objection_score, next_steps_score, "
                "closing_score, listening_score"
            )
            .in_("meeting_id", meeting_ids)
            .execute()
        )
        return response.data or []

    def calculate_user_kpis(self, user_id: str) -> dict:
        all_meetings = self.get_all_meetings(user_id)
        total = len(all_meetings)

        by_status: dict[str, int] = {}
        completed_ids: list[str] = []

        for m in all_meetings:
            s = m.get("status", "unknown")
            by_status[s] = by_status.get(s, 0) + 1
            if s == "completed":
                completed_ids.append(m["id"])

        completed_count = len(completed_ids)
        completion_rate = round(completed_count / total * 100, 1) if total else None
        rejection_rate  = round(by_status.get("rejected", 0) / total * 100, 1) if total else None

        meeting_stats = {
            "total_meetings":  total,
            "by_status":       by_status,
            "completion_rate": completion_rate,
            "rejection_rate":  rejection_rate,
        }

        if not completed_ids:
            return {
                "meeting_stats":    meeting_stats,
                "kpis": {
                    "avg_score":         None,
                    "avg_talk_ratio":    None,
                    "grade_distribution": {},
                    "avg_scores": {
                        "discovery":  None,
                        "objection":  None,
                        "closing":    None,
                        "listening":  None,
                        "next_steps": None,
                    },
                },
            }

        reports = self.get_completed_meeting_reports(completed_ids)

        score_fields = [
            "total_score", "talk_ratio",
            "discovery_score", "objection_score",
            "next_steps_score", "closing_score", "listening_score",
        ]
        sums:   dict[str, float] = {f: 0.0 for f in score_fields}
        counts: dict[str, int]   = {f: 0   for f in score_fields}
        grade_dist: dict[str, int] = {}

        for r in reports:
            for field in score_fields:
                val = r.get(field)
                if val is not None:
                    sums[field]   += float(val)
                    counts[field] += 1

            grade = r.get("grade")
            if grade:
                grade_dist[grade] = grade_dist.get(grade, 0) + 1

        def safe_avg(field: str) -> Optional[float]:
            return round(sums[field] / counts[field], 1) if counts[field] else None

        return {
            "meeting_stats": meeting_stats,
            "kpis": {
                "avg_score":          safe_avg("total_score"),
                "avg_talk_ratio":     safe_avg("talk_ratio"),
                "grade_distribution": grade_dist,
                "avg_scores": {
                    "discovery":  safe_avg("discovery_score"),
                    "objection":  safe_avg("objection_score"),
                    "closing":    safe_avg("closing_score"),
                    "listening":  safe_avg("listening_score"),
                    "next_steps": safe_avg("next_steps_score"),
                },
            },
        }

    def get_recent_completed_meetings(self, user_id: str, limit: int = 5) -> list[dict]:
        meetings_resp = (
            self.client.table("Meetings")
            .select("id, meeting_date, source")
            .eq("user_id", user_id)
            .eq("status", "completed")
            .order("meeting_date", desc=True)
            .limit(limit)
            .execute()
        )
        meetings = meetings_resp.data or []
        if not meetings:
            return []

        meeting_ids = [m["id"] for m in meetings]

        reports_resp = (
            self.client.table("Meeting_Reports")
            .select("meeting_id, total_score, grade, talk_ratio")
            .in_("meeting_id", meeting_ids)
            .execute()
        )
        reports_map = {r["meeting_id"]: r for r in (reports_resp.data or [])}

        result = []
        for m in meetings:
            report = reports_map.get(m["id"], {})
            result.append({
                "meeting_date": m.get("meeting_date"),
                "source":       m.get("source"),
                "total_score":  report.get("total_score"),
                "grade":        report.get("grade"),
                "talk_ratio":   report.get("talk_ratio"),
            })

        return result
