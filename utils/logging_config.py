"""
utils/logging_config.py — Structured logging مركزي + Sentry اختياري

الـ backend كان كله print() — صفر observability في production.
setup_logging() بتتنادى مرة واحدة من main.py (API) ومن workers/tasks.py (Celery):
- صيغة موحّدة: timestamp | level | module | message
- LOG_LEVEL من الـ .env
- لو SENTRY_DSN موجود + sentry-sdk متثبتة → error tracking تلقائي
  (pip install sentry-sdk). لو مش متثبتة → تحذير وتكملة عادي.
"""
import logging
import sys

from ..config import settings

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    root = logging.getLogger()
    root.setLevel(level)
    # لا نكرر الـ handlers لو اتنادت مرتين بالغلط
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        root.addHandler(handler)

    # تهدئة الـ libraries الكلامية
    for noisy in ("httpx", "httpcore", "urllib3", "botocore", "boto3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # ── Sentry (اختياري) ──────────────────────────────
    if settings.SENTRY_DSN:
        try:
            import sentry_sdk
            sentry_sdk.init(
                dsn=settings.SENTRY_DSN,
                environment=settings.ENVIRONMENT,
                traces_sample_rate=0.1,
                send_default_pii=False,
            )
            logging.getLogger(__name__).info("Sentry error tracking enabled")
        except ImportError:
            logging.getLogger(__name__).warning(
                "SENTRY_DSN is set but sentry-sdk is not installed — "
                "run: pip install sentry-sdk"
            )
