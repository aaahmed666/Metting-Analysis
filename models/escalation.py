"""
models/escalation.py — Escalation Rules + SLA Settings

EscalationRule: قواعد تنبيه تلقائية حسب أداء المندوبين
SLASetting:     مدة السماح لكل مرحلة في الـ pipeline
"""
from sqlalchemy import Column, Integer, String, Boolean, Float, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from datetime import datetime
from ..database import Base


class EscalationRule(Base):
    """
    مثال على قاعدة:
    لو مندوب ملوش اجتماع 7 أيام → ابعت إيميل للمدير (L2)
    """
    __tablename__ = "escalation_rules"

    id          = Column(Integer, primary_key=True)
    name        = Column(String(200), nullable=False)
    metric      = Column(String(50),  nullable=False)
    # no_meeting_days | score_drop | consecutive_losses
    # stale_pipeline  | low_score_streak | sla_breach

    threshold   = Column(Float, nullable=False, default=7)
    # الرقم الحدّ — مثلاً 7 للأيام، 20 لنقاط الانخفاض

    level       = Column(String(10), default="L1")
    # L1 = خفيف | L2 = متوسط | L3 = حرج

    action      = Column(String(50), default="email_manager")
    # email_rep | email_manager | email_both | dashboard_alert

    active      = Column(Boolean, default=True)
    created_by  = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id":         self.id,
            "name":       self.name,
            "metric":     self.metric,
            "threshold":  self.threshold,
            "level":      self.level,
            "action":     self.action,
            "active":     self.active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class SLASetting(Base):
    """
    مدة السماح لكل deal_stage قبل إرسال تنبيه.
    مثال: qualified = 7 أيام → لو بعد 7 أيام مافيش تحديث → alert
    """
    __tablename__ = "sla_settings"

    id          = Column(Integer, primary_key=True)
    stage       = Column(String(30), unique=True, nullable=False)
    # qualified | proposal | negotiation | closing

    max_days    = Column(Integer, nullable=False)
    # عدد الأيام المسموحة قبل التنبيه

    notify_rep      = Column(Boolean, default=True)
    notify_manager  = Column(Boolean, default=True)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "stage":          self.stage,
            "max_days":       self.max_days,
            "notify_rep":     self.notify_rep,
            "notify_manager": self.notify_manager,
        }
