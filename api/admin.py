"""
api/admin.py — Admin endpoints مع Redis caching + Webhooks

Endpoints:
POST   /api/admin/users              → إنشاء مستخدم
GET    /api/admin/users              → قائمة المستخدمين
PUT    /api/admin/users/{id}         → تعديل
DELETE /api/admin/users/{id}         → تعطيل
POST   /api/admin/users/{id}/reset-password
GET    /api/admin/teams
POST   /api/admin/teams
GET    /api/admin/rejected
GET    /api/admin/activity           → مع Redis cache (5 دقائق)
GET    /api/admin/stats              → إحصائيات سريعة للداشبورد
GET    /api/admin/leaderboard        → ترتيب المندوبين
POST   /api/admin/webhooks           → إضافة webhook URL
GET    /api/admin/webhooks
DELETE /api/admin/webhooks/{id}
"""
import json
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, HttpUrl
from sqlalchemy.orm import Session
from sqlalchemy import or_

from ..database import get_db
from ..models.user import User, Team
from ..models.meeting import Meeting
from ..utils.auth import hash_password, require_role
from ..utils.redis_client import redis_client, cache_admin_result, invalidate_admin_cache

router     = APIRouter()
admin_only = require_role("admin")
mgr_or_admin = require_role("admin", "manager")


# ── Schemas ───────────────────────────────────────────
class CreateUserRequest(BaseModel):
    name:     str
    email:    EmailStr
    password: str
    role:     str
    team_id:  Optional[int] = None

class UpdateUserRequest(BaseModel):
    name:      Optional[str]      = None
    email:     Optional[EmailStr] = None
    role:      Optional[str]      = None
    team_id:   Optional[int]      = None
    is_active: Optional[bool]     = None

class ResetPasswordRequest(BaseModel):
    new_password: str

class CreateTeamRequest(BaseModel):
    name:       str
    manager_id: Optional[int] = None

class WebhookRequest(BaseModel):
    url:         str
    description: Optional[str] = None
    events:      list[str] = ["meeting.analyzed"]  # أنواع الأحداث


# ── POST /users ───────────────────────────────────────
@router.post("/users", status_code=201)
def create_user(
    body:   CreateUserRequest,
    db:     Session = Depends(get_db),
    _admin          = Depends(admin_only),
):
    if body.role not in ("sales", "manager", "admin"):
        raise HTTPException(400, "الدور يجب أن يكون: sales أو manager أو admin")
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(409, f"الإيميل {body.email} مستخدم بالفعل")
    if body.team_id and not db.query(Team).filter(Team.id == body.team_id).first():
        raise HTTPException(404, "الفريق غير موجود")

    user = User(
        name          = body.name,
        email         = body.email,
        password_hash = hash_password(body.password),
        role          = body.role,
        team_id       = body.team_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    invalidate_admin_cache()
    return {**user.to_dict(), "team_name": user.team.name if user.team else None}


# ── GET /users ────────────────────────────────────────
@router.get("/users")
def list_users(
    role:      Optional[str]  = None,
    team_id:   Optional[int]  = None,
    is_active: Optional[bool] = None,
    db:        Session        = Depends(get_db),
    _admin                    = Depends(admin_only),
):
    query = db.query(User)
    if role:
        query = query.filter(User.role == role)
    if team_id:
        query = query.filter(User.team_id == team_id)
    if is_active is not None:
        query = query.filter(User.is_active == is_active)

    users = query.order_by(User.created_at.desc()).all()
    return {
        "total": len(users),
        "users": [{**u.to_dict(), "team_name": u.team.name if u.team else None} for u in users],
    }


# ── PUT /users/{id} ───────────────────────────────────
@router.put("/users/{user_id}")
def update_user(
    user_id:      int,
    body:         UpdateUserRequest,
    db:           Session = Depends(get_db),
    current_admin         = Depends(admin_only),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "المستخدم غير موجود")
    if user_id == current_admin.id and body.is_active is False:
        raise HTTPException(400, "لا يمكنك تعطيل حسابك أنت")

    if body.name is not None:
        user.name = body.name
    if body.email is not None:
        if db.query(User).filter(User.email == body.email, User.id != user_id).first():
            raise HTTPException(409, "الإيميل مستخدم بالفعل")
        user.email = body.email
    if body.role is not None:
        if body.role not in ("sales", "manager", "admin"):
            raise HTTPException(400, "دور غير صالح")
        user.role = body.role
    if body.team_id is not None:
        user.team_id = body.team_id
    if body.is_active is not None:
        user.is_active = body.is_active

    db.commit()
    db.refresh(user)
    invalidate_admin_cache()
    return {**user.to_dict(), "team_name": user.team.name if user.team else None}


# ── DELETE /users/{id} ───────────────────────────────
@router.delete("/users/{user_id}")
def deactivate_user(
    user_id:      int,
    db:           Session = Depends(get_db),
    current_admin         = Depends(admin_only),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "المستخدم غير موجود")
    if user_id == current_admin.id:
        raise HTTPException(400, "لا يمكنك تعطيل حسابك أنت")
    user.is_active = False
    db.commit()
    invalidate_admin_cache()
    return {"message": f"تم تعطيل {user.name}"}


# ── POST /users/{id}/reset-password ──────────────────
@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    body:    ResetPasswordRequest,
    db:      Session = Depends(get_db),
    _admin           = Depends(admin_only),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "المستخدم غير موجود")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    return {"message": f"تم تغيير كلمة مرور {user.name}"}


