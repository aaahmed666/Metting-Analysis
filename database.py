"""
database.py — إعداد قاعدة البيانات
SQLAlchemy + PostgreSQL
"""
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from .config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,       # تحقق من الاتصال قبل كل استخدام
    pool_size=10,
    max_overflow=20,
    pool_recycle=3600,        # تجديد الاتصالات كل ساعة
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Dependency للاستخدام في FastAPI endpoints."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """إنشاء كل الجداول."""
    from . import models  # noqa — import لتسجيل الـ models
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created/verified")
