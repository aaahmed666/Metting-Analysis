"""models/analysis.py — نموذج التحليل والتقييم والاعتراضات (محدّث)"""
from sqlalchemy import Column, Integer, String, Text, Float, DateTime, Boolean, ForeignKey, JSON
from sqlalchemy.orm import relationship
from datetime import datetime
from ..database import Base


class Analysis(Base):
    __tablename__ = "analyses"

    id         = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"), unique=True)

    # الملخص
    summary    = Column(Text)
    key_points = Column(JSON)

    # ── جديد: Sentiment trajectory ───────────────────
    # {"opening": "warm", "middle": "hot", "closing": "cold",
    #  "trend": "declining", "turning_point": "لما ذُكر السعر..."}
    sentiment_trajectory = Column(JSON)

    # تحليل العميل
    customer_questions   = Column(JSON)
    customer_pain_points = Column(JSON)
    customer_interest    = Column(String(20))  # high | medium | low

    # ── جديد: Competitor intelligence ────────────────
    # {"mentioned": true, "names": ["X"], "customer_reaction": "...",
    #  "competitive_response": "..."}
    competitor_intel = Column(JSON)

    # ── جديد: Decision maker detection ──────────────
    # {"level": "manager", "confidence": "high",
    #  "signals": [...], "recommendation": "..."}
    decision_maker = Column(JSON)

    # الاعتراضات
    objections_raw     = Column(JSON)
    objections_handled = Column(JSON)

    # تحليل المندوب
    rep_strengths  = Column(JSON)
    rep_weaknesses = Column(JSON)
    talk_ratio     = Column(Float)
    missing_topics = Column(JSON)
    coaching_notes = Column(Text)

    # ── جديد: Opening script ─────────────────────────
    # {"line1": "...", "line2": "...", "line3": "...", "tip": "..."}
    opening_script = Column(JSON)

    # الخطوات التالية
    next_steps     = Column(JSON)
    follow_up_days = Column(Integer, default=2)

    # التوقعات
    closing_probability = Column(Integer)
    deal_stage          = Column(String(30))
    # qualified | proposal | negotiation | closing | lost | won

    # ── جديد: Meeting signals (بدون AI) ──────────────
    # {rep_speaking_pace_wpm, longest_silence_sec, longest_monologue_sec,
    #  danger_count, opportunity_count, danger_hits, ...}
    meeting_signals = Column(JSON)

    # Metadata
    ai_model_used       = Column(String(50))
    processing_time_sec = Column(Integer)
    created_at          = Column(DateTime, default=datetime.utcnow)

    meeting = relationship("Meeting", back_populates="analysis")

    def to_dict(self):
        return {
            "summary":               self.summary,
            "sentiment_trajectory":  self.sentiment_trajectory,
            "customer_questions":    self.customer_questions,
            "customer_pain_points":  self.customer_pain_points,
            "customer_interest":     self.customer_interest,
            "competitor_intel":      self.competitor_intel,
            "decision_maker":        self.decision_maker,
            "objections":            self.objections_raw,
            "rep_strengths":         self.rep_strengths,
            "rep_weaknesses":        self.rep_weaknesses,
            "talk_ratio":            self.talk_ratio,
            "missing_topics":        self.missing_topics,
            "coaching_notes":        self.coaching_notes,
            "opening_script":        self.opening_script,
            "next_steps":            self.next_steps,
            "follow_up_days":        self.follow_up_days,
            "closing_probability":   self.closing_probability,
            "deal_stage":            self.deal_stage,
            "meeting_signals":       self.meeting_signals,
        }


class Score(Base):
    __tablename__ = "scores"

    id         = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"), unique=True)
    user_id    = Column(Integer, ForeignKey("users.id"), index=True)

    listening_score  = Column(Float)
    discovery_score  = Column(Float)
    objection_score  = Column(Float)
    next_steps_score = Column(Float)
    closing_score    = Column(Float)

    total_score = Column(Float)
    grade       = Column(String(5))

    team_avg_score = Column(Float, nullable=True)
    percentile     = Column(Integer, nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)

    meeting = relationship("Meeting", back_populates="score")

    def to_dict(self):
        return {
            "listening_score":  self.listening_score,
            "discovery_score":  self.discovery_score,
            "objection_score":  self.objection_score,
            "next_steps_score": self.next_steps_score,
            "closing_score":    self.closing_score,
            "total_score":      self.total_score,
            "grade":            self.grade,
            "team_avg_score":   self.team_avg_score,
            "percentile":       self.percentile,
        }


class Objection(Base):
    __tablename__ = "objections"

    id               = Column(Integer, primary_key=True)
    meeting_id       = Column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    user_id          = Column(Integer, ForeignKey("users.id"), index=True)
    objection_text   = Column(Text, nullable=False)
    category         = Column(String(50), index=True)
    was_handled      = Column(Boolean, default=False)
    handling_quality = Column(String(20))
    created_at       = Column(DateTime, default=datetime.utcnow)

    meeting = relationship("Meeting", back_populates="objections")
