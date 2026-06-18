"""
Module: Application Settings & Environment
Purpose: Loads and validates all environment variables, API keys, database
         credentials, and global application configurations.
"""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Upload limits ---
    MAX_UPLOAD_SIZE: int = 500 * 1024 * 1024  # 500 MB
    ALLOWED_EXTENSIONS: set[str] = {
        # video
        "mp4", "mov", "mkv", "avi", "webm", "m4v",
        # audio
        "mp3", "wav", "m4a", "aac", "flac", "ogg", "opus",
    }
    ALLOWED_MIME_PREFIXES: tuple[str, ...] = ("video/", "audio/")

    # --- Storage backend ---
    STORAGE_BACKEND: Literal["local", "s3"] = "local"
    LOCAL_STORAGE_DIR: str = "/tmp/media_uploads"

    # --- Task queue (Celery / Redis) ---
    CELERY_BROKER_URL: str = "redis://redis:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/1"

    # Working directory for extracted audio + chunks.
    PROCESSING_DIR: str = "/tmp/media_processing"

    # --- S3 / Cloud Storage ---
    S3_BUCKET: str | None = None
    S3_REGION: str = "us-east-1"
    S3_ENDPOINT_URL: str | None = None       # for S3-compatible stores (MinIO, R2)
    S3_PUBLIC_BASE_URL: str | None = None     # CDN / public host override
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
