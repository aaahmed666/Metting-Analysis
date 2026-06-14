"""models/user.py — نموذج المستخدمين والفرق"""
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from ..database import Base


class Team(Base):
    __tablename__ = "teams"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(100), nullable=False)
    manager_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    members = relationship("User", back_populates="team", foreign_keys="User.team_id")

    def to_dict(self):
        return {"id": self.id, "name": self.name, "manager_id": self.manager_id}


class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True)
    name          = Column(String(100), nullable=False)
    email         = Column(String(150), unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role          = Column(String(20), default="sales")  # sales | manager | admin
    team_id       = Column(Integer, ForeignKey("teams.id"), nullable=True)
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    last_login    = Column(DateTime, nullable=True)

    team     = relationship("Team", back_populates="members", foreign_keys=[team_id])
    meetings = relationship("Meeting", back_populates="user")

    def to_dict(self):
        return {
            "id":        self.id,
            "name":      self.name,
            "email":     self.email,
            "role":      self.role,
            "team_id":   self.team_id,
            "is_active": self.is_active,
        }
