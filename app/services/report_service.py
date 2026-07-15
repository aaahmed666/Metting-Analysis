from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from supabase import Client

from app.repositories.admin_repository import AdminRepository
from prompts.performance_report import PERFORMANCE_REPORT_SYSTEM, build_report_prompt
from services.audio.gemini_client import GeminiClient, LLMClientError

logger = logging.getLogger(__name__)

_gemini_client: GeminiClient | None = None


def _get_gemini_client() -> GeminiClient:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = GeminiClient()
    return _gemini_client


class ReportService:
    def __init__(self, supabase: Client) -> None:
        self.repo = AdminRepository(supabase)

    def generate_employee_report(self, user_id: str, org_id: str) -> dict[str, Any]:
        logger.info(
            "ReportService: generating report for user=%s in org=%s",
            user_id, org_id,
        )

        profile      = self.repo.get_user_profile(user_id, org_id)
        team_info    = self.repo.get_team_info(profile.get("team_id"), org_id)
        kpi_bundle   = self.repo.calculate_user_kpis(user_id)
        recent       = self.repo.get_recent_completed_meetings(user_id, limit=5)

        meeting_stats = kpi_bundle["meeting_stats"]
        kpis          = kpi_bundle["kpis"]

        data_payload: dict[str, Any] = {
            "profile":         profile,
            "team_info":       team_info,
            "meeting_stats":   meeting_stats,
            "kpis":            kpis,
            "recent_meetings": recent,
        }

        user_message = build_report_prompt(data_payload)

        logger.debug(
            "ReportService: prompt built user=%s chars=%d",
            user_id, len(user_message),
        )

        try:
            raw_ai: dict = _get_gemini_client().complete(
                system=PERFORMANCE_REPORT_SYSTEM,
                user=user_message,
                temperature=0.3,
            )
        except LLMClientError as exc:
            logger.error("ReportService: Gemini failed for user=%s: %s", user_id, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI report generation failed. Please try again in a moment.",
            ) from exc

        ai_report = _validate_ai_response(raw_ai)

        logger.info(
            "ReportService: report generated user=%s level=%s",
            user_id, ai_report.get("performance_level", "unknown"),
        )

        return {
            "user": {
                "user_id":    profile["id"],
                "full_name":  profile["full_name"],
                "email":      profile["email"],
                "role":       profile["role"],
                "is_active":  profile["is_active"],
                "member_since": str(profile.get("created_at", ""))[:10],
                "team_name":    team_info.get("team_name"),
                "manager_name": team_info.get("manager_name"),
            },
            "meeting_stats": meeting_stats,
            "kpis":          kpis,
            "ai_report":     ai_report,
            "generated_at":  datetime.now(timezone.utc).isoformat(),
        }

    def get_user_summary(self, user_id: str, org_id: str) -> dict[str, Any]:
        profile    = self.repo.get_user_profile(user_id, org_id)
        team_info  = self.repo.get_team_info(profile.get("team_id"), org_id)
        kpi_bundle = self.repo.calculate_user_kpis(user_id)
        recent     = self.repo.get_recent_completed_meetings(user_id, limit=5)

        return {
            "user": {
                "user_id":      profile["id"],
                "full_name":    profile["full_name"],
                "email":        profile["email"],
                "role":         profile["role"],
                "is_active":    profile["is_active"],
                "member_since": str(profile.get("created_at", ""))[:10],
                "team_name":    team_info.get("team_name"),
                "manager_name": team_info.get("manager_name"),
            },
            "meeting_stats":    kpi_bundle["meeting_stats"],
            "kpis":             kpi_bundle["kpis"],
            "recent_meetings":  recent,
        }


def _validate_ai_response(raw: dict) -> dict:
    defaults: dict[str, Any] = {
        "executive_summary":          "Report data unavailable.",
        "performance_level":          "N/A",
        "strengths":                  [],
        "areas_for_improvement":      [],
        "skill_analysis":             {},
        "meeting_activity_analysis":  "N/A",
        "talk_ratio_analysis":        "N/A",
        "grade_trend_analysis":       "N/A",
        "recommendations":            [],
        "overall_assessment":         "N/A",
    }

    result: dict[str, Any] = {}
    for key, default in defaults.items():
        value = raw.get(key)
        if value is None:
            logger.warning("ReportService: AI response missing key '%s' — using default.", key)
            result[key] = default
        else:
            result[key] = value

    return result