# ── Teams ─────────────────────────────────────────────
@router.get("/teams")
def list_teams(db: Session = Depends(get_db), _=Depends(mgr_or_admin)):
    teams = db.query(Team).all()
    return {"teams": [{"id": t.id, "name": t.name, "manager_id": t.manager_id} for t in teams]}


@router.post("/teams", status_code=201)
def create_team(body: CreateTeamRequest, db: Session = Depends(get_db), _=Depends(admin_only)):
    team = Team(name=body.name, manager_id=body.manager_id)
    db.add(team)
    db.commit()
    db.refresh(team)
    return {"id": team.id, "name": team.name}


# ── GET /stats — إحصائيات سريعة ──────────────────────
@router.get("/stats")
def get_stats(
    days:   int     = 7,
    db:     Session = Depends(get_db),
    _               = Depends(mgr_or_admin),
):
    """
    إحصائيات سريعة للـ dashboard header.
    مع Redis cache 5 دقائق.

    ✅ PERF FIX: النسخة القديمة كانت بتحمّل *كل* الاجتماعات ثم تمشي على
    m.score و m.analysis (lazy-load = استعلام لكل علاقة). دلوقتي 3 استعلامات
    aggregate ثابتة مهما كان حجم البيانات.
    """
    cache_key = f"admin:stats:{days}"
    cached    = redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    from sqlalchemy import func, case
    from ..models.analysis import Score as ScoreModel, Analysis as AnalysisModel

    since = datetime.utcnow() - timedelta(days=days)

    # استعلام واحد: العدّ حسب الحالة
    counts = (
        db.query(
            func.count(Meeting.id),
            func.sum(case((Meeting.status == "analyzed", 1), else_=0)),
            func.sum(case((Meeting.status == "rejected", 1), else_=0)),
            func.sum(case((Meeting.status.in_(("analyzed", "failed", "rejected")), 0), else_=1)),
        )
        .filter(Meeting.created_at >= since)
        .one()
    )
    total      = counts[0] or 0
    analyzed   = int(counts[1] or 0)
    rejected   = int(counts[2] or 0)
    processing = int(counts[3] or 0)

    avg_score = (
        db.query(func.avg(ScoreModel.total_score))
        .join(Meeting, Meeting.id == ScoreModel.meeting_id)
        .filter(Meeting.created_at >= since)
        .scalar()
    )
    avg_prob = (
        db.query(func.avg(AnalysisModel.closing_probability))
        .join(Meeting, Meeting.id == AnalysisModel.meeting_id)
        .filter(Meeting.created_at >= since)
        .scalar()
    )

    result = {
        "days":         days,
        "total":        total,
        "analyzed":     analyzed,
        "rejected":     rejected,
        "processing":   processing,
        "analysis_rate": round(analyzed / total * 100) if total else 0,
        "avg_score":    round(float(avg_score), 1) if avg_score is not None else 0,
        "avg_closing_prob": round(float(avg_prob), 1) if avg_prob is not None else 0,
    }

    cache_admin_result(cache_key, json.dumps(result))
    return result


