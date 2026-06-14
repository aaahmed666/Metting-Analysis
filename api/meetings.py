"""
api/meetings.py — Meetings endpoints
Features:
- Streaming upload + magic bytes validation + extension fallback
- Rate limiting: configurable via UPLOAD_RATE_LIMIT in .env (default 5/hour)
- Server-Sent Events للـ real-time status
- Search + فلترة متقدمة
- Export CSV
- Score trends
"""
import os
import uuid
import magic
import aiofiles
import asyncio
import json
import csv
import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, AsyncGenerator
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Request, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, func
from slowapi import Limiter
from slowapi.util import get_remote_address

from ..database import get_db
from ..models import Meeting, User, Score, Analysis
from ..services.storage_service import storage_service
# ✅ FIX: استيراد workers.tasks كان بيسحب torch + whisper جوه الـ API process
# (مئات الميجابايت RAM بلا داعي). بنبعت الـ task بالاسم عبر dispatcher خفيف.
from ..workers.queue import enqueue_process_meeting
from ..utils.auth import get_current_user
from ..config import settings

router  = APIRouter()
limiter = Limiter(key_func=get_remote_address)

# ── Constants ─────────────────────────────────────────
ALLOWED_EXT = {".mp4", ".mp3", ".m4a", ".wav", ".webm", ".ogg"}

ALLOWED_MIME_TYPES = {
    "video/mp4", "video/webm", "video/ogg",
    "audio/mpeg", "audio/mp4", "audio/x-m4a",
    "audio/wav", "audio/x-wav", "audio/ogg",
    "audio/webm", "application/ogg", "audio/x-mp3",
}

# ── Extension → MIME fallback ─────────────────────────
# بعض الـ MP4/M4A files بتيجي من python-magic كـ application/octet-stream
# لأن الـ ftyp atom ممكن تكون بعد أول 4096 bytes في الـ container.
# الحل: لو magic رجع octet-stream ولكن الـ extension مدعوم → نقبله.
EXT_MIME_FALLBACK: dict[str, str] = {
    ".mp4":  "video/mp4",
    ".webm": "video/webm",
    ".ogg":  "audio/ogg",
    ".mp3":  "audio/mpeg",
    ".m4a":  "audio/mp4",
    ".wav":  "audio/wav",
}

MAX_BYTES        = settings.MAX_FILE_SIZE_MB * 1024 * 1024
CHUNK_SIZE       = 1024 * 1024
MAGIC_READ_BYTES = 8192   # رفعنا من 4096 لـ 8192 لنكتشف ftyp box أكتر

STATUS_MSG = {
    "uploaded":     "⏳ في انتظار المعالجة",
    "processing":   "🎬 جاري استخراج الصوت",
    "transcribing": "🎙️ جاري تحويل الصوت لنص",
    "validating":   "🔍 جاري التحقق من المحتوى",
    "analyzing":    "🧠 جاري التحليل بالذكاء الاصطناعي",
    "analyzed":     "✅ اكتمل التحليل",
    "failed":       "❌ فشلت المعالجة",
    "rejected":     "⛔ تعذّر التحليل — المحتوى غير صالح",
}


def _validate_mime(first_bytes: bytes, filename: str) -> str:
    """
    يتحقق من نوع الملف الفعلي باستخدام magic bytes.
    لو python-magic رجع octet-stream ولكن الـ extension مدعوم → نقبله.
    هذا يحل مشكلة MP4 files التي تبدأ بـ ftyp atom بعد offset > 4096.
    """
    detected = magic.from_buffer(first_bytes, mime=True)

    # الحالة الطبيعية — magic عرف النوع وهو مدعوم
    if detected in ALLOWED_MIME_TYPES:
        return detected

    # Fallback — magic رجع octet-stream أو نوع غير معروف
    # نتحقق من الـ extension كـ secondary check
    if detected in ("application/octet-stream", "application/x-empty", ""):
        ext = Path(filename or "").suffix.lower()
        if ext in EXT_MIME_FALLBACK:
            return EXT_MIME_FALLBACK[ext]

    # نوع غير مدعوم فعلاً
    raise HTTPException(
        status_code=400,
        detail=f"نوع الملف الفعلي غير مدعوم ({detected}). المدعوم: صوت وفيديو فقط.",
    )


