from __future__ import annotations

from pydantic import BaseModel, Field


class DealStageUpdateRequest(BaseModel):
    stage: str = Field(
        ...,
        min_length=1,
        description="New deal stage (e.g., lead, qualified, proposal, negotiation, closed_won, closed_lost)",
    )
