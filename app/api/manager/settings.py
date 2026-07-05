"""
Module: Manager – SLA & Escalation Settings Routes
Purpose: Allows the manager to view and customize their organisation's
         escalation thresholds and SLA rules stored in Rules_And_SLAs.

Endpoints
─────────
GET  /manager/settings/rules          → get current rules (with defaults for missing ones)
PUT  /manager/settings/rules/{category} → update a single rule
POST /manager/settings/rules/reset    → reset all rules to system defaults

Rule categories
───────────────
Category                  | Level  | conditions keys
─────────────────────────────────────────────────────────────────
no_meetings_days          | yellow | days (int)
low_score_threshold       | yellow | threshold (float)
score_drop_threshold      | orange | drop (float), meetings_count (int)
repeated_losses           | orange | min_losses (int), days (int)
consecutive_losses        | red    | count (int)
sla_qualified_days        | red    | days (int)
sla_proposal_days         | red    | days (int)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status
from fastapi.responses import JSONResponse
from supabase import Client

from app.core.dependencies import get_supabase_admin_client
from app.core.rbac import require_manager
from app.repositories.manager_repository import ManagerRepository
from app.repositories.rules_sla_repository import (
    CATEGORY_LEVELS,
    DEFAULTS,
    RulesSLARepository,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/manager/settings", tags=["Manager – SLA Settings"])

# Valid categories the manager is allowed to configure
VALID_CATEGORIES = set(DEFAULTS.keys())


# ─────────────────────────────────────────────────────────────────────────────
# GET /manager/settings/rules
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/rules", summary="Get current escalation rules and SLA thresholds")
async def get_rules(
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Returns all escalation rules for the manager's organisation.
    Categories not yet customised show the system default values.

    Each rule includes:
    - rule_category : identifier
    - alert_level   : yellow / orange / red
    - conditions    : the threshold values 
    - is_default    : true if using system default (not yet saved by the manager)
    """
    manager_repo = ManagerRepository(supabase)
    org_id       = manager_repo.get_caller_org(current_user["user_id"])

    rules_repo   = RulesSLARepository(supabase)
    saved_rows   = rules_repo.get_all_rules_raw(org_id)

    saved_map = {row["rule_category"]: row for row in saved_rows}

    result = []
    for category, default_conditions in DEFAULTS.items():
        if category in saved_map:
            row = saved_map[category]
            result.append({
                "rule_category": category,
                "alert_level":   row.get("alert_level", CATEGORY_LEVELS[category]),
                "conditions":    row["conditions"],
                "is_default":    False,
            })
        else:
            result.append({
                "rule_category": category,
                "alert_level":   CATEGORY_LEVELS[category],
                "conditions":    default_conditions,
                "is_default":    True,
            })

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"success": True, "rules": result},
    )


# ─────────────────────────────────────────────────────────────────────────────
# PUT /manager/settings/rules/{category}
# ─────────────────────────────────────────────────────────────────────────────

@router.put(
    "/rules/{rule_category}",
    summary="Update a single escalation rule or SLA threshold",
)
async def update_rule(
    rule_category: str = Path(..., description="Rule category to update"),
    conditions: dict = Body(..., description="New threshold values as JSON"),
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Creates or updates a single escalation rule for the organisation.

    Example — update the Qualified SLA from 7 to 5 days:
    ```
    PUT /manager/settings/rules/sla_qualified_days
    Body: {"days": 5}
    ```

    Example — lower the low-score threshold to 55:
    ```
    PUT /manager/settings/rules/low_score_threshold
    Body: {"threshold": 55}
    ```
    """
    if rule_category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unknown rule category '{rule_category}'. "
                f"Valid categories: {sorted(VALID_CATEGORIES)}"
            ),
        )

    manager_repo = ManagerRepository(supabase)
    org_id       = manager_repo.get_caller_org(current_user["user_id"])

    rules_repo = RulesSLARepository(supabase)
    updated    = rules_repo.upsert_rule(org_id, rule_category, conditions)

    logger.info(
        "Manager %s updated rule '%s' → %s",
        current_user["email"],
        rule_category,
        conditions,
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "success":       True,
            "message":       f"Rule '{rule_category}' updated successfully.",
            "rule_category": rule_category,
            "alert_level":   CATEGORY_LEVELS[rule_category],
            "conditions":    conditions,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST /manager/settings/rules/reset
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/rules/reset", summary="Reset all rules to system defaults")
async def reset_rules(
    current_user: dict = Depends(require_manager),
    supabase: Client = Depends(get_supabase_admin_client),
):
    """
    Deletes all custom rules for the organisation.
    The system will fall back to default thresholds on the next evaluation.
    """
    manager_repo = ManagerRepository(supabase)
    org_id       = manager_repo.get_caller_org(current_user["user_id"])

    rules_repo = RulesSLARepository(supabase)
    deleted    = rules_repo.reset_to_defaults(org_id)

    logger.info(
        "Manager %s reset all SLA rules (%d deleted)",
        current_user["email"],
        deleted,
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "success": True,
            "message": f"All custom rules removed. System defaults will be used.",
            "deleted": deleted,
            "defaults": DEFAULTS,
        },
    )
