"""
workers/tasks.py — Celery pipeline كامل

Pipeline:
1. استخراج الصوت (FFmpeg)
2. تحويل لنص (Whisper) — مباشر أو chunked
3. استخراج الإشارات (Signal service — بدون AI)
4. التحقق من صحة المحتوى (Validation)
5. تحليل AI (Groq/Gemini) — sentiment + script + competitor
6. Personal weakness pattern (كل 5 اجتماعات)
7. حفظ النتائج
8. Webhook dispatch
9. إشعار المندوب

Scheduled Tasks:
- cleanup_temp_files      : 3 AM يومياً
- send_followup_reminders : 9 AM يومياً
- send_daily_report_task  : حسب DAILY_REPORT_HOUR
- weekly_pattern_analysis : الأحد 8 AM
"""
import os
import json
from datetime import datetime, timedelta
from celery import Celery
from celery.schedules import crontab

from ..database import SessionLocal
from ..models import Meeting, Transcript, Analysis, Score, Objection, SpeakerSegment, User, Team
from ..services.audio_service import extract_audio, get_duration_seconds
from ..services.whisper_service import transcribe_audio
from ..services.chunking_service import chunking_service
from ..services.ai_service import analyze_transcript
from ..services.signal_service import extract_signals, signals_to_dict
from ..services.storage_service import storage_service
from ..services.email_service import send_email, send_analysis_ready
from ..services.validation_service import validation_service
from ..config import settings
from ..utils.logging_config import setup_logging

# ✅ Observability: تهيئة الـ logging الموحّد لعملية الـ worker
setup_logging()

celery_app = Celery(
    "sales_intelligence",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Africa/Cairo",
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "cleanup-temp-files":      {"task": "cleanup_temp_files",       "schedule": crontab(hour=3,  minute=0)},
        "send-followup-reminders": {"task": "send_followup_reminders",  "schedule": crontab(hour=9,  minute=0)},
        "send-daily-report":       {"task": "send_daily_report_task",   "schedule": crontab(hour=settings.DAILY_REPORT_HOUR, minute=0)},
        "weekly-pattern-analysis": {"task": "weekly_pattern_analysis",  "schedule": crontab(hour=8,  minute=0, day_of_week=0)},
    },
)


def _update_status(meeting_id: int, status_val: str, error: str = None):
    db = SessionLocal()
    try:
        m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if m:
            m.status = status_val
            if error:
                m.error_message = error[:500]
            if status_val == "analyzed":
                m.processed_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
    # ✅ ARCH FIX: ننشر التغيير على Redis — الـ SSE endpoint بيستقبله فوراً
    # بدل ما يستعلم الـ DB كل 5 ثواني (كان بيستنزف الـ connection pool).
    from ..utils.redis_client import publish_meeting_status
    publish_meeting_status(meeting_id, status_val)


# ══════════════════════════════════════════════════════
# NOTIFICATION TASKS — منفصلة عن المسار الحرج للمعالجة
# ══════════════════════════════════════════════════════
@celery_app.task(name="dispatch_webhook_task", max_retries=3, retry_backoff=True)
def dispatch_webhook_task(event: str, payload: dict):
    """إرسال الـ webhooks كـ task مستقل — فشله ما يأثرش على المعالجة."""
    from ..services.webhook_service import dispatch_webhook
    return dispatch_webhook(event, payload)


@celery_app.task(name="send_analysis_email_task", max_retries=3, retry_backoff=True)
def send_analysis_email_task(email: str, data: dict):
    """إرسال إيميل اكتمال التحليل كـ task مستقل."""
    send_analysis_ready(email, data)


# ══════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════
@celery_app.task(bind=True, max_retries=2, retry_backoff=True,
                 retry_backoff_max=300, name="process_meeting")
