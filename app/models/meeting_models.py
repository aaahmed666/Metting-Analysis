"""
Module: Pydantic Models – Rep Meeting Upload
Purpose: Request/response schemas used by the rep meeting-upload endpoint.
         Keeps HTTP-layer validation completely separate from business logic.
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class MeetingUploadResponse(BaseModel):
    """Returned immediately after a successful upload + pipeline trigger."""
    success:     bool
    meeting_id:  str
    deal_id:     str
    status:      str  # "processing"
    file_url:    str
    message:     str


class MeetingStatusResponse(BaseModel):
    """Returned by the GET /rep/meetings/{meeting_id} status endpoint."""
    meeting_id:   str
    deal_id:      str
    status:       str
    file_url:     Optional[str] = None
    meeting_date: Optional[str] = None
    duration_seconds: Optional[int] = None
    rejection_reason: Optional[str] = None