# ── POST /upload ──────────────────────────────────────
@router.post("/upload")
@limiter.limit(f"{settings.UPLOAD_RATE_LIMIT}/hour")
async def upload_meeting(
    request:           Request,
    file:              UploadFile = File(...),
    customer_name:     str        = Form(...),
    customer_company:  str        = Form(None),
    meeting_title:     str        = Form(None),
    meeting_date:      str        = Form(None),
    customer_industry: str        = Form(None),
    db:                Session    = Depends(get_db),
    current_user:      User       = Depends(get_current_user),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"امتداد غير مدعوم. المدعوم: {', '.join(ALLOWED_EXT)}")

    tmp_path = Path(settings.LOCAL_STORAGE_PATH) / "tmp" / f"{uuid.uuid4()}{ext}"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)

    total_size   = 0
    mime_checked = False

    try:
        async with aiofiles.open(tmp_path, "wb") as f:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                total_size += len(chunk)
                if not mime_checked:
                    _validate_mime(chunk[:MAGIC_READ_BYTES], file.filename)
                    mime_checked = True
                if total_size > MAX_BYTES:
                    raise HTTPException(413, f"الملف يتجاوز الحد ({settings.MAX_FILE_SIZE_MB}MB)")
                await f.write(chunk)
    except HTTPException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    except Exception as e:
        if tmp_path.exists():
            tmp_path.unlink()
        raise HTTPException(500, f"خطأ أثناء الرفع: {e}")

    if total_size < 1024:
        tmp_path.unlink()
        raise HTTPException(400, "الملف صغير جداً أو تالف")

    # ── parse meeting_date (string → date) بأمان ──
    parsed_meeting_date = None
    if meeting_date:
        try:
            parsed_meeting_date = datetime.strptime(meeting_date.strip(), "%Y-%m-%d").date()
        except (ValueError, AttributeError):
            parsed_meeting_date = None

    file_key = storage_service.save_from_path(str(tmp_path), file.filename)

    meeting = Meeting(
        user_id           = current_user.id,
        customer_name     = customer_name,
        customer_company  = customer_company,
        meeting_title     = meeting_title or f"اجتماع مع {customer_name}",
        customer_industry = customer_industry,
        file_path         = file_key,
        file_size_mb      = round(total_size / 1024 / 1024, 2),
        meeting_date      = parsed_meeting_date,
        status            = "uploaded",
    )
    db.add(meeting)
    db.commit()
    db.refresh(meeting)

    enqueue_process_meeting(meeting.id)

    return {
        "message":           "✅ تم استلام الملف. جاري المعالجة.",
        "meeting_id":        meeting.id,
        "status":            "uploaded",
        "estimated_minutes": max(2, round(total_size / 1024 / 1024 / 8)),
    }