def process_meeting_task(self, meeting_id: int):
    db         = SessionLocal()
    audio_path = None
    local_path = None

    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise ValueError(f"Meeting {meeting_id} not found")
        user = meeting.user

        # ── Step 1: استخراج الصوت ─────────────────────
        _update_status(meeting_id, "processing")
        local_path = storage_service.get_local_path(meeting.file_path)
        duration   = get_duration_seconds(local_path)
        if duration > 0:
            meeting.duration_seconds = duration
            db.commit()
        audio_path = extract_audio(local_path)
        print(f"[{meeting_id}] ✅ Audio | {duration}s")

        # ── Step 2: تحويل الصوت لنص ───────────────────
        _update_status(meeting_id, "transcribing")
        CHUNK_THRESHOLD_SEC = settings.CHUNK_THRESHOLD_MIN * 60
        CHUNK_SIZE_SEC      = settings.CHUNK_SIZE_MIN * 60

        if duration >= CHUNK_THRESHOLD_SEC:
            n_chunks = max(1, duration // CHUNK_SIZE_SEC + 1)
            print(f"[{meeting_id}] 🔀 Chunked: ~{n_chunks} chunks")
            transcript_result = chunking_service.transcribe_with_chunking(
                audio_path=audio_path,
                chunk_duration_sec=CHUNK_SIZE_SEC,
                overlap_sec=settings.CHUNK_OVERLAP_SEC,
                max_workers=settings.CHUNK_WORKERS,
            )
            if transcript_result.get("chunks_failed"):
                db.query(Meeting).filter(Meeting.id == meeting_id).update(
                    {"error_message": f"تحذير: chunks {transcript_result['chunks_failed']} لم تتحول"}
                )
                db.commit()
        else:
            transcript_result = transcribe_audio(audio_path)

        full_text  = transcript_result["text"]
        segments   = transcript_result.get("segments", [])
        talk_ratio = float(transcript_result.get("talk_ratio", 50.0))
        word_count = transcript_result.get("word_count", len(full_text.split()))
        proc_time  = transcript_result.get("total_elapsed") or transcript_result.get("processing_time", 0)

        print(f"[{meeting_id}] ✅ Transcribed: {word_count}w | ratio={talk_ratio}%")

        # ── Step 2.5: Speaker diarization حقيقي (اختياري) ──
        # ✅ FIX: الـ heuristics (طول الجملة/علامة استفهام) مش diarization فعلي.
        # لو pyannote مفعّلة (DIARIZATION_ENABLED + HF_TOKEN) → نعيد تسمية
        # المتكلمين بالـ embeddings على الصوت *الكامل* (مش per-chunk عشان
        # اتساق الـ clusters)، ونعيد حساب talk_ratio. لو مش متاحة → الـ
        # heuristics القديمة شغالة كـ fallback بدون أي تعطّل.
        from ..services.diarization_service import diarization_service
        if segments and diarization_service.is_available():
            print(f"[{meeting_id}] 🗣️ Step 2.5: Speaker diarization (pyannote)")
            segments = diarization_service.assign_roles(audio_path, segments)
            rep_t   = sum(s.get("duration", 0) for s in segments if s.get("speaker") == "sales_rep")
            total_t = sum(s.get("duration", 0) for s in segments)
            if total_t > 0:
                talk_ratio = round((rep_t / total_t) * 100, 1)
                print(f"[{meeting_id}] ✅ Diarization done | recalculated ratio={talk_ratio}%")

        # ── Step 3: استخراج الإشارات (بدون AI) ────────
        print(f"[{meeting_id}] 📊 Step 3: Signal extraction")
        signals     = extract_signals(segments)
        signals_dict = signals_to_dict(signals)
        print(f"[{meeting_id}] ✅ Signals: pace={signals.rep_speaking_pace_wpm}wpm | "
              f"silence={signals.longest_silence_sec}s | "
              f"danger={signals.danger_count} | opp={signals.opportunity_count}")

        # ── Step 4: التحقق من صحة المحتوى ─────────────
        _update_status(meeting_id, "validating")
        validation = validation_service.validate(full_text, duration, word_count)
        if not validation.is_valid:
            rejection_data = json.dumps({
                "rejection_reason": validation.rejection_reason,
                "signals":          validation.signals,
            }, ensure_ascii=False)
            _update_status(meeting_id, "rejected", error=rejection_data)
            print(f"[{meeting_id}] ⛔ Rejected: {validation.rejection_reason}")
            return {"status": "rejected", "meeting_id": meeting_id}

        # حفظ النص والـ segments
        transcript_record = Transcript(
            meeting_id=meeting_id, full_text=full_text,
            word_count=word_count, whisper_model=settings.WHISPER_MODEL,
            processing_time_sec=proc_time,
        )
        db.add(transcript_record)
        # ✅ PERF FIX: bulk insert بدل db.add() لكل segment — اجتماع طويل
        # ممكن يطلع مئات الـ segments، والـ bulk بيقلل الـ round-trips للـ DB.
        segment_rows = [
            SpeakerSegment(
                meeting_id=meeting_id,
                speaker=seg.get("speaker", "unknown"),
                text=seg.get("text", ""),
                start_time=float(seg.get("start", 0)),
                end_time=float(seg.get("end", 0)),
                confidence=float(seg.get("confidence", 0.8)),
            )
            for seg in segments
        ]
        if segment_rows:
            db.bulk_save_objects(segment_rows)
        db.commit()

        # ── Step 5: تحليل AI ──────────────────────────
        _update_status(meeting_id, "analyzing")
        print(f"[{meeting_id}] 🧠 Step 5: AI Analysis")
        ai_result     = analyze_transcript(
            transcript         = full_text,
            customer_name      = meeting.customer_name,
            duration_seconds   = duration,
            talk_ratio         = talk_ratio,
            customer_industry  = meeting.customer_industry or "",
            signals            = signals_dict,
        )
        analysis_data = ai_result["analysis"]
        scores_data   = ai_result["scores"]
        print(f"[{meeting_id}] ✅ AI done | score={scores_data['total_score']} ({scores_data['grade']})")

        # ── Step 6: حفظ النتائج ───────────────────────
        analysis_obj = Analysis(
            meeting_id           = meeting_id,
            summary              = analysis_data["summary"],
            sentiment_trajectory = analysis_data.get("sentiment_trajectory"),
            customer_questions   = analysis_data["customer_questions"],
            customer_pain_points = analysis_data.get("customer_pain_points", []),
            customer_interest    = analysis_data["customer_interest"],
            competitor_intel     = analysis_data.get("competitor_intel"),
            objections_raw       = analysis_data["objections"],
            rep_strengths        = analysis_data["rep_strengths"],
            rep_weaknesses       = analysis_data["rep_weaknesses"],
            talk_ratio           = talk_ratio,
            missing_topics       = analysis_data.get("missing_topics", []),
            coaching_notes       = analysis_data.get("coaching_notes", ""),
            opening_script       = analysis_data.get("opening_script"),
            next_steps           = analysis_data["next_steps"],
            follow_up_days       = analysis_data.get("follow_up_days", 2),
            closing_probability  = analysis_data["closing_probability"],
            deal_stage           = analysis_data.get("deal_stage", "qualified"),
            meeting_signals      = signals_dict,
            decision_maker       = analysis_data.get("decision_maker"),
            ai_model_used        = analysis_data.get("ai_model_used", ""),
            processing_time_sec  = analysis_data.get("processing_time_sec", 0),
        )
        db.add(analysis_obj)

        score_obj = Score(
            meeting_id=meeting_id, user_id=meeting.user_id,
            listening_score=scores_data["listening_score"],
            discovery_score=scores_data["discovery_score"],
            objection_score=scores_data["objection_score"],
            next_steps_score=scores_data["next_steps_score"],
            closing_score=scores_data["closing_score"],
            total_score=scores_data["total_score"],
            grade=scores_data["grade"],
        )
        db.add(score_obj)

        for obj in analysis_data.get("objections", []):
            db.add(Objection(
                meeting_id=meeting_id, user_id=meeting.user_id,
                objection_text=obj.get("text", ""),
                category=obj.get("category", "other"),
                was_handled=obj.get("was_handled", False),
                handling_quality=obj.get("handling_quality", "not_handled"),
            ))

        db.commit()
        _update_status(meeting_id, "analyzed")
        print(f"[{meeting_id}] 🎉 Complete!")

        # ── Step 7: Pattern check (كل 5 اجتماعات) ─────
        analyzed_count = db.query(Meeting).filter(
            Meeting.user_id == meeting.user_id,
            Meeting.status  == "analyzed",
        ).count()
        if analyzed_count % 5 == 0 and analyzed_count > 0:
            generate_personal_pattern.delay(meeting.user_id)

        # ── Step 8 + 9: Webhook + إيميل — خارج المسار الحرج ─────
        # ✅ PERF FIX: كانوا بيتنفذوا inline جوه task المعالجة — 1-10 ثواني
        # blocking I/O زيادة، وأي فشل فيهم كان ممكن يدخل في retry logic
        # بتاع المعالجة كلها. دلوقتي tasks منفصلة بـ .delay():
        # - الاجتماع بيوصل لحالة "analyzed" فوراً
        # - فشل الإيميل/الـ webhook بيعاد لوحده بدون إعادة المعالجة كلها
        notification_payload = {
            "meeting_id":          meeting_id,
            "customer_name":       meeting.customer_name,
            "score":               scores_data["total_score"],
            "grade":               scores_data["grade"],
            "closing_probability": analysis_data["closing_probability"],
            "deal_stage":          analysis_data.get("deal_stage", ""),
            "customer_interest":   analysis_data["customer_interest"],
            "next_steps":          analysis_data.get("next_steps", [])[:3],
        }
        dispatch_webhook_task.delay("meeting.analyzed", notification_payload)
        if user and user.email:
            send_analysis_email_task.delay(user.email, {
                "meeting_id":          meeting_id,
                "customer_name":       meeting.customer_name,
                "score":               scores_data["total_score"],
                "grade":               scores_data["grade"],
                "closing_probability": analysis_data["closing_probability"],
            })

        return {"status": "success", "meeting_id": meeting_id, "score": scores_data["total_score"]}

    except Exception as exc:
        print(f"[{meeting_id}] ❌ Error: {exc}")
        # ✅ FIX (retry status): النسخة القديمة كانت بتعلّم الاجتماع "failed"
        # *قبل* الـ retry → الواجهة بتعرض فشل لاجتماع لسه بيتعالج، ولو
        # المحاولات خلصت كان بيتعدّى بصمت. دلوقتي:
        # - لسه فيه محاولات → status = "processing" + رسالة "إعادة محاولة"
        # - خلصت المحاولات → status = "failed" نهائي
        if self.request.retries < self.max_retries:
            _update_status(
                meeting_id, "processing",
                error=f"إعادة محاولة {self.request.retries + 1}/{self.max_retries}: {exc}",
            )
            raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))
        _update_status(meeting_id, "failed", error=str(exc))
        return {"status": "failed", "meeting_id": meeting_id, "error": str(exc)[:200]}

    finally:
        db.close()
        for path in [audio_path]:
            if path and os.path.exists(path) and "_audio.wav" in path:
                try:
                    os.remove(path)
                except Exception:
                    pass


