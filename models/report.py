"""models/report.py — نموذج التقارير اليومية"""
from sqlalchemy import Column, Integer, String, Float, Text, DateTime, Date, ForeignKey, JSON
from datetime import datetime
from ..database import Base


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id          = Column(Integer, primary_key=True)
    report_date = Column(Date, unique=True, nullable=False, index=True)

    total_meetings     = Column(Integer, default=0)
    total_duration_min = Column(Integer, default=0)
    avg_score          = Column(Float)
    avg_closing_prob   = Column(Float)

    top_performer_id  = Column(Integer, ForeignKey("users.id"), nullable=True)
    needs_coaching_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    top_objections = Column(JSON)
    reps_data      = Column(JSON)

    report_html = Column(Text)
    report_json = Column(JSON)

    sent_to    = Column(JSON)
    sent_at    = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
