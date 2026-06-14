"""
config.py — إعدادات النظام المركزية
كل الإعدادات بتيجي من .env
"""
from pydantic_settings import BaseSettings
from pydantic import field_validator, model_validator
from typing import Optional
from pathlib import Path


class Settings(BaseSettings):
    # ── Database ─────────────────────────────────────
    DATABASE_URL: str

    # ── Security ─────────────────────────────────────
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480  # 8 ساعات

    # ── AI Services ──────────────────────────────────
    GROQ_API_KEY: str
    GEMINI_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"

    # ── Whisper ──────────────────────────────────────
    WHISPER_MODEL: str = "large-v3-turbo"
    WHISPER_LANGUAGE: str = "ar"
    WHISPER_DEVICE: str = "cuda"  # cuda | cpu | mps
    # backend: "openai" (الافتراضي الحالي) أو "faster" (faster-whisper / CTranslate2 —
    # أسرع 3-5× وأقل VRAM بنفس الدقة). لو faster-whisper مش متثبتة → fallback تلقائي.
    WHISPER_BACKEND: str = "openai"
    WHISPER_COMPUTE_TYPE: str = ""  # فاضي = تلقائي (int8_float16 على cuda، int8 على cpu)

    # ── Chunking للاجتماعات الطويلة ──────────────────
    CHUNK_THRESHOLD_MIN: int = 10   # اجتماعات أطول من كده → chunked
    CHUNK_SIZE_MIN: int = 5         # حجم كل chunk بالدقائق
    CHUNK_OVERLAP_SEC: int = 10     # overlap بين chunks
    # ملاحظة: inference الـ Whisper متسلسل بـ lock (مش thread-safe)،
    # فالقيمة 1 هي الصحيحة على GPU واحد. ارفعها فقط لو عندك multi-GPU setup مخصص.
    CHUNK_WORKERS: int = 1          # parallel workers

    # ── AI Analysis ──────────────────────────────────
    # حجم الـ transcript الأقصى المُرسل للـ AI (بالحروف).
    # llama-3.3-70b على Groq سياقه 128K token؛ 48K حرف عربي ≈ 20-30K token.
    # لو بتصطدم بحدود TPM عند Groq قلّل الرقم من الـ .env.
    AI_MAX_TRANSCRIPT_CHARS: int = 48000

    # ── Speaker Diarization (اختياري — pyannote) ─────
    # لو True + HF_TOKEN موجود + pyannote.audio متثبتة → diarization حقيقي
    # بالـ embeddings بدل الـ heuristics. لو أي شرط ناقص → fallback تلقائي للـ heuristics.
    DIARIZATION_ENABLED: bool = False
    HF_TOKEN: str = ""              # HuggingFace token لـ pyannote/speaker-diarization-3.1

    # ── Email (Resend.com) ───────────────────────────
    RESEND_API_KEY: str = ""
    FROM_EMAIL: str = "reports@example.com"
    ADMIN_EMAILS: str = ""          # comma-separated

    # ── Storage (Cloudflare R2 أو Local) ─────────────
    R2_ACCOUNT_ID: Optional[str] = None
    R2_ACCESS_KEY_ID: Optional[str] = None
    R2_SECRET_ACCESS_KEY: Optional[str] = None
    R2_BUCKET_NAME: str = "sales-meetings"
    R2_PUBLIC_URL: str = ""
    LOCAL_STORAGE_PATH: str = "./storage/meetings"

    # ── Redis ────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379"

    # ── App ──────────────────────────────────────────
    APP_NAME: str = "Sales Intelligence System"
    FRONTEND_URL: str = "http://localhost:3000"
    # عنوان الـ API كما يراه المتصفح (يُستخدم لروابط الصوت المحلية).
    # فاضي = يُبنى تلقائياً من الـ request نفسه.
    API_PUBLIC_URL: str = ""
    DAILY_REPORT_HOUR: int = 8
    MAX_FILE_SIZE_MB: int = 500
    ENVIRONMENT: str = "development"

    # ── Observability ─────────────────────────────────
    SENTRY_DSN: str = ""            # فاضي = Sentry غير مفعّل
    LOG_LEVEL: str = "INFO"

    # ── Rate Limiting ─────────────────────────────────
    # ارفع الرقم في .env وانت بتطور: UPLOAD_RATE_LIMIT=100
    # حطه 5 في production
    UPLOAD_RATE_LIMIT: int = 5

    # ── Company Context للـ AI Prompt ────────────────
    COMPANY_NAME: str = "شركتنا"
    PRODUCT_NAME: str = "منتجنا"
    TARGET_MARKET: str = "السوق المستهدف"
    SALES_CYCLE_DAYS: int = 30

    @property
    def use_r2_storage(self) -> bool:
        return bool(self.R2_ACCOUNT_ID and self.R2_ACCESS_KEY_ID)

    # ── SECRET_KEY strength ──────────────────────────
    # المفتاح بيوقّع كل الـ JWTs بـ HS256 — مفتاح قصير = اختراق كل الجلسات.
    # في production: نرفض الإقلاع نهائياً لو أقل من 32 حرف (fail closed).
    # في development: تحذير فقط حتى لا نكسر البيئات المحلية القائمة.
    @model_validator(mode="after")
    def _enforce_secret_key_strength(self):
        if len(self.SECRET_KEY) < 32:
            if self.ENVIRONMENT == "production":
                raise ValueError(
                    "SECRET_KEY must be at least 32 characters in production. "
                    "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
                )
            import warnings
            warnings.warn(
                "SECRET_KEY is shorter than 32 characters — fine for development, "
                "but the app will REFUSE to start in production with this key.",
                stacklevel=1,
            )
        return self

    @property
    def admin_email_list(self) -> list[str]:
        return [e.strip() for e in self.ADMIN_EMAILS.split(",") if e.strip()]

    # ── Zoom Integration ──────────────────────────────────
    ZOOM_WEBHOOK_SECRET:  str = ""   # من Zoom App → Features → Webhooks → Secret Token
    RGEEB_WEBHOOK_SECRET: str = ""   # من Rgeeb للتحقق من الـ callbacks

    class Config:
        env_file = Path(__file__).parent / ".env"
        extra = "ignore"


settings = Settings()