# ══════════════════════════════════════════════════════
# PERSONAL WEAKNESS PATTERN — كل 5 اجتماعات
# ══════════════════════════════════════════════════════
@celery_app.task(name="generate_personal_pattern")
def generate_personal_pattern(user_id: int):
    """
    يحلل آخر 10 اجتماعات للمندوب ويستخرج:
    - نقطة الضعف المتكررة
    - DNA الاجتماع الناجح (لو في اجتماعات مغلقة)
    ويبعت الـ insights بالإيميل.
    """
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
        if not user or not user.email:
            return

        # آخر 10 اجتماعات محللة
        meetings = (
            db.query(Meeting)
            .filter(Meeting.user_id == user_id, Meeting.status == "analyzed")
            .order_by(Meeting.created_at.desc())
            .limit(10)
            .all()
        )

        if len(meetings) < 3:
            return

        # ── تحليل نقاط الضعف المتكررة ─────────────────
        score_components = {
            "listening_score":  [],
            "discovery_score":  [],
            "objection_score":  [],
            "next_steps_score": [],
            "closing_score":    [],
        }
        max_per = {
            "listening_score": 25, "discovery_score": 20,
            "objection_score": 25, "next_steps_score": 15, "closing_score": 15,
        }
        labels_ar = {
            "listening_score":  "الاستماع",
            "discovery_score":  "الاكتشاف",
            "objection_score":  "معالجة الاعتراضات",
            "next_steps_score": "الخطوات التالية",
            "closing_score":    "محاولة الإغلاق",
        }

        for m in meetings:
            if m.score:
                for key in score_components:
                    val = getattr(m.score, key, 0) or 0
                    pct = (val / max_per[key]) * 100
                    score_components[key].append(pct)

        # أضعف component
        avg_per_component = {
            k: round(sum(v) / len(v), 1)
            for k, v in score_components.items() if v
        }
        weakest_key   = min(avg_per_component, key=avg_per_component.get)
        weakest_label = labels_ar[weakest_key]
        weakest_avg   = avg_per_component[weakest_key]

        # ── DNA الاجتماعات الناجحة ─────────────────────
        successful = [
            m for m in meetings
            if m.analysis and m.analysis.deal_stage in ("closing", "won")
        ]

        dna = None
        if len(successful) >= 2:
            dna_durations = [m.duration_seconds / 60 for m in successful if m.duration_seconds]
            dna_ratios    = [m.analysis.talk_ratio for m in successful if m.analysis and m.analysis.talk_ratio]
            dna_scores    = [m.score.total_score for m in successful if m.score]

            dna = {
                "avg_duration_min": round(sum(dna_durations) / len(dna_durations), 1) if dna_durations else None,
                "avg_talk_ratio":   round(sum(dna_ratios)   / len(dna_ratios),    1) if dna_ratios    else None,
                "avg_score":        round(sum(dna_scores)   / len(dna_scores),    1) if dna_scores     else None,
                "count":            len(successful),
            }

        # ── Overall avg ────────────────────────────────
        all_scores = [m.score.total_score for m in meetings if m.score]
        overall_avg = round(sum(all_scores) / len(all_scores), 1) if all_scores else 0

        _send_pattern_email(user, weakest_label, weakest_avg, overall_avg, len(meetings), dna)
        print(f"📊 Pattern email sent to {user.email} | weakest={weakest_label} ({weakest_avg}%)")

    except Exception as e:
        print(f"❌ Pattern analysis error for user {user_id}: {e}")
    finally:
        db.close()