# ── GET /leaderboard — ترتيب المندوبين ───────────────
@router.get("/leaderboard")
def get_leaderboard(
    days:   int     = 30,
    metric: str     = "avg_score",  # avg_score | total | closing_prob
    db:     Session = Depends(get_db),
    _               = Depends(mgr_or_admin),
):
    """
    ترتيب المندوبين حسب الأداء مع Redis cache.
    """
    cache_key = f"admin:leaderboard:{days}:{metric}"
    cached    = redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    # ✅ PERF FIX (N+1): النسخة القديمة كانت بتعمل استعلام Meeting لكل مندوب
    # ثم lazy-load لـ score و analysis لكل اجتماع — 50 مندوب × 200 اجتماع
    # ≈ 10,000+ استعلام لكل cache miss. دلوقتي: استعلام JOIN + GROUP BY واحد.
    from sqlalchemy import func
    from ..models.analysis import Score as ScoreModel, Analysis as AnalysisModel

    since = datetime.utcnow() - timedelta(days=days)

    rows = (
        db.query(
            User.id,
            User.name,
            User.team_id,
            func.count(Meeting.id).label("total"),
            func.avg(ScoreModel.total_score).label("avg_score"),
            func.max(ScoreModel.total_score).label("best_score"),
            func.avg(AnalysisModel.closing_probability).label("avg_prob"),
        )
        .join(Meeting, Meeting.user_id == User.id)
        .outerjoin(ScoreModel, ScoreModel.meeting_id == Meeting.id)
        .outerjoin(AnalysisModel, AnalysisModel.meeting_id == Meeting.id)
        .filter(
            User.role == "sales",
            User.is_active == True,
            Meeting.status == "analyzed",
            Meeting.created_at >= since,
        )
        .group_by(User.id, User.name, User.team_id)
        .all()
    )

    # أسماء الفرق في استعلام واحد بدل lazy-load لكل صف
    team_ids   = {r.team_id for r in rows if r.team_id}
    team_names = {}
    if team_ids:
        team_names = dict(
            db.query(Team.id, Team.name).filter(Team.id.in_(team_ids)).all()
        )

    board = [
        {
            "user_id":    r.id,
            "name":       r.name,
            "team_name":  team_names.get(r.team_id),
            "total":      r.total,
            "avg_score":  round(float(r.avg_score), 1) if r.avg_score is not None else 0,
            "best_score": float(r.best_score) if r.best_score is not None else 0,
            "avg_closing_prob": round(float(r.avg_prob), 1) if r.avg_prob is not None else 0,
        }
        for r in rows
    ]

    # ترتيب حسب الـ metric المختار
    sort_key = metric if metric in ("avg_score", "total", "avg_closing_prob") else "avg_score"
    board.sort(key=lambda r: r[sort_key], reverse=True)

    # إضافة الترتيب
    for i, rep in enumerate(board, 1):
        rep["rank"] = i
        if i == 1:
            rep["badge"] = "🥇"
        elif i == 2:
            rep["badge"] = "🥈"
        elif i == 3:
            rep["badge"] = "🥉"
        else:
            rep["badge"] = None

    result = {"days": days, "metric": metric, "reps": board}
    cache_admin_result(cache_key, json.dumps(result))
    return result