# ── GET / — list + search ─────────────────────────────
@router.get("/")
def list_meetings(
    page:         int           = 1,
    limit:        int           = 20,
    status:       Optional[str] = None,
    q:            Optional[str] = None,
    date_from:    Optional[str] = None,
    date_to:      Optional[str] = None,
    score_min:    Optional[int] = None,
    score_max:    Optional[int] = None,
    db:           Session       = Depends(get_db),
    current_user: User          = Depends(get_current_user),
):
    query = db.query(Meeting).filter(Meeting.user_id == current_user.id)

    if status:
        query = query.filter(Meeting.status == status)

    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Meeting.customer_name.ilike(term),
                Meeting.customer_company.ilike(term),
                Meeting.meeting_title.ilike(term),
            )
        )

    if date_from:
        try:
            query = query.filter(Meeting.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
        except ValueError:
            pass

    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            query = query.filter(Meeting.created_at < dt)
        except ValueError:
            pass

    if score_min is not None or score_max is not None:
        query = query.join(Score, Score.meeting_id == Meeting.id, isouter=True)
        if score_min is not None:
            query = query.filter(Score.total_score >= score_min)
        if score_max is not None:
            query = query.filter(Score.total_score <= score_max)

    total = query.count()
    items = (
        query.order_by(Meeting.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )

    return {
        "total":    total,
        "page":     page,
        "pages":    (total + limit - 1) // limit,
        "meetings": [m.to_dict() for m in items],
    }


# ── GET /export — تصدير CSV ───────────────────────────
@router.get("/export")
def export_meetings(
    date_from:    Optional[str] = None,
    date_to:      Optional[str] = None,
    status:       Optional[str] = "analyzed",
    db:           Session       = Depends(get_db),
    current_user: User          = Depends(get_current_user),
):
    query = db.query(Meeting).filter(Meeting.user_id == current_user.id)

    if status:
        query = query.filter(Meeting.status == status)
    if date_from:
        try:
            query = query.filter(Meeting.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            query = query.filter(Meeting.created_at < dt)
        except ValueError:
            pass

    meetings = query.order_by(Meeting.created_at.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "ID", "العميل", "الشركة", "عنوان الاجتماع",
        "التاريخ", "المدة (دقيقة)", "الحالة",
        "التقييم /100", "الدرجة",
        "استماع /25", "اكتشاف /20", "اعتراضات /25", "خطوات تالية /15", "إغلاق /15",
        "احتمالية الإغلاق %", "اهتمام العميل", "مرحلة الصفقة",
        "نسبة كلام المندوب %",
    ])

    for m in meetings:
        sc  = m.score
        an  = m.analysis
        row = [
            m.id,
            m.customer_name,
            m.customer_company or "",
            m.meeting_title or "",
            m.created_at.strftime("%Y-%m-%d") if m.created_at else "",
            round(m.duration_seconds / 60) if m.duration_seconds else "",
            m.status,
            sc.total_score      if sc else "",
            sc.grade            if sc else "",
            sc.listening_score  if sc else "",
            sc.discovery_score  if sc else "",
            sc.objection_score  if sc else "",
            sc.next_steps_score if sc else "",
            sc.closing_score    if sc else "",
            an.closing_probability if an else "",
            an.customer_interest   if an else "",
            an.deal_stage          if an else "",
            an.talk_ratio          if an else "",
        ]
        writer.writerow(row)

    output.seek(0)

    filename = f"meetings_{current_user.id}_{datetime.now().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── GET /trends ───────────────────────────────────────
@router.get("/trends")
def get_trends(
    period:       str     = Query("weekly", enum=["weekly", "monthly"]),
    weeks:        int     = Query(8, ge=2, le=24),
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    now   = datetime.utcnow()
    since = now - timedelta(weeks=weeks)

    meetings = (
        db.query(Meeting)
        .filter(
            Meeting.user_id    == current_user.id,
            Meeting.status     == "analyzed",
            Meeting.created_at >= since,
        )
        .all()
    )

    if not meetings:
        return {"period": period, "data": [], "summary": None}

    buckets: dict[str, list] = {}

    for m in meetings:
        if not m.score:
            continue
        key = m.created_at.strftime("%Y-W%W") if period == "weekly" else m.created_at.strftime("%Y-%m")
        buckets.setdefault(key, []).append(m.score.total_score)

    data = [
        {
            "period":    k,
            "avg_score": round(sum(v) / len(v), 1),
            "count":     len(v),
            "best":      max(v),
        }
        for k, v in sorted(buckets.items())
    ]

    summary = None
    if len(data) >= 2:
        first_half  = data[:len(data)//2]
        second_half = data[len(data)//2:]
        avg_first   = sum(d["avg_score"] for d in first_half)  / len(first_half)
        avg_second  = sum(d["avg_score"] for d in second_half) / len(second_half)
        diff        = round(avg_second - avg_first, 1)
        summary = {
            "trend":          "improving" if diff > 2 else "declining" if diff < -2 else "stable",
            "change":         diff,
            "overall_avg":    round(sum(d["avg_score"] for d in data) / len(data), 1),
            "total_meetings": sum(d["count"] for d in data),
        }

    return {"period": period, "data": data, "summary": summary}


# ── GET /{id} ─────────────────────────────────────────
@router.get("/{meeting_id}")
def get_meeting(
    meeting_id:   int,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(404, "الاجتماع غير موجود")
    if current_user.role == "sales" and meeting.user_id != current_user.id:
        raise HTTPException(403, "غير مصرح")
    return meeting.to_dict(include_analysis=True)


# ── GET /{id}/stream — SSE ────────────────────────────
@router.get("/{meeting_id}/stream")
async def stream_status(
    meeting_id:   int,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    """
    ✅ ARCH FIX: النسخة القديمة كانت بتفتح DB session جديدة كل 5 ثواني
    لمدة تصل لـ 30 دقيقة لكل متصفح متصل — مع pool_size=10 كام تبويبة
    متزامنة كانت بتستنزف الـ connection pool. دلوقتي:
    - الـ worker بينشر تغييرات الحالة على Redis pub/sub (في _update_status)
    - الـ SSE بيستنى الرسائل بدل ما يستعلم الـ DB
    - fallback: فحص DB كل 30 ثانية فقط (لو رسالة ضاعت أو الـ worker
      اشتغل قبل الاشتراك) — تخفيض ~6x في فتح الجلسات حتى في أسوأ حالة
    """
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(404, "غير موجود")
    if current_user.role == "sales" and meeting.user_id != current_user.id:
        raise HTTPException(403, "غير مصرح")

    TERMINAL          = ("analyzed", "failed", "rejected")
    MAX_SECONDS       = 30 * 60   # حد أقصى 30 دقيقة لكل اتصال
    DB_FALLBACK_EVERY = 30.0      # فحص دوري احتياطي

    def _read_payload() -> Optional[dict]:
        """قراءة قصيرة من الـ DB لبناء الـ payload — جلسة تُفتح وتُقفل فوراً."""
        fresh_db = next(get_db())
        try:
            m = fresh_db.query(Meeting).filter(Meeting.id == meeting_id).first()
            if not m:
                return None
            return {
                "status":  m.status,
                "message": STATUS_MSG.get(m.status, ""),
                "error":   m.error_message if m.status == "failed" else None,
                "score":   m.score.total_score if m.score else None,
            }
        finally:
            fresh_db.close()

    async def event_generator() -> AsyncGenerator[str, None]:
        import time as _time
        import redis.asyncio as aioredis
        from ..utils.redis_client import meeting_status_channel

        r      = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        pubsub = r.pubsub()
        try:
            await pubsub.subscribe(meeting_status_channel(meeting_id))

            # snapshot أولي (الحالة وقت الاتصال)
            payload = await asyncio.to_thread(_read_payload)
            if payload is None:
                yield 'data: {"done": true}\n\n'
                return
            last_status = payload["status"]
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if last_status in TERMINAL:
                yield 'data: {"done": true}\n\n'
                return

            started       = _time.monotonic()
            last_db_check = started

            while _time.monotonic() - started < MAX_SECONDS:
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=5.0
                )
                now        = _time.monotonic()
                new_status = None

                if msg and msg.get("type") == "message":
                    new_status = msg.get("data")
                elif now - last_db_check >= DB_FALLBACK_EVERY:
                    # fallback نادر — رسالة ممكن تكون فاتتنا
                    fallback = await asyncio.to_thread(_read_payload)
                    last_db_check = now
                    if fallback is None:
                        break
                    if fallback["status"] != last_status:
                        new_status = fallback["status"]

                if new_status and new_status != last_status:
                    payload = await asyncio.to_thread(_read_payload)
                    if payload is None:
                        break
                    last_status = payload["status"]
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    if last_status in TERMINAL:
                        break

            yield 'data: {"done": true}\n\n'
        finally:
            try:
                await pubsub.unsubscribe()
                await pubsub.aclose()
                await r.aclose()
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── GET /{id}/status ──────────────────────────────────
@router.get("/{meeting_id}/status")
def get_status(
    meeting_id:   int,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(404, "غير موجود")
    if current_user.role == "sales" and meeting.user_id != current_user.id:
        raise HTTPException(403, "غير مصرح")
    return {
        "status":  meeting.status,
        "message": STATUS_MSG.get(meeting.status, ""),
        "error":   meeting.error_message if meeting.status == "failed" else None,
        "score":   meeting.score.total_score if meeting.score else None,
    }


# ── GET /{id}/transcript ──────────────────────────────
@router.get("/{meeting_id}/transcript")
def get_transcript(
    meeting_id:   int,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(404, "غير موجود")
    if current_user.role == "sales" and meeting.user_id != current_user.id:
        raise HTTPException(403, "غير مصرح")
    if not meeting.transcript:
        raise HTTPException(404, "النص غير متاح بعد")
    return {
        "text":       meeting.transcript.full_text,
        "word_count": meeting.transcript.word_count,
        "segments": [
            {"speaker": s.speaker, "text": s.text, "start": s.start_time, "end": s.end_time}
            for s in meeting.segments
        ],
    }


# ── DELETE /{id} ──────────────────────────────────────
@router.delete("/{meeting_id}")
def delete_meeting(
    meeting_id:   int,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(404, "غير موجود")
    if current_user.role == "sales" and meeting.user_id != current_user.id:
        raise HTTPException(403, "غير مصرح")
    if meeting.file_path:
        storage_service.delete(meeting.file_path)
    db.delete(meeting)
    db.commit()
    return {"message": "تم الحذف"}


# ── PATCH /{id}/stage ─────────────────────────────────
from pydantic import BaseModel
from typing import Literal

class StageUpdate(BaseModel):
    # ✅ FIX: كان dict خام بدون أي Pydantic validation — دلوقتي Literal
    # بيرفض أي قيمة غير صالحة تلقائياً ويظهر الـ schema في OpenAPI.
    deal_stage: Literal["qualified", "proposal", "negotiation", "closing", "won", "lost"]

@router.patch("/{meeting_id}/stage")
def update_deal_stage(
    meeting_id:   int,
    body:         StageUpdate,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(404, "غير موجود")
    if current_user.role == "sales" and meeting.user_id != current_user.id:
        raise HTTPException(403, "غير مصرح")
    if not meeting.analysis:
        raise HTTPException(400, "الاجتماع لم يُحلَّل بعد")

    meeting.analysis.deal_stage = body.deal_stage
    db.commit()

    # ✅ PERF FIX: كان redis.keys("admin:*") — O(N) على الـ keyspace كله
    # وبيعمل block للـ Redis. دلوقتي مسح مفاتيح متتبَّعة فقط.
    from ..utils.redis_client import invalidate_admin_cache
    invalidate_admin_cache()

    return {"meeting_id": meeting_id, "deal_stage": body.deal_stage, "message": "تم التحديث"}


# ── GET /{id}/audio ───────────────────────────────────
@router.get("/{meeting_id}/audio")
def get_audio_url(
    meeting_id:   int,
    request:      Request,
    seek:         Optional[int] = None,
    db:           Session       = Depends(get_db),
    current_user: User          = Depends(get_current_user),
):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(404, "غير موجود")
    if current_user.role == "sales" and meeting.user_id != current_user.id:
        raise HTTPException(403, "غير مصرح")
    if not meeting.file_path:
        raise HTTPException(404, "الملف غير متاح")

    if settings.use_r2_storage:
        try:
            import boto3
            from botocore.client import Config

            r2 = boto3.client(
                "s3",
                endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
                aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
                config=Config(signature_version="s3v4"),
                region_name="auto",
            )
            url = r2.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.R2_BUCKET_NAME, "Key": meeting.file_path},
                ExpiresIn=3600,
            )
            return {"url": url, "seek": seek, "expires_in": 3600}
        except Exception as e:
            raise HTTPException(500, f"خطأ في توليد الرابط: {e}")
    else:
        # ✅ FIX: كان FRONTEND_URL.replace(":3000", ":8000") — بينكسر فوراً
        # لو الفرونت إند على دومين حقيقي أو HTTPS على 443. دلوقتي:
        # API_PUBLIC_URL من الإعدادات لو موجود، وإلا نبنيه من الـ request نفسه
        # (يشتغل صح وراء أي reverse proxy بيمرر X-Forwarded-* headers).
        base_url = settings.API_PUBLIC_URL.rstrip("/") if settings.API_PUBLIC_URL \
                   else str(request.base_url).rstrip("/")
        url      = f"{base_url}/api/meetings/{meeting_id}/stream-audio"
        return {"url": url, "seek": seek, "expires_in": None}


# ── GET /{id}/stream-audio ────────────────────────────
@router.get("/{meeting_id}/stream-audio")
async def stream_audio_file(
    meeting_id:   int,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    from fastapi.responses import FileResponse

    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting or not meeting.file_path:
        raise HTTPException(404, "غير موجود")
    if current_user.role == "sales" and meeting.user_id != current_user.id:
        raise HTTPException(403, "غير مصرح")

    file_path = Path(settings.LOCAL_STORAGE_PATH) / meeting.file_path
    if not file_path.exists():
        raise HTTPException(404, "الملف غير موجود على القرص")

    return FileResponse(
        str(file_path),
        media_type="audio/mpeg",
        headers={"Accept-Ranges": "bytes"},
    )


# ── GET /pipeline/view ────────────────────────────────
@router.get("/pipeline/view")
def get_pipeline(
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    subq = (
        db.query(
            Meeting.customer_name,
            Meeting.customer_company,
            func.max(Meeting.id).label("latest_id"),
        )
        .filter(Meeting.user_id == current_user.id, Meeting.status == "analyzed")
        .group_by(Meeting.customer_name, Meeting.customer_company)
        .subquery()
    )

    meetings = (
        db.query(Meeting)
        .join(subq, Meeting.id == subq.c.latest_id)
        .all()
    )

    stages = {
        "qualified":   [],
        "proposal":    [],
        "negotiation": [],
        "closing":     [],
        "won":         [],
        "lost":        [],
    }

    for m in meetings:
        stage = (m.analysis.deal_stage if m.analysis else None) or "qualified"
        if stage not in stages:
            stage = "qualified"
        stages[stage].append({
            "meeting_id":          m.id,
            "customer_name":       m.customer_name,
            "customer_company":    m.customer_company,
            "score":               m.score.total_score             if m.score    else None,
            "closing_probability": m.analysis.closing_probability  if m.analysis else None,
            "created_at":          m.created_at.isoformat()        if m.created_at else None,
        })

    return {
        "stages":       stages,
        "total_active": sum(len(v) for k, v in stages.items() if k not in ("won", "lost")),
    }