def _send_pattern_email(user, weakest_label, weakest_avg, overall_avg, meeting_count, dna):
    dna_html = ""
    if dna:
        dna_html = f"""
        <div style="background:#f0fdf4;border-radius:10px;padding:16px;margin-top:16px">
            <p style="font-weight:700;color:#166534;margin:0 0 8px">🏆 DNA اجتماعاتك الناجحة ({dna['count']} اجتماع):</p>
            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;text-align:center">
                {f'<div style="background:#fff;padding:10px;border-radius:8px"><div style="font-size:20px;font-weight:700;color:#16a34a">{dna["avg_duration_min"]}د</div><div style="font-size:11px;color:#64748b">متوسط المدة</div></div>' if dna['avg_duration_min'] else ''}
                {f'<div style="background:#fff;padding:10px;border-radius:8px"><div style="font-size:20px;font-weight:700;color:#2563eb">{dna["avg_talk_ratio"]}%</div><div style="font-size:11px;color:#64748b">نسبة الكلام</div></div>' if dna['avg_talk_ratio'] else ''}
                {f'<div style="background:#fff;padding:10px;border-radius:8px"><div style="font-size:20px;font-weight:700;color:#7c3aed">{dna["avg_score"]}/100</div><div style="font-size:11px;color:#64748b">متوسط التقييم</div></div>' if dna['avg_score'] else ''}
            </div>
        </div>"""

    html = f"""
    <div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;max-width:580px;margin:auto;background:#f8fafc;padding:24px;border-radius:16px">
        <div style="background:#1e3a5f;color:#fff;padding:24px;border-radius:12px;text-align:center;margin-bottom:20px">
            <div style="font-size:32px;margin-bottom:8px">🤖</div>
            <h2 style="margin:0;font-size:20px">تقرير المدرب الأسبوعي</h2>
            <p style="margin:8px 0 0;opacity:.8;font-size:14px">بناءً على آخر {meeting_count} اجتماعات</p>
        </div>

        <div style="background:#fff;border-radius:12px;padding:20px;margin-bottom:16px;border:1px solid #e2e8f0">
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
                <div style="width:48px;height:48px;border-radius:50%;background:#fef2f2;display:flex;align-items:center;justify-content:center;font-size:24px;flex-shrink:0">🎯</div>
                <div>
                    <p style="font-weight:700;color:#0f172a;margin:0">نقطة ضعفك الأكثر تكراراً</p>
                    <p style="color:#dc2626;font-size:13px;margin:4px 0 0">{weakest_label} — {weakest_avg:.0f}% من الدرجة الكاملة</p>
                </div>
            </div>
            <p style="color:#374151;font-size:13px;margin:0;line-height:1.6">
                في آخر {meeting_count} اجتماعات، <strong>{weakest_label}</strong> كانت أضعف نقطة عندك باستمرار.
                ركّز في الاجتماع القادم على تحسين هذا الجانب تحديداً.
            </p>
        </div>

        <div style="background:#fff;border-radius:12px;padding:20px;border:1px solid #e2e8f0;margin-bottom:16px">
            <p style="font-weight:700;color:#0f172a;margin:0 0 8px">📊 متوسط أدائك العام</p>
            <div style="display:flex;align-items:center;gap:12px">
                <div style="font-size:36px;font-weight:800;color:{'#16a34a' if overall_avg >= 70 else '#d97706' if overall_avg >= 50 else '#dc2626'}">{overall_avg}</div>
                <div style="flex:1">
                    <div style="height:8px;background:#f1f5f9;border-radius:99px;overflow:hidden">
                        <div style="height:100%;width:{overall_avg}%;background:{'#16a34a' if overall_avg >= 70 else '#d97706' if overall_avg >= 50 else '#dc2626'};border-radius:99px"></div>
                    </div>
                    <p style="font-size:12px;color:#64748b;margin:4px 0 0">من 100 | {'ممتاز' if overall_avg >= 70 else 'جيد' if overall_avg >= 50 else 'يحتاج تحسين'}</p>
                </div>
            </div>
        </div>

        {dna_html}

        <a href="{settings.FRONTEND_URL}/dashboard/analytics"
           style="display:block;background:#2563eb;color:#fff;text-align:center;padding:14px;border-radius:10px;text-decoration:none;font-weight:700;margin-top:16px">
            عرض التحليل الكامل →
        </a>
        <p style="color:#94a3b8;font-size:12px;text-align:center;margin-top:16px">Sales Intelligence — تقرير أسبوعي تلقائي</p>
    </div>
    """

    send_email([user.email], f"🤖 مدربك الأسبوعي — نقطة ضعفك: {weakest_label}", html)


