"""
api/zoom_webhook.py — Zoom Webhook Integration
===============================================

كيف يعمل الـ Flow:
1. اجتماع Zoom يخلص → Zoom يرسل POST لسيرفرك على /api/zoom/webhook
2. السيرفر يتحقق من صحة الـ webhook (HMAC signature)
3. يجيب بـ 200 OK فوراً (Zoom يحتاج الرد خلال 3 ثواني)
4. في الخلفية يحمّل ملف M4A من Zoom — **streaming للقرص مباشرة، مش في الـ RAM**
5. يحفظه ويبدأ pipeline المعالجة تلقائياً
6. المندوب يستلم إيميل لما التحليل يخلص — بدون ما يعمل أي حاجة

✅ FIXES المطبّقة:
- Streaming download (httpx stream + chunks 1MB) بدل response.content
  → ذاكرة O(1MB) بدل O(حجم الملف)؛ تسجيلات متعددة متزامنة آمنة.
- الـ background handler بقى sync `def` → FastAPI بتشغّله في threadpool
  → استعلامات الـ DB وكتابة الملفات ما بتجمّدش الـ event loop.
- Idempotency: Zoom بيعيد إرسال الـ webhooks عند أي تأخير —
  recording UUID بيتسجّل في Redis فمفيش معالجة مكررة.
- dispatch بالاسم عبر workers/queue.py → الـ API process ما بيستوردش torch.

الشروط المطلوبة من Zoom:
- حساب Zoom Pro أو Business (مش Free)
- Cloud Recording مفعّل
- المندوب يضغط "Record to Cloud" قبل الاجتماع
- webhook endpoint على domain حقيقي (مش localhost)
"""
import hmac
import hashlib
import json
import httpx
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks

from ..database import SessionLocal
from ..models import Meeting, User
from ..services.storage_service import storage_service
from ..workers.queue import enqueue_process_meeting
from ..config import settings

router = APIRouter()

DOWNLOAD_CHUNK_BYTES = 1024 * 1024  # 1MB لكل chunk


