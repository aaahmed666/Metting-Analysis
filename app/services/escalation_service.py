"""
Module: Escalation Service
Purpose: Evaluates all escalation conditions for a team using dynamic thresholds
         loaded from Rules_And_SLAs, then persists alerts and sends emails.

Priority (highest wins per rep):
  red    → 3 consecutive losses  OR  SLA breach
           Action: save to Alerts table (dashboard alert, no email)
  orange → significant score drop  OR  repeated deal losses
           Action: save to Alerts + email rep AND manager
  yellow → no meetings in N days  OR  latest score below threshold
           Action: save to Alerts + email rep only
"""
from __future__ import annotations

import logging
from typing import Optional

from supabase import Client

from app.core.email import send_email
from app.repositories.escalation_repository import EscalationRepository
from app.repositories.manager_repository import ManagerRepository
from app.repositories.rules_sla_repository import RulesSLARepository

logger = logging.getLogger(__name__)


class EscalationService:

    def __init__(self, supabase: Client) -> None:
        self.repo         = EscalationRepository(supabase)
        self.manager_repo = ManagerRepository(supabase)
        self.rules_repo   = RulesSLARepository(supabase)

    # ─────────────────────────────────────────────────────────────
    # Public: Evaluate full team
    # ─────────────────────────────────────────────────────────────

    def evaluate_team(
        self,
        member_ids: list[str],
        org_id: str,
        manager_email: str,
    ) -> dict:
        """
        Runs all escalation checks using the org's configured thresholds.
        Creates new Alert records in DB and sends emails where applicable.
        Returns a summary: {created, skipped, details}
        """
        if not member_ids:
            return {"created": 0, "skipped": 0, "details": []}

        # ── Load org thresholds (falls back to defaults if not set) ───
        rules = self.rules_repo.get_org_rules(org_id)

        no_meetings_days    = rules["no_meetings_days"].get("days", 7)
        low_score_threshold = rules["low_score_threshold"].get("threshold", 60.0)
        score_drop          = rules["score_drop_threshold"].get("drop", 15.0)
        meetings_count      = rules["score_drop_threshold"].get("meetings_count", 3)
        min_losses          = rules["repeated_losses"].get("min_losses", 2)
        losses_days         = rules["repeated_losses"].get("days", 30)
        consecutive         = rules["consecutive_losses"].get("count", 3)
        qualified_days      = rules["sla_qualified_days"].get("days", 7)
        proposal_days       = rules["sla_proposal_days"].get("days", 14)

        # ── Member info (for email notifications) ─────────────────────
        members_info = self.manager_repo.get_team_members_info(member_ids)
        member_map: dict[str, dict] = {m["id"]: m for m in members_info}

        # ── Run all DB checks with org-specific thresholds ────────────
        no_meetings     = set(self.repo.get_reps_with_no_recent_meetings(member_ids, days=no_meetings_days))
        low_scores      = self.repo.get_reps_with_low_score(member_ids, threshold=low_score_threshold)
        score_drops     = self.repo.get_reps_with_score_drop(member_ids, drop_threshold=score_drop, meetings_count=meetings_count)
        repeated_losses = self.repo.get_reps_with_repeated_losses(member_ids, min_losses=min_losses, days=losses_days)
        consec_losses   = set(self.repo.get_reps_with_consecutive_losses(member_ids, consecutive=consecutive))
        sla_breaches    = self.repo.get_reps_with_sla_breach(member_ids, qualified_days=qualified_days, proposal_days=proposal_days)

        created = 0
        skipped = 0
        details: list[dict] = []

        for uid in member_ids:
            rep       = member_map.get(uid, {})
            rep_name  = rep.get("full_name", uid)
            rep_email = rep.get("email")

            # ── RED ───────────────────────────────────────────────────
            red_reasons: list[str] = []
            if uid in consec_losses:
                red_reasons.append(f"{consecutive} consecutive deal losses")
            if uid in sla_breaches:
                red_reasons.extend(sla_breaches[uid])

            if red_reasons:
                message = " | ".join(red_reasons)
                if not self.repo.has_active_alert(uid, "red"):
                    self.repo.save_alert(uid, "red", message)
                    created += 1
                    details.append({"user_id": uid, "level": "red", "message": message})
                    logger.warning("RED alert — %s: %s", rep_name, message)
                else:
                    skipped += 1
                continue

            # ── ORANGE ────────────────────────────────────────────────
            orange_reasons: list[str] = []
            if uid in score_drops:
                d = score_drops[uid]
                orange_reasons.append(
                    f"Score dropped {d['drop']} pts "
                    f"(recent avg: {d['avg_recent']}, previous: {d['avg_previous']})"
                )
            if uid in repeated_losses:
                cnt = repeated_losses[uid]
                orange_reasons.append(f"{cnt} deal losses in the last {losses_days} days")

            if orange_reasons:
                message = " | ".join(orange_reasons)
                if not self.repo.has_active_alert(uid, "orange"):
                    self.repo.save_alert(uid, "orange", message)
                    created += 1
                    details.append({"user_id": uid, "level": "orange", "message": message})
                    self._email_orange(rep_name, rep_email, manager_email, message)
                    logger.warning("ORANGE alert — %s: %s", rep_name, message)
                else:
                    skipped += 1
                continue

            # ── YELLOW ────────────────────────────────────────────────
            yellow_reasons: list[str] = []
            if uid in no_meetings:
                yellow_reasons.append(f"No meetings in the last {no_meetings_days} days")
            if uid in low_scores:
                yellow_reasons.append(
                    f"Latest score: {low_scores[uid]:.0f}/100 (below {low_score_threshold:.0f})"
                )

            if yellow_reasons:
                message = " | ".join(yellow_reasons)
                if not self.repo.has_active_alert(uid, "yellow"):
                    self.repo.save_alert(uid, "yellow", message)
                    created += 1
                    details.append({"user_id": uid, "level": "yellow", "message": message})
                    self._email_yellow(rep_name, rep_email, message)
                    logger.info("YELLOW alert — %s: %s", rep_name, message)
                else:
                    skipped += 1

        return {"created": created, "skipped": skipped, "details": details}

    # ─────────────────────────────────────────────────────────────
    # Public: Read & resolve
    # ─────────────────────────────────────────────────────────────

    def get_active_alerts(self, member_ids: list[str]) -> list[dict]:
        """Returns all active (unresolved) alerts for the team."""
        return self.repo.get_active_alerts(member_ids)

    def resolve_alert(self, alert_id: str) -> dict:
        """Marks an alert as resolved."""
        return self.repo.resolve_alert(alert_id)

    # ─────────────────────────────────────────────────────────────
    # Private: Email templates
    # ─────────────────────────────────────────────────────────────

    def _email_yellow(
        self, rep_name: str, rep_email: Optional[str], message: str
    ) -> None:
        if not rep_email:
            return
        send_email(
            to=rep_email,
            subject="⚠️ Performance Alert — Action Required",
            body=(
                f"Hi {rep_name},\n\n"
                f"Our system has flagged the following concern:\n\n"
                f"  {message}\n\n"
                f"Please take action to get back on track.\n\n"
                f"— Sales Intelligence System"
            ),
        )

    def _email_orange(
        self,
        rep_name: str,
        rep_email: Optional[str],
        manager_email: str,
        message: str,
    ) -> None:
        subject = f"🚨 Escalation Alert — {rep_name}"
        body = (
            f"Hi,\n\n"
            f"An escalation has been triggered for {rep_name}:\n\n"
            f"  {message}\n\n"
            f"Please review and take corrective action.\n\n"
            f"— Sales Intelligence System"
        )
        recipients = [r for r in [rep_email, manager_email] if r]
        if recipients:
            send_email(to=recipients, subject=subject, body=body)