# ── GET /activity — مع Redis cache ───────────────────
@router.get("/activity")
def get_activity(
    days:   int     = 30,
    db:     Session = Depends(get_db),
    _               = Depends(mgr_or_admin),
):
    """نشاط كل مندوب — مع Redis cache 5 دقائق."""
    cache_key = f"admin:activity:{days}"
    cached    = redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    since = datetime.utcnow() - timedelta(days=days)
    reps  = db.query(User).filter(User.role == "sales", User.is_active == True).all()

    # ✅ PERF FIX (N+1): استعلام Meeting واحد لكل المندوبين دفعة واحدة
    # (مع eager-load للـ score) بدل استعلام لكل مندوب + lazy-load لكل score.
    from sqlalchemy.orm import selectinload
    rep_ids = [r.id for r in reps]
    all_meetings = []
    if rep_ids:
        all_meetings = (
            db.query(Meeting)
            .options(selectinload(Meeting.score))
            .filter(Meeting.user_id.in_(rep_ids), Meeting.created_at >= since)
            .all()
        )
    meetings_by_rep: dict[int, list] = {}
    for m in all_meetings:
        meetings_by_rep.setdefault(m.user_id, []).append(m)

    result_reps = []

    for rep in reps:
        meetings = meetings_by_rep.get(rep.id, [])

        total_uploaded = len(meetings)
        total_analyzed = sum(1 for m in meetings if m.status == "analyzed")
        total_rejected = sum(1 for m in meetings if m.status == "rejected")
        total_failed   = sum(1 for m in meetings if m.status == "failed")

        daily: dict[str, int] = {}
        for m in meetings:
            day = m.created_at.strftime("%Y-%m-%d")
            daily[day] = daily.get(day, 0) + 1

        daily_full = []
        for i in range(days):
            d = (datetime.utcnow() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
            daily_full.append({"date": d, "count": daily.get(d, 0)})

        hourly = {h: 0 for h in range(24)}
        for m in meetings:
            hourly[m.created_at.hour] += 1
        hourly_list = [{"hour": h, "count": hourly[h]} for h in range(24)]
        peak_hour   = max(hourly, key=lambda h: hourly[h]) if meetings else None

        DAYS_AR = ["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
        weekday_counts = {i: 0 for i in range(7)}
        for m in meetings:
            weekday_counts[m.created_at.weekday()] += 1
        weekday_list = [{"day": DAYS_AR[i], "day_num": i, "count": weekday_counts[i]} for i in range(7)]

        scores = [m.score.total_score for m in meetings if m.score and m.status == "analyzed"]
        avg_score  = round(sum(scores) / len(scores), 1) if scores else None
        best_score = max(scores) if scores else None

        last_upload = max(m.created_at for m in meetings).isoformat() if meetings else None
        durations   = [m.duration_seconds for m in meetings if m.duration_seconds]
        avg_duration_min = round(sum(durations) / len(durations) / 60, 1) if durations else None
        active_days = sorted(set(m.created_at.strftime("%Y-%m-%d") for m in meetings))
        streak      = _calc_streak(active_days)

        result_reps.append({
            "user_id":          rep.id,
            "name":             rep.name,
            "email":            rep.email,
            "team_name":        rep.team.name if rep.team else None,
            "total_uploaded":   total_uploaded,
            "total_analyzed":   total_analyzed,
            "total_rejected":   total_rejected,
            "total_failed":     total_failed,
            "total_pending":    total_uploaded - total_analyzed - total_rejected - total_failed,
            "analysis_rate":    round(total_analyzed / total_uploaded * 100) if total_uploaded else 0,
            "rejection_rate":   round(total_rejected / total_uploaded * 100) if total_uploaded else 0,
            "avg_score":        avg_score,
            "best_score":       best_score,
            "avg_duration_min": avg_duration_min,
            "daily":            daily_full,
            "hourly":           hourly_list,
            "weekday":          weekday_list,
            "peak_hour":        peak_hour,
            "active_days_count": len(active_days),
            "streak_days":      streak,
            "last_upload":      last_upload,
        })

    result_reps.sort(key=lambda r: r["total_uploaded"], reverse=True)
    result = {"days": days, "reps": result_reps}
    cache_admin_result(cache_key, json.dumps(result, default=str))
    return result


# ── GET /rejected ─────────────────────────────────────
@router.get("/rejected")
def list_rejected(db: Session = Depends(get_db), _=Depends(mgr_or_admin)):
    meetings = (
        db.query(Meeting)
        .filter(Meeting.status == "rejected")
        .order_by(Meeting.created_at.desc())
        .limit(100)
        .all()
    )
    result = []
    for m in meetings:
        signals = {}
        rejection_reason = m.error_message or "غير محدد"
        try:
            parsed = json.loads(m.error_message or "{}")
            signals          = parsed.get("signals", {})
            rejection_reason = parsed.get("rejection_reason", m.error_message)
        except Exception:
            pass
        user = db.query(User).filter(User.id == m.user_id).first()
        result.append({
            "meeting_id":       m.id,
            "user_name":        user.name if user else "غير معروف",
            "user_email":       user.email if user else "",
            "customer_name":    m.customer_name,
            "file_size_mb":     m.file_size_mb,
            "duration_seconds": m.duration_seconds,
            "rejection_reason": rejection_reason,
            "signals":          signals,
            "created_at":       m.created_at.isoformat() if m.created_at else None,
        })
    return {"total": len(result), "meetings": result}


# ── Webhooks ──────────────────────────────────────────
WEBHOOK_STORE_KEY = "admin:webhooks"

@router.get("/webhooks")
def list_webhooks(_=Depends(admin_only)):
    """جيب كل الـ webhooks المسجلة."""
    raw = redis_client.get(WEBHOOK_STORE_KEY)
    webhooks = json.loads(raw) if raw else []
    return {"webhooks": webhooks}


@router.post("/webhooks", status_code=201)
def add_webhook(body: WebhookRequest, _=Depends(admin_only)):
    """
    أضف webhook URL. النظام هيبعت POST لهذا الـ URL
    عند كل تحليل مكتمل بـ JSON يحتوي على نتائج الاجتماع.
    """
    # ✅ SECURITY FIX (SSRF): نرفض أي URL مش https أو يتحلّ لـ IP داخلي
    # (loopback / private / link-local / metadata). الفحص بيتكرر برضه
    # قبل كل إرسال في webhook_service (الـ DNS ممكن يتغير بعد التسجيل).
    from ..utils.url_safety import is_safe_webhook_url
    safe, reason = is_safe_webhook_url(body.url)
    if not safe:
        raise HTTPException(400, f"Webhook URL مرفوض: {reason}")

    raw      = redis_client.get(WEBHOOK_STORE_KEY)
    webhooks = json.loads(raw) if raw else []

    new_hook = {
        "id":          len(webhooks) + 1,
        "url":         body.url,
        "description": body.description or "",
        "events":      body.events,
        "created_at":  datetime.utcnow().isoformat(),
        "active":      True,
    }
    webhooks.append(new_hook)
    redis_client.set(WEBHOOK_STORE_KEY, json.dumps(webhooks))
    return new_hook


@router.delete("/webhooks/{webhook_id}")
def delete_webhook(webhook_id: int, _=Depends(admin_only)):
    """حذف webhook."""
    raw      = redis_client.get(WEBHOOK_STORE_KEY)
    webhooks = json.loads(raw) if raw else []
    webhooks = [w for w in webhooks if w["id"] != webhook_id]
    redis_client.set(WEBHOOK_STORE_KEY, json.dumps(webhooks))
    return {"message": "تم الحذف"}


def _calc_streak(active_days: list[str]) -> int:
    if not active_days:
        return 0
    today   = datetime.utcnow().strftime("%Y-%m-%d")
    streak  = 0
    current = today
    day_set = set(active_days)
    while current in day_set:
        streak += 1
        current = (datetime.strptime(current, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    return streak


# ════════════════════════════════════════════════════════
# ESCALATION RULES
# ════════════════════════════════════════════════════════
from ..models.escalation import EscalationRule, SLASetting
from ..models.analysis import Analysis
from ..models.user import User as UserModel

class EscalationRuleRequest(BaseModel):
    name:      str
    metric:    str
    threshold: float = 7
    level:     str   = "L1"
    action:    str   = "email_manager"

class SLARequest(BaseModel):
    sla: dict[str, int]   # {"qualified": 7, "proposal": 14, ...}


@router.get("/escalation-rules")
def list_escalation_rules(
    db: Session = Depends(get_db),
    _=Depends(mgr_or_admin),
):
    rules = db.query(EscalationRule).order_by(EscalationRule.created_at.desc()).all()
    return {"rules": [r.to_dict() for r in rules]}


@router.post("/escalation-rules", status_code=201)
def create_escalation_rule(
    body: EscalationRuleRequest,
    db:   Session = Depends(get_db),
    current_user=Depends(mgr_or_admin),
):
    valid_metrics = {
        "no_meeting_days", "score_drop", "consecutive_losses",
        "stale_pipeline", "low_score_streak", "sla_breach",
    }
    if body.metric not in valid_metrics:
        raise HTTPException(400, f"metric غير صالح. المتاح: {', '.join(valid_metrics)}")
    if body.level not in ("L1", "L2", "L3"):
        raise HTTPException(400, "level يجب أن يكون L1 أو L2 أو L3")

    rule = EscalationRule(
        name       = body.name.strip(),
        metric     = body.metric,
        threshold  = body.threshold,
        level      = body.level,
        action     = body.action,
        created_by = current_user.id,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule.to_dict()


@router.patch("/escalation-rules/{rule_id}")
def toggle_escalation_rule(
    rule_id: int,
    body:    dict,
    db:      Session = Depends(get_db),
    _=Depends(mgr_or_admin),
):
    rule = db.query(EscalationRule).filter(EscalationRule.id == rule_id).first()
    if not rule:
        raise HTTPException(404, "القاعدة غير موجودة")
    if "active" in body:
        rule.active = bool(body["active"])
    db.commit()
    return rule.to_dict()


@router.delete("/escalation-rules/{rule_id}")
def delete_escalation_rule(
    rule_id: int,
    db:      Session = Depends(get_db),
    _=Depends(admin_only),
):
    rule = db.query(EscalationRule).filter(EscalationRule.id == rule_id).first()
    if not rule:
        raise HTTPException(404, "القاعدة غير موجودة")
    db.delete(rule)
    db.commit()
    return {"deleted": rule_id}


# ════════════════════════════════════════════════════════
# SLA SETTINGS
# ════════════════════════════════════════════════════════
DEFAULT_SLA = {
    "qualified":   7,
    "proposal":    14,
    "negotiation": 21,
    "closing":     7,
}

@router.get("/sla-settings")
def get_sla_settings(
    db: Session = Depends(get_db),
    _=Depends(mgr_or_admin),
):
    rows = db.query(SLASetting).all()
    if not rows:
        # أرجع الـ defaults لو مفيش إعدادات بعد
        return {"sla": DEFAULT_SLA, "is_default": True}
    return {"sla": {r.stage: r.max_days for r in rows}, "is_default": False}


@router.post("/sla-settings")
def update_sla_settings(
    body: SLARequest,
    db:   Session = Depends(get_db),
    _=Depends(mgr_or_admin),
):
    valid_stages = {"qualified", "proposal", "negotiation", "closing"}
    for stage, days in body.sla.items():
        if stage not in valid_stages:
            raise HTTPException(400, f"stage غير صالح: {stage}")
        if not (1 <= days <= 365):
            raise HTTPException(400, f"max_days يجب بين 1 و365 للـ stage: {stage}")

    for stage, days in body.sla.items():
        row = db.query(SLASetting).filter(SLASetting.stage == stage).first()
        if row:
            row.max_days = days
        else:
            db.add(SLASetting(stage=stage, max_days=days))
    db.commit()

    rows = db.query(SLASetting).all()
    return {"sla": {r.stage: r.max_days for r in rows}, "message": "تم الحفظ"}


# ════════════════════════════════════════════════════════
# ESCALATION CHECK — يُستدعى من Celery beat يومياً
# ════════════════════════════════════════════════════════
@router.post("/run-escalation-check")
def run_escalation_check_now(
    db: Session = Depends(get_db),
    _=Depends(admin_only),
):
    """تشغيل فحص الـ escalation يدوياً (للاختبار)."""
    from ..workers.tasks import run_escalation_checks
    run_escalation_checks.delay()
    return {"message": "Escalation check started in background"}



# ── GET /admin/meetings — كل اجتماعات كل المندوبين ────
@router.get("/meetings")
def list_all_meetings(
    page:       int           = 1,
    limit:      int           = 20,
    status:     Optional[str] = None,
    q:          Optional[str] = None,
    rep_id:     Optional[int] = None,
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    score_min:  Optional[int] = None,
    score_max:  Optional[int] = None,
    db:         Session       = Depends(get_db),
    _                         = Depends(mgr_or_admin),
):
    """
    كل اجتماعات كل المندوبين — للمدير والأدمن فقط.
    يدعم: فلتر بالحالة، البحث، المندوب، التاريخ، التقييم.
    """
    from sqlalchemy.orm import joinedload
    from ..models import Score, Analysis

    query = (
        db.query(Meeting)
        .options(
            joinedload(Meeting.score),
            joinedload(Meeting.analysis),
            joinedload(Meeting.user),
        )
    )

    if status:
        query = query.filter(Meeting.status == status)

    if rep_id:
        query = query.filter(Meeting.user_id == rep_id)

    if q and q.strip():
        term = f"%{q.strip()}%"
        # بنجوين مع User عشان نبحث في اسم المندوب كمان
        query = (
            query.join(User, User.id == Meeting.user_id)
            .filter(
                or_(
                    Meeting.customer_name.ilike(term),
                    Meeting.customer_company.ilike(term),
                    Meeting.meeting_title.ilike(term),
                    User.name.ilike(term),
                )
            )
        )

    if date_from:
        try:
            from datetime import datetime
            query = query.filter(Meeting.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
        except ValueError:
            pass

    if date_to:
        try:
            from datetime import datetime, timedelta
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

    def meeting_dict(m: Meeting):
        d = m.to_dict()
        # أضف بيانات المندوب
        if m.user:
            d["user"] = {
                "id":    m.user.id,
                "name":  m.user.name,
                "email": m.user.email,
            }
        return d

    return {
        "total":    total,
        "page":     page,
        "pages":    (total + limit - 1) // limit,
        "meetings": [meeting_dict(m) for m in items],
    }


# ── GET /admin/meetings/export — CSV لكل الاجتماعات ──
@router.get("/meetings/export")
def export_all_meetings(
    status:    Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    rep_id:    Optional[int] = None,
    db:        Session       = Depends(get_db),
    _                        = Depends(mgr_or_admin),
):
    import csv, io
    from datetime import datetime, timedelta
    from fastapi.responses import StreamingResponse

    query = db.query(Meeting).join(User, User.id == Meeting.user_id)

    if status:
        query = query.filter(Meeting.status == status)
    if rep_id:
        query = query.filter(Meeting.user_id == rep_id)
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
        "ID", "المندوب", "العميل", "الشركة", "عنوان الاجتماع",
        "التاريخ", "المدة (دقيقة)", "الحالة",
        "التقييم /100", "الدرجة", "احتمالية الإغلاق %",
        "اهتمام العميل", "مرحلة الصفقة",
    ])

    for m in meetings:
        sc = m.score
        an = m.analysis
        writer.writerow([
            m.id,
            m.user.name if m.user else "",
            m.customer_name,
            m.customer_company or "",
            m.meeting_title or "",
            m.created_at.strftime("%Y-%m-%d") if m.created_at else "",
            round(m.duration_seconds / 60) if m.duration_seconds else "",
            m.status,
            sc.total_score if sc else "",
            sc.grade       if sc else "",
            an.closing_probability if an else "",
            an.customer_interest   if an else "",
            an.deal_stage          if an else "",
        ])

    output.seek(0)
    filename = f"all_meetings_{datetime.now().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