# ══════════════════════════════════════════════════════
# WEEKLY PATTERN ANALYSIS — كل الأحد
# ══════════════════════════════════════════════════════
@celery_app.task(name="weekly_pattern_analysis")
def weekly_pattern_analysis():
    """يشغّل generate_personal_pattern لكل مندوب نشط."""
    db = SessionLocal()
    try:
        reps = db.query(User).filter(User.role == "sales", User.is_active == True).all()
        for rep in reps:
            # فقط لو عنده 5+ اجتماعات محللة
            count = db.query(Meeting).filter(
                Meeting.user_id == rep.id,
                Meeting.status  == "analyzed",
            ).count()
            if count >= 5:
                generate_personal_pattern.delay(rep.id)
        print(f"📊 Weekly pattern triggered for {len(reps)} reps")
    finally:
        db.close()


# ══════════════════════════════════════════════════════
# FOLLOW-UP REMINDERS
# ══════════════════════════════════════════════════════
@celery_app.task(name="send_followup_reminders")
def send_followup_reminders():
    db = SessionLocal()
    try:
        today = datetime.utcnow().date()
        sent  = []

        # ✅ FIX (scalability): النسخة القديمة كانت بتحمّل *كل* الاجتماعات
        # المحللة في التاريخ (.all() بلا حدود) وبتعمل db.query(User) لكل
        # اجتماع جوه الـ loop (N+1). دلوقتي:
        # - نافذة زمنية في SQL: follow_up_days أقصاها 60 يوم منطقياً،
        #   والتذكير بيغطي 0-3 أيام تأخير → processed_at خلال آخر 63 يوم فقط.
        # - الـ User بيتجاب بـ JOIN واحد بدل استعلام لكل اجتماع.
        window_start = datetime.utcnow() - timedelta(days=63)

        rows = (
            db.query(Meeting, User)
            .join(Analysis, Analysis.meeting_id == Meeting.id)
            .join(User, User.id == Meeting.user_id)
            .filter(
                Meeting.status == "analyzed",
                Analysis.follow_up_days.isnot(None),
                Meeting.processed_at >= window_start,
                User.is_active == True,
                User.email.isnot(None),
            )
            .all()
        )

        from ..utils.redis_client import redis_client
        for m, user in rows:
            if not m.analysis or not m.processed_at:
                continue
            follow_up_date = (m.processed_at + timedelta(days=m.analysis.follow_up_days)).date()
            days_overdue   = (today - follow_up_date).days
            if 0 <= days_overdue <= 3:
                reminder_key = f"followup_sent:{m.id}:{follow_up_date}"
                if redis_client.exists(reminder_key):
                    continue
                _send_followup_email(user, m, days_overdue)
                redis_client.setex(reminder_key, 86400 * 4, "1")
                sent.append(m.id)

        print(f"📅 Follow-up reminders: {len(sent)} sent")
        return {"sent": len(sent), "meetings": sent}
    finally:
        db.close()


