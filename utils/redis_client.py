"""
utils/redis_client.py — Redis client مركزي
يُستخدم لـ: Reset tokens | Rate limit cache | Session data
"""
import redis
from ..config import settings


def _create_client() -> redis.Redis:
    return redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
        retry_on_timeout=True,
        health_check_interval=30,
    )


redis_client: redis.Redis = _create_client()


def get_redis() -> redis.Redis:
    """FastAPI Dependency."""
    return redis_client


# ══════════════════════════════════════════════════════
# Admin cache — tracked keys بدل keys("admin:*")
# ══════════════════════════════════════════════════════
# المشكلة القديمة: الإبطال كان بـ redis.keys("admin:*") — أمر O(N) على
# الـ keyspace كله وبيعمل block للـ Redis. الحل: كل مفتاح cache بيتسجّل
# في Set، والإبطال بيمسح أعضاء الـ Set فقط (O(عدد مفاتيح الكاش)).
ADMIN_CACHE_INDEX = "admin:cache_index"
ADMIN_CACHE_TTL   = 300  # 5 دقائق


def cache_admin_result(key: str, json_payload: str, ttl: int = ADMIN_CACHE_TTL) -> None:
    """يخزّن نتيجة admin endpoint ويسجّل المفتاح في الـ index."""
    try:
        pipe = redis_client.pipeline()
        pipe.setex(key, ttl, json_payload)
        pipe.sadd(ADMIN_CACHE_INDEX, key)
        pipe.expire(ADMIN_CACHE_INDEX, ttl * 2)
        pipe.execute()
    except Exception:
        pass  # الكاش تحسين مش شرط — لو Redis واقع الـ endpoint يكمل


def invalidate_admin_cache() -> None:
    """يمسح كل مفاتيح كاش الأدمن المعروفة — بدون scan."""
    try:
        keys = redis_client.smembers(ADMIN_CACHE_INDEX)
        pipe = redis_client.pipeline()
        for k in keys:
            pipe.delete(k)
        pipe.delete(ADMIN_CACHE_INDEX)
        pipe.execute()
    except Exception:
        pass


# ══════════════════════════════════════════════════════
# Meeting status pub/sub — للـ SSE بدل الـ DB polling
# ══════════════════════════════════════════════════════
def meeting_status_channel(meeting_id: int) -> str:
    return f"meeting_status:{meeting_id}"


def publish_meeting_status(meeting_id: int, status: str) -> None:
    """الـ worker بينشر هنا عند كل تغيير حالة؛ الـ SSE endpoints مشتركة."""
    try:
        redis_client.publish(meeting_status_channel(meeting_id), status)
    except Exception:
        pass  # الـ SSE عنده fallback دوري على الـ DB

