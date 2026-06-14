"""
main.py — نقطة دخول التطبيق
FastAPI + CORS + Rate Limiting + Security Headers + Health Check متقدم
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from .database import init_db
from .api import auth, meetings, admin
from .api import zoom_webhook
from .api import rgeeb_callback
from .config import settings
from .utils.logging_config import setup_logging

# ✅ Observability: logging موحّد (timestamp | level | module) + Sentry
# اختياري لو SENTRY_DSN متضبوط — بدل ما كان النظام كله print() بلا تتبع.
setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    print(f"\n🚀 {settings.APP_NAME} started")
    print(f"   Env:     {settings.ENVIRONMENT}")
    print(f"   Storage: {'R2 ☁️' if settings.use_r2_storage else 'Local 💾'}")
    print(f"   Whisper: {settings.WHISPER_MODEL} ({settings.WHISPER_DEVICE})")
    yield
    print("🔴 Shutting down")


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title       = settings.APP_NAME,
    version     = "1.0.0",
    description = "نظام تحليل اجتماعات المبيعات بالذكاء الاصطناعي",
    lifespan    = lifespan,
    docs_url    = "/docs" if settings.ENVIRONMENT != "production" else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"]        = "DENY"
    response.headers["X-XSS-Protection"]       = "1; mode=block"
    response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    if settings.ENVIRONMENT == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


ALLOWED_ORIGINS = ["http://localhost:3000", settings.FRONTEND_URL]

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ALLOWED_ORIGINS,
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)


# ── CSRF protection (Origin check) ────────────────────
# ✅ SECURITY FIX: الـ auth بـ HttpOnly cookie و SameSite=lax — وده بيحمي
# من معظم سيناريوهات CSRF لكن مش كلها (top-level POST forms مثلاً).
# الطبقة الإضافية: أي request بيغيّر حالة (POST/PUT/PATCH/DELETE) وجاي
# من متصفح (فيه Origin header) لازم يكون الـ Origin من النطاقات المسموحة.
# الـ webhooks الخارجية (Zoom/Rgeeb) server-to-server وما بتبعتش Origin
# أصلاً + عندها HMAC signature خاصة بها — فمش متأثرة.
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

@app.middleware("http")
async def csrf_origin_check(request: Request, call_next):
    if request.method not in _CSRF_SAFE_METHODS:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in {o.rstrip("/") for o in ALLOWED_ORIGINS}:
            return JSONResponse(
                status_code=403,
                content={"detail": "Origin غير مسموح"},
            )
    return await call_next(request)

app.include_router(auth.router,     prefix="/api/auth",     tags=["Auth"])
app.include_router(meetings.router, prefix="/api/meetings", tags=["Meetings"])
app.include_router(admin.router,    prefix="/api/admin",    tags=["Admin"])
app.include_router(zoom_webhook.router,     prefix="/api/zoom",     tags=["Zoom"])
app.include_router(rgeeb_callback.router, prefix="/api/rgeeb",    tags=["Rgeeb"])


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    if settings.ENVIRONMENT == "production":
        return JSONResponse(status_code=500, content={"detail": "حدث خطأ داخلي"})
    return JSONResponse(status_code=500, content={"detail": str(exc), "type": type(exc).__name__})


# ── Health Check متقدم ────────────────────────────────
@app.get("/health")
def health():
    """
    Health check متقدم — يتحقق من كل الـ dependencies.
    """
    import shutil
    checks   = {}
    is_ok    = True

    # Database
    try:
        from .database import SessionLocal
        db = SessionLocal()
        db.execute(__import__("sqlalchemy").text("SELECT 1"))
        db.close()
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"
        is_ok = False

    # Redis
    try:
        from .utils.redis_client import redis_client
        redis_client.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"
        is_ok = False

    # Disk space
    try:
        usage = shutil.disk_usage("/")
        free_gb = round(usage.free / 1024**3, 1)
        checks["disk_free_gb"] = free_gb
        if free_gb < 2:
            checks["disk_warning"] = "أقل من 2GB متاح"
    except Exception:
        pass

    # Whisper model loaded?
    try:
        from .services.whisper_service import WhisperService
        checks["whisper_loaded"] = WhisperService._model is not None
    except Exception:
        checks["whisper_loaded"] = False

    return {
        "status":        "ok" if is_ok else "degraded",
        "version":       "1.0.0",
        "environment":   settings.ENVIRONMENT,
        "whisper_model": settings.WHISPER_MODEL,
        "whisper_device": settings.WHISPER_DEVICE,
        "checks":        checks,
    }