def _send_followup_email(user, meeting, days_overdue):
    analysis = meeting.analysis
    score    = meeting.score
    overdue_text = "اليوم" if days_overdue == 0 else f"منذ {days_overdue} {'يوم' if days_overdue == 1 else 'أيام'}"
    color    = "#16a34a" if score and score.total_score >= 70 else "#d97706" if score and score.total_score >= 50 else "#dc2626"

    # Opening script لو موجود
    script_html = ""
    if analysis and analysis.opening_script:
        s = analysis.opening_script
        script_html = f"""
        <div style="background:#eff6ff;border-radius:10px;padding:16px;margin:16px 0">
            <p style="font-weight:700;color:#1e40af;margin:0 0 8px">💬 جمل الافتتاح المقترحة:</p>
            {''.join(f'<p style="font-size:13px;color:#1e40af;margin:0 0 6px">"{line}"</p>' for line in [s.get("line1",""), s.get("line2",""), s.get("line3","")] if line)}
            {f'<p style="font-size:11px;color:#64748b;margin:8px 0 0">💡 {s.get("tip","")}</p>' if s.get("tip") else ""}
        </div>"""

    next_steps_html = ""
    if analysis and analysis.next_steps:
        items = "".join(f"<li style='margin:4px 0;font-size:13px'>{s}</li>" for s in analysis.next_steps[:3])
        next_steps_html = f"""
        <div style="background:#f0fdf4;border-radius:10px;padding:16px;margin:16px 0">
            <p style="font-weight:700;color:#166534;margin:0 0 8px">✅ الخطوات المتفق عليها:</p>
            <ul style="margin:0;padding-right:20px;color:#166534">{items}</ul>
        </div>"""

    html = f"""
    <div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;max-width:600px;margin:auto;background:#f8fafc;padding:24px;border-radius:16px">
        <div style="background:#1e3a5f;color:#fff;padding:24px;border-radius:12px;text-align:center;margin-bottom:20px">
            <div style="font-size:32px;margin-bottom:8px">📅</div>
            <h2 style="margin:0;font-size:20px">تذكير متابعة</h2>
            <p style="margin:8px 0 0;opacity:.85;font-size:14px">{meeting.customer_name}</p>
        </div>
        <div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;padding:16px;margin-bottom:16px;text-align:center">
            <p style="margin:0;color:#c2410c;font-weight:700;font-size:15px">
                موعد المتابعة كان {overdue_text}
            </p>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
            <div style="background:#fff;padding:16px;border-radius:10px;text-align:center;border:1px solid #e2e8f0">
                <div style="font-size:36px;font-weight:700;color:{color}">{score.total_score if score else '—'}</div>
                <div style="color:#64748b;font-size:12px;margin-top:4px">تقييم الاجتماع / 100</div>
            </div>
            <div style="background:#fff;padding:16px;border-radius:10px;text-align:center;border:1px solid #e2e8f0">
                <div style="font-size:36px;font-weight:700;color:#7c3aed">{analysis.closing_probability if analysis else '—'}%</div>
                <div style="color:#64748b;font-size:12px;margin-top:4px">احتمالية الإغلاق</div>
            </div>
        </div>
        {script_html}
        {next_steps_html}
        <a href="{settings.FRONTEND_URL}/meetings/{meeting.id}"
           style="display:block;background:#2563eb;color:#fff;text-align:center;padding:14px;border-radius:10px;text-decoration:none;font-weight:700">
            عرض تقرير الاجتماع الكامل →
        </a>
        <p style="color:#94a3b8;font-size:12px;text-align:center;margin-top:16px">Sales Intelligence — تذكير تلقائي</p>
    </div>"""

    send_email([user.email], f"📅 تذكير: تابع مع {meeting.customer_name} — {overdue_text}", html)


# ══════════════════════════════════════════════════════
# DAILY REPORT + CLEANUP
# ══════════════════════════════════════════════════════
@celery_app.task(name="send_daily_report_task")
def send_daily_report_task():
    try:
        from ..services.report_service import report_service
        result = report_service.generate_and_send()
        print(f"📊 Daily report: {result}")
        return result
    except Exception as e:
        print(f"❌ Daily report error: {e}")


@celery_app.task(name="cleanup_temp_files")
def cleanup_temp_files():
    import time
    from pathlib import Path
    tmp_dir = Path(settings.LOCAL_STORAGE_PATH) / "tmp"
    if not tmp_dir.exists():
        return {"deleted": 0}

    # ✅ FIX (race مع R2): storage_service.get_local_path بينزّل ملفات R2
    # في tmp/ للمعالجة — مسح ملف عمره ساعة كان ممكن يضرب job لسه في
    # الطابور/شغال. دلوقتي:
    # - النافذة بقت 6 ساعات بدل ساعة.
    # - لو فيه أي اجتماع لسه في مرحلة معالجة نشطة، الملفات اللي اسمها
    #   بيطابق file_path بتاعه بتتعدّى.
    ACTIVE_STATUSES = ("uploaded", "processing", "transcribing", "validating", "analyzing")
    db = SessionLocal()
    try:
        active_names = {
            Path(fp).name
            for (fp,) in db.query(Meeting.file_path)
                           .filter(Meeting.status.in_(ACTIVE_STATUSES))
                           .all()
            if fp
        }
    finally:
        db.close()

    now     = time.time()
    deleted = 0
    for f in tmp_dir.iterdir():
        if not f.is_file():
            continue
        if f.name in active_names:
            continue  # لسه في الـ pipeline — سيبه
        if (now - f.stat().st_mtime) > 6 * 3600:
            f.unlink()
            deleted += 1
    print(f"🧹 Cleaned {deleted} temp files")
    return {"deleted": deleted}


