"""
services/webhook_service.py — إرسال Webhook بعد كل تحليل

يُستدعى من workers/tasks.py بعد اكتمال التحليل.
يجيب الـ URLs المسجلة من Redis ويبعت POST لكل واحد.

✅ SECURITY FIX (SSRF): النسخة القديمة كانت بتعمل POST لأي URL مسجّل
بدون أي تحقق — webhook يشاور على http://169.254.169.254 (cloud metadata)
أو http://localhost:6379 كان بيخلي السيرفر نفسه أداة استكشاف للشبكة
الداخلية. دلوقتي:
- https فقط + الـ host لازم يتحلّ لـ IP عام (فحص قبل كل إرسال —
  الـ DNS ممكن يتغير بعد التسجيل: DNS rebinding)
- follow_redirects=False — حتى الـ 302 ما يقدرش يحوّل لـ host داخلي
- نفس الفحص بيحصل عند التسجيل في api/admin.py (دفاع مزدوج)
"""
import json
import logging
import httpx
from datetime import datetime
from ..utils.redis_client import redis_client
from ..utils.url_safety import is_safe_webhook_url

logger = logging.getLogger(__name__)

WEBHOOK_STORE_KEY = "admin:webhooks"


def dispatch_webhook(event: str, payload: dict) -> int:
    """
    يبعت الـ webhook لكل الـ URLs المسجلة.
    يرجع عدد الـ webhooks اللي بُعتوا بنجاح.
    """
    raw      = redis_client.get(WEBHOOK_STORE_KEY)
    webhooks = json.loads(raw) if raw else []

    if not webhooks:
        return 0

    body = {
        "event":     event,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        **payload,
    }

    sent = 0
    # client واحد لكل الـ hooks (connection reuse) بدل client لكل hook
    with httpx.Client(timeout=10.0, follow_redirects=False) as client:
        for hook in webhooks:
            if not hook.get("active", True):
                continue
            if event not in hook.get("events", []):
                continue

            url = hook.get("url", "")
            safe, reason = is_safe_webhook_url(url)
            if not safe:
                logger.warning("Webhook skipped (unsafe URL) %s: %s", url, reason)
                continue

            try:
                r = client.post(
                    url,
                    json=body,
                    headers={
                        "Content-Type":  "application/json",
                        "User-Agent":    "SalesIntelligence-Webhook/1.0",
                        "X-Event-Type":  event,
                    },
                )
                if r.status_code < 300:
                    sent += 1
                    logger.info("Webhook sent to %s → %s", url, r.status_code)
                else:
                    logger.warning("Webhook failed for %s → %s", url, r.status_code)
            except Exception as e:
                logger.error("Webhook error for %s: %s", url, e)

    return sent