# ── Signature Verification ────────────────────────────
def _verify_zoom_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """
    Zoom بيبعت كل webhook مع signature للتحقق إنه فعلاً منهم.
    بدون التحقق ده، أي حد يقدر يبعت POST لسيرفرك ويخلي النظام يشتغل.
    """
    if not settings.ZOOM_WEBHOOK_SECRET:
        # ✅ SECURITY FIX (fail closed): النسخة القديمة كانت بترجع True
        # لو الـ secret مش متضبوط — لو الـ env var وقعت في production
        # كان أي webhook مزوّر بيتقبل، والمزوّر يقدر يخلي السيرفر يحمّل
        # download_url من اختياره. دلوقتي: التساهل في development فقط.
        if settings.ENVIRONMENT == "production":
            print("❌ ZOOM_WEBHOOK_SECRET is not set in production — rejecting webhook")
            return False
        return True  # development فقط

    message = f"v0:{timestamp}:{body.decode()}"
    expected = "v0=" + hmac.new(
        settings.ZOOM_WEBHOOK_SECRET.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── Main Webhook Endpoint ─────────────────────────────
@router.post("/webhook")
async def zoom_webhook(request: Request, background: BackgroundTasks):
    """
    Zoom يبعت POST هنا بعد كل اجتماع يخلص.
    الـ endpoint لازم يرد بـ 200 خلال 3 ثواني — عشان كده المعالجة تكون في الخلفية.
    """
    body      = await request.body()
    timestamp = request.headers.get("x-zm-request-timestamp", "")
    signature = request.headers.get("x-zm-signature", "")

    # التحقق من الـ signature
    if not _verify_zoom_signature(body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Zoom signature")

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = payload.get("event")

    # ── Zoom URL Validation (مرة واحدة عند الإعداد الأول) ──
    # Zoom بيبعت هذا الـ event للتحقق من صحة الـ URL
    if event == "endpoint.url_validation":
        plain_token = payload["payload"]["plainToken"]
        hashed = hmac.new(
            settings.ZOOM_WEBHOOK_SECRET.encode(),
            plain_token.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "plainToken":     plain_token,
            "encryptedToken": hashed,
        }

    # ── Recording Completed ────────────────────────────
    if event == "recording.completed":
        # _handle_recording_completed دالة sync عمداً —
        # BackgroundTasks بتشغّل الدوال الـ sync في threadpool،
        # فالتحميل والـ DB ما بيلمسوش الـ event loop.
        background.add_task(_handle_recording_completed, payload)
        return {"message": "Processing started"}

    # أي event تاني — نقبله بهدوء
    return {"message": f"Event {event} received"}


# ── Background Handler (sync → يشتغل في threadpool) ───
def _handle_recording_completed(payload: dict):
    """
    يعالج الـ webhook في الخلفية بعد ما ردينا على Zoom.

    الـ payload بيحتوي على:
    - payload.object.uuid: معرّف التسجيل الفريد (للـ idempotency)
    - payload.object.topic: اسم الاجتماع
    - payload.object.host_email: إيميل المضيف
    - payload.object.duration: مدة الاجتماع (دقائق)
    - payload.object.recording_files: قائمة الملفات
    - download_token: token مؤقت لتحميل الملف (صالح ساعة)
    """
    obj            = payload.get("payload", {}).get("object", {})
    download_token = payload.get("download_token", "")
    host_email     = obj.get("host_email", "")
    topic          = obj.get("topic", "اجتماع Zoom")
    duration_min   = obj.get("duration", 0)
    recording_uuid = obj.get("uuid", "")
    recording_files = obj.get("recording_files", [])

    # ── Idempotency: Zoom بيعيد المحاولة → ما نعالجش نفس التسجيل مرتين ──
    if recording_uuid:
        try:
            from ..utils.redis_client import redis_client
            dedup_key = f"zoom_recording:{recording_uuid}"
            # SET NX — لو المفتاح موجود يعني اتعالج قبل كده
            if not redis_client.set(dedup_key, "1", nx=True, ex=86400 * 3):
                print(f"↩️ Zoom webhook: duplicate delivery for {recording_uuid} — skipped")
                return
        except Exception as e:
            # Redis واقع؟ نكمل — تكرار نادر أهون من فقدان تسجيل
            print(f"⚠️ Zoom dedup check failed (continuing): {e}")

    # نجيب ملف M4A فقط (الصوت بس — أصغر وأسرع)
    audio_file = next(
        (f for f in recording_files
         if f.get("file_type") == "M4A" and f.get("status") == "completed"),
        None
    )

    if not audio_file:
        print(f"⚠️ Zoom webhook: No M4A file found in {topic}")
        return

    download_url = audio_file.get("download_url", "")
    file_size    = audio_file.get("file_size", 0)

    # ✅ SECURITY FIX (SSRF): الـ download_url لازم يكون https على نطاق
    # Zoom رسمي ويتحلّ لـ IP عام — webhook مزوّر (أو dev mode) ما يقدرش
    # يخلي السيرفر يحمّل من خدمة داخلية أو cloud metadata.
    from ..utils.url_safety import is_zoom_download_url
    safe, reason = is_zoom_download_url(download_url)
    if not safe:
        print(f"❌ Zoom webhook: rejected download_url ({reason}): {download_url[:120]}")
        return

    # ✅ Guard: ارفض الملفات الأكبر من الحد المسموح قبل ما نبدأ التحميل أصلاً
    max_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024
    if file_size and file_size > max_bytes:
        print(f"❌ Zoom webhook: file too large ({file_size/1024/1024:.0f}MB > "
              f"{settings.MAX_FILE_SIZE_MB}MB) — skipped")
        return

    print(f"📥 Zoom webhook: Downloading {topic} | {duration_min}min | {file_size/1024/1024:.1f}MB")

    # ── جيب المندوب من قاعدة البيانات بناءً على إيميله ──
    db   = SessionLocal()
    user = None
    try:
        user = db.query(User).filter(
            User.email     == host_email,
            User.is_active == True,
        ).first()
    finally:
        db.close()

    if not user:
        print(f"⚠️ Zoom webhook: User {host_email} not found — تأكد إن إيميله مسجّل في النظام")
        return

    # ── حمّل الملف من Zoom — streaming للقرص مباشرة ────
    # ✅ FIX: مفيش response.content (كان بيحمّل الملف كله في الـ RAM).
    # بنكتب chunk-by-chunk → الذاكرة ثابتة مهما كان حجم التسجيل،
    # وأي عدد تسجيلات متزامنة آمن.
    tmp_path = Path(settings.LOCAL_STORAGE_PATH) / "tmp" / \
        f"zoom_{user.id}_{datetime.utcnow().timestamp():.0f}.m4a"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        downloaded = 0
        with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
            with client.stream(
                "GET",
                download_url,
                headers={"Authorization": f"Bearer {download_token}"},
                follow_redirects=True,
            ) as response:
                response.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=DOWNLOAD_CHUNK_BYTES):
                        f.write(chunk)
                        downloaded += len(chunk)
                        # الـ file_size من الـ payload ممكن يكذب — نفرض الحد فعلياً
                        if downloaded > max_bytes:
                            raise RuntimeError(
                                f"download exceeded {settings.MAX_FILE_SIZE_MB}MB limit"
                            )

        print(f"✅ Downloaded {downloaded/1024/1024:.1f}MB (streamed to disk)")
    except Exception as e:
        print(f"❌ Zoom webhook: Download failed: {e}")
        tmp_path.unlink(missing_ok=True)
        return

    file_key = storage_service.save_from_path(str(tmp_path), f"zoom_{topic}.m4a")

    # ── أنشئ Meeting record في DB ──────────────────────
    db = SessionLocal()
    try:
        meeting = Meeting(
            user_id          = user.id,
            customer_name    = _extract_customer_name(topic),
            meeting_title    = topic,
            file_path        = file_key,
            file_size_mb     = round(file_size / 1024 / 1024, 2),
            duration_seconds = duration_min * 60,
            status           = "uploaded",
            # نضع ملاحظة إن الاجتماع جاء من Zoom تلقائياً
            error_message    = f"auto-imported from Zoom | host: {host_email}",
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
        meeting_id = meeting.id
        print(f"✅ Meeting created: #{meeting_id}")
    finally:
        db.close()

    # ── ابدأ الـ pipeline تلقائياً ─────────────────────
    enqueue_process_meeting(meeting_id)
    print(f"🚀 Pipeline started for meeting #{meeting_id}")


def _extract_customer_name(topic: str) -> str:
    """
    يحاول يستخرج اسم العميل من عنوان الاجتماع.

    أمثلة:
    "اجتماع مع أحمد محمد - شركة ABC"  → "أحمد محمد"
    "Meeting with John - Demo"         → "John"
    "Sales Call - Client Name"         → "Client Name"
    "اجتماع عادي"                       → "عميل Zoom"
    """
    import re
    # محاولة استخراج الاسم بعد "مع" أو "with" أو "-"
    patterns = [
        r"مع\s+(.+?)(?:\s*-|\s*$)",          # "اجتماع مع أحمد"
        r"with\s+(.+?)(?:\s*-|\s*$)",         # "Meeting with John"
        r"(?:call|meeting)\s*[-:]\s*(.+?)$",  # "Sales Call: Client"
        r"-\s*(.+?)$",                         # "Zoom - عميل اسمه"
    ]
    for pattern in patterns:
        match = re.search(pattern, topic, re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            if 2 < len(name) < 100:
                return name

    return "عميل Zoom"
