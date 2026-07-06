"""
Module: AI Analysis Pydantic Models
Purpose: Defines validated data structures for:
  1. The raw JSON payload returned by the AI insights engine (InsightsResult).
  2. The data contracts used when persisting results to the database
     (Meeting_Reports, Transcripts, Signals tables).

Design notes
------------
* All models use strict typing so mapping errors between the AI output and
  the DB schema surface at validation time, not at runtime.
* ``SentimentEnum``, ``SpeakerEnum``, ``SignalTypeEnum``, and ``GradeEnum``
  are kept in sync with the Postgres ENUM definitions.
* ``CompetitorEntry`` maps competitor-related keywords from the AI output
  into the ``competitors_summary`` JSONB column.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Database ENUM mirrors
# ---------------------------------------------------------------------------

class SentimentEnum(str, Enum):
    positive = "positive"
    neutral  = "neutral"
    negative = "negative"


class SpeakerEnum(str, Enum):
    rep     = "rep"
    client  = "client"
    unknown = "unknown"


class SignalTypeEnum(str, Enum):
    risk        = "risk"
    opportunity = "opportunity"


class GradeEnum(str, Enum):
    A_PLUS = "A+"
    A      = "A"
    B      = "B"
    C      = "C"
    D      = "D"


# ---------------------------------------------------------------------------
# AI Engine output sub-models
# These mirror the JSON schema in ``prompts/sales_intelligence.py``.
# ---------------------------------------------------------------------------

class MeetingSummaryAI(BaseModel):
    """Top-level meeting summary block returned by the Gemini LLM."""
    overall_sentiment:          str
    customer_engagement_score:  int = Field(ge=0, le=100)
    likelihood_to_close_score:  int = Field(ge=0, le=100)
    summary:                    str


class SentimentSegmentAI(BaseModel):
    """One entry in the sentiment_trajectory array."""
    timestamp:       str
    sentiment:       str
    sentiment_score: float = Field(ge=-1.0, le=1.0)
    reason:          str


class OpeningScriptAI(BaseModel):
    """One personalised opening script for the next call."""
    strategy: str
    script:   str


class KeywordEntryAI(BaseModel):
    """A single detected risk or opportunity keyword."""
    keyword:     str
    category:    str
    timestamp:   str
    confidence:  float = Field(ge=0.0, le=1.0)
    quote:       str
    explanation: str


class KeywordDetectionAI(BaseModel):
    """Container for all risk and opportunity keywords."""
    risks:         list[KeywordEntryAI] = Field(default_factory=list)
    opportunities: list[KeywordEntryAI] = Field(default_factory=list)


class NextActionAI(BaseModel):
    """One recommended next action."""
    priority: str   # "high" | "medium" | "low"
    action:   str
    reason:   str


# ---------------------------------------------------------------------------
# Assembled AI insights payload (output of insights_generator.generate())
# ---------------------------------------------------------------------------

class AIInsightsPayload(BaseModel):
    """
    The full parsed output of the Gemini insights engine, validated before
    being handed to the persistence layer.
    """
    file_id:                     str
    meeting_summary:             MeetingSummaryAI
    sentiment_trajectory:        list[SentimentSegmentAI] = Field(default_factory=list)
    opening_scripts_next_call:   list[OpeningScriptAI]    = Field(default_factory=list)
    keyword_detection:           KeywordDetectionAI        = Field(default_factory=KeywordDetectionAI)
    recommended_next_actions:    list[NextActionAI]        = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Scoring engine output (mirrors ScoringResult from scoring_engine.py)
# ---------------------------------------------------------------------------

class PillarScoresPayload(BaseModel):
    """Validated raw pillar scores (0-100)."""
    discovery:          float
    objection_handling: float
    talk_ratio:         float
    next_steps:         float
    closing:            float


class ScoringPayload(BaseModel):
    """
    The full output of the scoring engine, validated before being handed
    to the persistence layer.
    """
    pillar_scores:   PillarScoresPayload
    weighted_scores: dict[str, float]
    final_score:     float
    grade:           GradeEnum


# ---------------------------------------------------------------------------
# Transcript segment (mirrors TranscriptSegment from stt_whisper.py)
# ---------------------------------------------------------------------------

class TranscriptSegmentPayload(BaseModel):
    """One speaker turn as produced by the speech-transcription step."""
    id:      int
    start:   float
    end:     float
    speaker: SpeakerEnum
    text:    str


class TranscriptPayload(BaseModel):
    """Full transcription result for a single uploaded meeting file."""
    file_id:          str
    language:         Optional[str]   = None
    duration_seconds: float
    chunk_count:      int
    segments:         list[TranscriptSegmentPayload] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# DB insert payloads (used internally by the repository)
# ---------------------------------------------------------------------------

class MeetingReportRecord(BaseModel):
    """
    Maps all pipeline outputs into the Meeting_Reports table schema.
    ``None`` values are omitted on insert — Supabase will use the column
    default (NULL) for nullable fields.
    """
    meeting_id:                 str
    total_score:                Optional[int]         = None
    grade:                      Optional[str]         = None
    talk_ratio:                 Optional[int]         = None
    listening_score:            Optional[int]         = None
    discovery_score:            Optional[int]         = None
    objection_score:            Optional[int]         = None
    next_steps_score:           Optional[int]         = None
    closing_score:              Optional[int]         = None
    ai_summary:                 Optional[str]         = None
    opening_script:             Optional[str]         = None
    decision_maker_identified:  bool                  = False
    competitors_summary:        Optional[list[dict[str, Any]]] = None


class TranscriptRecord(BaseModel):
    """Maps one speaker-turn segment into the Transcripts table schema."""
    meeting_id:   str
    start_time:   float
    end_time:     float
    text_segment: str
    speaker:      str   # "rep" | "client" | "unknown"
    sentiment:    Optional[str] = None   # "positive" | "neutral" | "negative"


class SignalRecord(BaseModel):
    """Maps one risk/opportunity keyword into the Signals table schema."""
    meeting_id:   str
    transcript_id: str       # FK → Transcripts.id
    signal_type:  str        # "risk" | "opportunity"
    keyword:      str