# ════════════════════════════════════════════════════════
# ESCALATION CHECKS — يشتغل يومياً الساعة 9 صباحاً
# ════════════════════════════════════════════════════════
@celery_app.task(name="run_escalation_checks")
def run_escalation_checks():
    """
    يفحص كل الـ escalation rules ويبعت تنبيهات للمندوبين والمديرين.
    يشتغل كل يوم الساعة 9 صباحاً من الـ beat schedule.
    """
    from ..models.escalation import EscalationRule, SLASetting
    from ..models.analysis import Analysis
    from ..utils.redis_client import redis_client

    db = SessionLocal()
    try:
        rules = db.query(EscalationRule).filter(EscalationRule.active == True).all()
        if not rules:
            return {"checked": 0}

        # جيب كل المندوبين النشطين
        reps = db.query(User).filter(User.role == "sales", User.is_active == True).all()
        alerts_sent = 0

        for rep in reps:
            for rule in rules:
                # تحقق من Redis — لو بعتنا تنبيه لنفس المندوب بنفس القاعدة اليوم → تخطّ
                cache_key = f"escalation:{rule.id}:{rep.id}:{datetime.utcnow().date()}"
                if redis_client.exists(cache_key):
                    continue

                triggered = False
                context   = {}

                # ── no_meeting_days ─────────────────────────
                if rule.metric == "no_meeting_days":
                    cutoff = datetime.utcnow() - timedelta(days=int(rule.threshold))
                    last   = (
                        db.query(Meeting)
                        .filter(Meeting.user_id == rep.id, Meeting.created_at >= cutoff)
                        .first()
                    )
                    if not last:
                        triggered = True
                        context   = {"days": int(rule.threshold)}

                # ── score_drop ──────────────────────────────
                elif rule.metric == "score_drop":
                    from ..models.analysis import Score
                    recent_scores = (
                        db.query(Score.total_score)
                        .join(Meeting, Score.meeting_id == Meeting.id)
                        .filter(Meeting.user_id == rep.id)
                        .order_by(Meeting.created_at.desc())
                        .limit(5)
                        .all()
                    )
                    if len(recent_scores) >= 2:
                        scores = [s[0] for s in recent_scores]
                        drop   = scores[1] - scores[0]   # آخر - قبله
                        if drop >= rule.threshold:
                            triggered = True
                            context   = {"drop": round(drop, 1), "latest": scores[0]}

                # ── consecutive_losses ──────────────────────
                elif rule.metric == "consecutive_losses":
                    recent = (
                        db.query(Analysis.deal_stage)
                        .join(Meeting, Analysis.meeting_id == Meeting.id)
                        .filter(Meeting.user_id == rep.id)
                        .order_by(Meeting.created_at.desc())
                        .limit(int(rule.threshold))
                        .all()
                    )
                    if len(recent) >= int(rule.threshold):
                        if all(r[0] == "lost" for r in recent):
                            triggered = True
                            context   = {"count": int(rule.threshold)}

                # ── stale_pipeline ──────────────────────────
                elif rule.metric == "stale_pipeline":
                    cutoff = datetime.utcnow() - timedelta(days=int(rule.threshold))
                    stale  = (
                        db.query(Meeting)
                        .join(Analysis, Analysis.meeting_id == Meeting.id)
                        .filter(
                            Meeting.user_id    == rep.id,
                            Meeting.status     == "analyzed",
                            Analysis.deal_stage.notin_(["won", "lost"]),
                            Meeting.processed_at <= cutoff,
                        )
                        .count()
                    )
                    if stale > 0:
                        triggered = True
                        context   = {"stale_count": stale, "days": int(rule.threshold)}

                # ── low_score_streak ────────────────────────
                elif rule.metric == "low_score_streak":
                    from ..models.analysis import Score
                    recent = (
                        db.query(Score.total_score)
                        .join(Meeting, Score.meeting_id == Meeting.id)
                        .filter(Meeting.user_id == rep.id)
                        .order_by(Meeting.created_at.desc())
                        .limit(int(rule.threshold))
                        .all()
                    )
                    if len(recent) >= int(rule.threshold):
                        if all(s[0] < 50 for s in recent):
                            triggered = True
                            context   = {"count": int(rule.threshold)}

                # ── sla_breach ──────────────────────────────
                elif rule.metric == "sla_breach":
                    # يُغطّيه check_sla_breaches أدناه
                    pass

                if triggered:
                    _send_escalation_alert(rep, rule, context, db)
                    redis_client.setex(cache_key, 86400, "1")
                    alerts_sent += 1

        # ── SLA breach check ────────────────────────────────
        sla_alerts = _check_sla_breaches(db)
        alerts_sent += sla_alerts

        print(f"🚨 Escalation check complete — {alerts_sent} alerts sent")
        return {"checked": len(reps), "alerts": alerts_sent}

    except Exception as e:
        print(f"❌ Escalation check error: {e}")
    finally:
        db.close()


def _check_sla_breaches(db) -> int:
    """يفحص الصفقات الجامدة التي تجاوزت الـ SLA المضبوط."""
    from ..models.escalation import SLASetting

    sla_rows = db.query(SLASetting).all()
    if not sla_rows:
        # defaults
        sla_map = {"qualified": 7, "proposal": 14, "negotiation": 21, "closing": 7}
    else:
        sla_map = {r.stage: r.max_days for r in sla_rows}

    alerts = 0
    from ..utils.redis_client import redis_client

    for stage, max_days in sla_map.items():
        cutoff = datetime.utcnow() - timedelta(days=max_days)
        breached = (
            db.query(Meeting)
            .join(Analysis, Analysis.meeting_id == Meeting.id)
            .filter(
                Meeting.status      == "analyzed",
                Analysis.deal_stage == stage,
                Meeting.processed_at <= cutoff,
            )
            .all()
        )
        for meeting in breached:
            cache_key = f"sla_breach:{meeting.id}:{stage}:{datetime.utcnow().date()}"
            if redis_client.exists(cache_key):
                continue

            user = db.query(User).filter(User.id == meeting.user_id).first()
            if not user:
                continue

            days_stuck = (datetime.utcnow() - meeting.processed_at).days
            _send_sla_breach_email(user, meeting, stage, days_stuck, max_days)
            redis_client.setex(cache_key, 86400, "1")
            alerts += 1

    return alerts


