"""models/transcript.py — نموذج النصوص والمقاطع"""
from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from ..database import Base


class Transcript(Base):
    __tablename__ = "transcripts"

    id                  = Column(Integer, primary_key=True)
    meeting_id          = Column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"), unique=True)
    full_text           = Column(Text, nullable=False)
    language            = Column(String(10), default="ar")
    word_count          = Column(Integer, nullable=True)
    whisper_model       = Column(String(30), nullable=True)
    processing_time_sec = Column(Integer, nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow)

    meeting = relationship("Meeting", back_populates="transcript")


class SpeakerSegment(Base):
    __tablename__ = "speaker_segments"

    id         = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    speaker    = Column(String(20))   # sales_rep | customer | unknown
    text       = Column(Text, nullable=False)
    start_time = Column(Float)
    end_time   = Column(Float)
    confidence = Column(Float)

    meeting = relationship("Meeting", back_populates="segments")
