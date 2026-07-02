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

    # --- Storage backend (S3-compatible object storage only) ---
    STORAGE_BACKEND: Literal["s3"] = "s3"

    # --- Task queue (Celery / Redis) ---
    CELERY_BROKER_URL: str = "redis://redis:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/1"

    # Working directory for extracted audio + chunks.
    PROCESSING_DIR: str = "/tmp/media_processing"

    # --- S3 / Cloud Storage (S3-compatible, e.g. Hetzner Object Storage) ---
    S3_BUCKET: str
    S3_REGION: str = "hel1"
    S3_ENDPOINT_URL: str = "https://hel1.your-objectstorage.com"  # S3-compatible store
    S3_PUBLIC_BASE_URL: str | None = None      # CDN / public host override
    AWS_ACCESS_KEY_ID: SecretStr
    AWS_SECRET_ACCESS_KEY: SecretStr

    OPENAI_API_KEY: SecretStr
    WHISPER_MODEL: str = "whisper-1"
    WHISPER_LANGUAGE: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "Sales Intelligence API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    SUPABASE_URL: str
    SUPABASE_ANON_KEY: str
    SUPABASE_SERVICE_KEY: str
    
    GOOGLE_APPLICATION_CREDENTIALS: str | None = None
    GOOGLE_PROJECT_ID: str | None = None
    VERTEX_AI_REGION: str = "us-central1"
    VERTEX_AI_MODEL: str = "gemini-2.5-flash"
    
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:8000"]

    # --- Zoom Webhook ---
    # Secret Token from Zoom Marketplace → App → Features → Webhooks.
    ZOOM_WEBHOOK_SECRET_TOKEN: SecretStr = SecretStr("")

@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    return Settings()


settings = get_settings()

