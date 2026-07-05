"""
Module: Rules & SLA Repository
Purpose: Reads and writes escalation rules and SLA thresholds from the
         Rules_And_SLAs table.

Table schema
────────────
Rules_And_SLAs:
  id            UUID    PK
  org_id        UUID    FK → Organizations
  rule_category TEXT    e.g. "sla_qualified_days", "low_score_threshold"
  conditions    JSONB   e.g. {"days": 7} or {"threshold": 60}
  alert_level   TEXT    yellow | orange | red

Rule categories
───────────────
Category                  | Level  | conditions key
─────────────────────────────────────────────────────
no_meetings_days          | yellow | {"days": 7}
low_score_threshold       | yellow | {"threshold": 60}
score_drop_threshold      | orange | {"drop": 15, "meetings_count": 3}
repeated_losses           | orange | {"min_losses": 2, "days": 30}
consecutive_losses        | red    | {"count": 3}
sla_qualified_days        | red    | {"days": 7}
sla_proposal_days         | red    | {"days": 14}
"""
from __future__ import annotations

from supabase import Client

# ── Default thresholds  ─────────────
DEFAULTS: dict[str, dict] = {
    "no_meetings_days":     {"days": 7},
    "low_score_threshold":  {"threshold": 60},
    "score_drop_threshold": {"drop": 15, "meetings_count": 3},
    "repeated_losses":      {"min_losses": 2, "days": 30},
    "consecutive_losses":   {"count": 3},
    "sla_qualified_days":   {"days": 7},
    "sla_proposal_days":    {"days": 14},
}

# ── alert_level for each category ───────────────────────────────────────────
CATEGORY_LEVELS: dict[str, str] = {
    "no_meetings_days":     "yellow",
    "low_score_threshold":  "yellow",
    "score_drop_threshold": "orange",
    "repeated_losses":      "orange",
    "consecutive_losses":   "red",
    "sla_qualified_days":   "red",
    "sla_proposal_days":    "red",
}


class RulesSLARepository:

    def __init__(self, supabase: Client) -> None:
        self.client = supabase

    # ─────────────────────────────────────────────────────────────
    # Read
    # ─────────────────────────────────────────────────────────────

    def get_org_rules(self, org_id: str) -> dict[str, dict]:
        """
        Returns all rules for the org as {rule_category: conditions}.
        Missing categories are filled with default values.
        """
        response = (
            self.client.table("Rules_And_SLAs")
            .select("rule_category, conditions")
            .eq("org_id", org_id)
            .execute()
        )

        # Start from defaults, override with whatever the org has saved
        rules = dict(DEFAULTS)
        for row in (response.data or []):
            category = row["rule_category"]
            if category in rules and row.get("conditions"):
                rules[category] = row["conditions"]

        return rules

    def get_all_rules_raw(self, org_id: str) -> list[dict]:
        """
        Returns the raw rows from Rules_And_SLAs for the org.
        Used by the settings API to show current config to the manager.
        """
        response = (
            self.client.table("Rules_And_SLAs")
            .select("*")
            .eq("org_id", org_id)
            .execute()
        )
        return response.data or []

    # ─────────────────────────────────────────────────────────────
    # Write
    # ─────────────────────────────────────────────────────────────

    def upsert_rule(
        self, org_id: str, rule_category: str, conditions: dict
    ) -> dict:
        """
        Creates or updates a rule for the org.
        Uses upsert on (org_id, rule_category) uniqueness.
        """
        alert_level = CATEGORY_LEVELS.get(rule_category, "yellow")

        response = (
            self.client.table("Rules_And_SLAs")
            .upsert(
                {
                    "org_id":        org_id,
                    "rule_category": rule_category,
                    "conditions":    conditions,
                    "alert_level":   alert_level,
                },
                on_conflict="org_id,rule_category",
            )
            .execute()
        )
        return response.data[0] if response.data else {}

    def reset_to_defaults(self, org_id: str) -> int:
        """
        Deletes all custom rules for the org, reverting to system defaults.
        Returns the number of deleted rows.
        """
        response = (
            self.client.table("Rules_And_SLAs")
            .delete()
            .eq("org_id", org_id)
            .execute()
        )
        return len(response.data or [])