def _send_escalation_alert(user, rule, context: dict, db):
    """بيبعت إيميل الـ escalation للمندوب و/أو المدير."""
    LEVEL_LABEL = {"L1": "تنبيه", "L2": "تحذير", "L3": "⚠️ حرج"}
    level_label = LEVEL_LABEL.get(rule.level, rule.level)

    METRIC_LABEL = {
        "no_meeting_days":    f"لم يرفع اجتماع منذ {context.get('days', rule.threshold)} يوم",
        "score_drop":         f"انخفض التقييم {context.get('drop', 0)} نقطة (آخر نتيجة: {context.get('latest', 0):.0f})",
        "consecutive_losses": f"{context.get('count', 0)} خسارات متتالية",
        "stale_pipeline":     f"{context.get('stale_count', 0)} صفقة جامدة منذ {context.get('days', 0)} يوم",
        "low_score_streak":   f"{context.get('count', 0)} اجتماعات متتالية أقل من 50 نقطة",
    }
    message = METRIC_LABEL.get(rule.metric, rule.metric)

    html = f"""
    <div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;max-width:560px;margin:auto;background:#f8fafc;padding:20px;border-radius:14px">
        <div style="background:#{'dc2626' if rule.level=='L3' else 'ea580c' if rule.level=='L2' else 'd97706'};color:#fff;padding:20px;border-radius:10px;text-align:center;margin-bottom:16px">
            <div style="font-size:28px;margin-bottom:6px">🚨</div>
            <h2 style="margin:0;font-size:18px">{level_label} تلقائي — {rule.name}</h2>
        </div>
        <div style="background:#fff;border-radius:10px;padding:16px;margin-bottom:12px;border:1px solid #e2e8f0">
            <p style="margin:0;font-size:14px;color:#374151"><strong>المندوب:</strong> {user.name}</p>
            <p style="margin:8px 0 0;font-size:14px;color:#374151"><strong>الحالة:</strong> {message}</p>
        </div>
        <a href="{settings.FRONTEND_URL}/admin/activity"
           style="display:block;background:#2563eb;color:#fff;text-align:center;padding:12px;border-radius:8px;text-decoration:none;font-weight:700">
            عرض نشاط الفريق ←
        </a>
        <p style="color:#94a3b8;font-size:11px;text-align:center;margin-top:12px">Sales Intelligence — تنبيه تلقائي | القاعدة: {rule.name}</p>
    </div>"""

    recipients = []
    if rule.action in ("email_rep", "email_both"):
        recipients.append(user.email)
    if rule.action in ("email_manager", "email_both"):
        # جيب المدير من الفريق
        if user.team_id:
            team = db.query(Team).filter(Team.id == user.team_id).first()
            if team and team.manager_id:
                mgr = db.query(User).filter(User.id == team.manager_id).first()
                if mgr and mgr.email:
                    recipients.append(mgr.email)

    if recipients:
        send_email(recipients, f"🚨 {level_label}: {user.name} — {rule.name}", html)


def _send_sla_breach_email(user, meeting, stage: str, days_stuck: int, max_days: int):
    STAGE_AR = {
        "qualified": "تأهيل", "proposal": "عرض سعر",
        "negotiation": "تفاوض", "closing": "إغلاق",
    }
    html = f"""
    <div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;max-width:560px;margin:auto;background:#f8fafc;padding:20px;border-radius:14px">
        <div style="background:#7c3aed;color:#fff;padding:20px;border-radius:10px;text-align:center;margin-bottom:16px">
            <div style="font-size:28px;margin-bottom:6px">⏱️</div>
            <h2 style="margin:0;font-size:18px">تجاوز SLA — صفقة جامدة</h2>
        </div>
        <div style="background:#fff;border-radius:10px;padding:16px;margin-bottom:12px;border:1px solid #e2e8f0">
            <p style="margin:0 0 8px;font-size:14px"><strong>المندوب:</strong> {user.name}</p>
            <p style="margin:0 0 8px;font-size:14px"><strong>العميل:</strong> {meeting.customer_name}</p>
            <p style="margin:0 0 8px;font-size:14px"><strong>المرحلة:</strong> {STAGE_AR.get(stage, stage)}</p>
            <p style="margin:0;font-size:14px;color:#dc2626"><strong>جامدة منذ:</strong> {days_stuck} يوم (الحد المسموح: {max_days} يوم)</p>
        </div>
        <a href="{settings.FRONTEND_URL}/meetings/{meeting.id}"
           style="display:block;background:#7c3aed;color:#fff;text-align:center;padding:12px;border-radius:8px;text-decoration:none;font-weight:700">
            عرض الاجتماع وتحديث المرحلة ←
        </a>
    </div>"""
    send_email([user.email], f"⏱️ صفقة جامدة: {meeting.customer_name} — {days_stuck} يوم في {STAGE_AR.get(stage, stage)}", html)


# تسجيل الـ task في الـ beat schedule
celery_app.conf.beat_schedule["run-escalation-checks"] = {
    "task":     "run_escalation_checks",
    "schedule": crontab(hour=9, minute=0),
}
