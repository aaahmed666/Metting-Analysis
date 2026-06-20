"""
Module: Application Settings & Environment
Purpose: Loads and validates all environment variables, API keys, storage
         credentials, and global application configuration from the environment
         (or a local .env file) using Pydantic Settings.
"""
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Runtime environment ---
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Upload limits ---
    MAX_UPLOAD_SIZE: int = 500 * 1024 * 1024  # 500 MB
    ALLOWED_EXTENSIONS: set[str] = {
        # video
        "mp4", "mov", "mkv", "avi", "webm", "m4v",
        # audio
        "mp3", "wav", "m4a", "aac", "flac", "ogg", "opus",
    }
    ALLOWED_MIME_PREFIXES: tuple[str, ...] = ("video/", "audio/")

    # When True, content-type sniffing (libmagic) MUST succeed; if libmagic is
    # unavailable the upload is rejected rather than silently trusting the
    # extension. Recommended True in staging/production.
    REQUIRE_MAGIC: bool = False

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
    S3_ENDPOINT_URL: str | None = None        # for S3-compatible stores (MinIO, R2)
    S3_PUBLIC_BASE_URL: str | None = None      # CDN / public host override
    AWS_ACCESS_KEY_ID: SecretStr | None = None
    AWS_SECRET_ACCESS_KEY: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    return Settings()
