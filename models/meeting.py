"""models/meeting.py — نموذج الاجتماعات مع Indexes محسّنة"""
from sqlalchemy import Column, Integer, String, DateTime, Float, Date, ForeignKey, Text, Index
from sqlalchemy.orm import relationship
from datetime import datetime
from ..database import Base


class Meeting(Base):
    __tablename__ = "meetings"

    id               = Column(Integer, primary_key=True, index=True)
    user_id          = Column(Integer, ForeignKey("users.id"), nullable=False)
    customer_name    = Column(String(200), nullable=False)
    customer_company = Column(String(200), nullable=True)
    customer_industry = Column(String(50),  nullable=True)   # restaurant|medical|factory|retail|parking|education|sports|services|other
    meeting_title    = Column(String(300), nullable=True)
    file_path        = Column(Text, nullable=True)
    file_size_mb     = Column(Float, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    status           = Column(String(30), default="uploaded")
    # uploaded | processing | transcribing | validating | analyzing | analyzed | failed | rejected
    error_message    = Column(Text, nullable=True)
    meeting_date     = Column(Date, nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)
    processed_at     = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_meeting_user_created",  "user_id", "created_at"),
        Index("ix_meeting_user_status",   "user_id", "status"),
        Index("ix_meeting_created_at",    "created_at"),
        Index("ix_meeting_customer",      "user_id", "customer_name"),
    )

    user       = relationship("User",            back_populates="meetings")
    transcript = relationship("Transcript",      back_populates="meeting", uselist=False)
    analysis   = relationship("Analysis",        back_populates="meeting", uselist=False)
    score      = relationship("Score",           back_populates="meeting", uselist=False)
    segments   = relationship("SpeakerSegment",  back_populates="meeting")
    objections = relationship("Objection",       back_populates="meeting")

    def to_dict(self, include_analysis=False):
        data = {
            "id":               self.id,
            "customer_name":    self.customer_name,
            "customer_company":  self.customer_company,
            "customer_industry":  self.customer_industry,
            "meeting_title":    self.meeting_title,
            "status":           self.status,
            "duration_seconds": self.duration_seconds,
            "file_size_mb":     self.file_size_mb,
            "meeting_date":     str(self.meeting_date) if self.meeting_date else None,
            "created_at":       self.created_at.isoformat() if self.created_at else None,
            "error_message":    self.error_message,
        }
        if include_analysis:
            data["score"]    = self.score.to_dict()    if self.score    else None
            data["analysis"] = self.analysis.to_dict() if self.analysis else None
        return data
