from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from supabase import Client

logger = logging.getLogger(__name__)


class DealRepositoryError(Exception):
    pass


class DealRepository:
    def __init__(self, supabase: Client) -> None:
        self.client = supabase

    def get_deal_by_id(self, deal_id: str, user_id: str) -> Optional[dict]:
        try:
            response = (
                self.client.table("Deals")
                .select("*")
                .eq("id", deal_id)
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
            return response.data if response else None
        except Exception as exc:
            raise DealRepositoryError(f"Failed to fetch deal_id={deal_id}: {exc}") from exc

    def list_user_deals(self, user_id: str) -> list[dict]:
        try:
            response = (
                self.client.table("Deals")
                .select("*")
                .eq("user_id", user_id)
                .order("stage_updated_at", desc=True)
                .execute()
            )
            return response.data or []
        except Exception as exc:
            raise DealRepositoryError(f"Failed to list deals for user_id={user_id}: {exc}") from exc

    def update_deal_stage(self, deal_id: str, user_id: str, stage: str) -> Optional[dict]:
        try:
            # Check ownership first
            deal = self.get_deal_by_id(deal_id, user_id)
            if not deal:
                return None

            now = datetime.now(timezone.utc).isoformat()
            response = (
                self.client.table("Deals")
                .update({
                    "deal_stage": stage.lower().strip(),
                    "stage_updated_at": now,
                })
                .eq("id", deal_id)
                .execute()
            )
            return response.data[0] if response and response.data else None
        except Exception as exc:
            raise DealRepositoryError(f"Failed to update stage for deal_id={deal_id}: {exc}") from exc
