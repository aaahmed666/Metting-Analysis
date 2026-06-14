"""
api/auth.py — Auth endpoints
- JWT في HttpOnly Cookie
- Reset tokens في Redis
- Rate limiting على login
- Password strength validation
"""
import re
import secrets
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Request, Response
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from typing import Optional
from slowapi import Limiter
from slowapi.util import get_remote_address

from ..database import get_db
from ..models import User, Team
from ..utils.auth import hash_password, verify_password, create_token, get_current_user
from ..utils.redis_client import redis_client
from ..config import settings

router  = APIRouter()
limiter = Limiter(key_func=get_remote_address)

COOKIE_NAME    = "access_token"
COOKIE_MAX_AGE = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60


# ── Password strength ─────────────────────────────────
def _validate_password(password: str) -> None:
    """
    يتحقق من قوة كلمة المرور:
    - 8 أحرف على الأقل
    - حرف كبير واحد على الأقل
    - رقم واحد على الأقل
    """
    errors = []
    if len(password) < 8:
        errors.append("8 أحرف على الأقل")
    if not re.search(r"[A-Z]", password):
        errors.append("حرف كبير (A-Z) واحد على الأقل")
    if not re.search(r"\d", password):
        errors.append("رقم واحد على الأقل")
    if errors:
        raise HTTPException(
            status_code=400,
            detail=f"كلمة المرور ضعيفة — يجب أن تحتوي على: {' | '.join(errors)}",
        )


# ── Schemas ───────────────────────────────────────────
class LoginRequest(BaseModel):
    email:    str
    password: str

class RegisterRequest(BaseModel):
    # ✅ SECURITY FIX: role و team_id اتشالوا من الـ schema نهائياً.
    # النسخة القديمة كانت بتقبل role من الـ body وتكتبه زي ما هو —
    # يعني أي حد بـ curl يعمل POST {"role": "admin"} ياخد حساب أدمن
    # بدون أي مصادقة (privilege escalation كامل). الـ API هو حدود الثقة،
    # مش الفرونت إند. تعيين الأدوار متاح للأدمن فقط عبر POST /api/admin/users.
    name:     str
    email:    EmailStr
    password: str

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    token:        str
    new_password: str

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


# ── Cookie helpers ────────────────────────────────────
def _set_auth_cookie(response: Response, token: str):
    is_prod = settings.ENVIRONMENT == "production"
    response.set_cookie(
        key="access_token", value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True, secure=is_prod,
        samesite="lax", path="/",
    )

def _clear_auth_cookie(response: Response):
    response.delete_cookie("access_token", path="/")


# ── POST /login ───────────────────────────────────────
@router.post("/login")
@limiter.limit("10/minute")
async def login(
    request:  Request,
    req:      LoginRequest,
    response: Response,
    db:       Session = Depends(get_db),
):
    user = db.query(User).filter(
        User.email == req.email, User.is_active == True
    ).first()

    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "الإيميل أو كلمة المرور غلط")

    user.last_login = datetime.utcnow()
    db.commit()

    token = create_token(user.id, user.role)
    _set_auth_cookie(response, token)

    return {"message": "تم تسجيل الدخول", "user": user.to_dict(), "access_token": token, "token_type": "bearer"}


# ── POST /logout ──────────────────────────────────────
@router.post("/logout")
def logout(response: Response):
    _clear_auth_cookie(response)
    return {"message": "تم تسجيل الخروج"}


# ── POST /register ────────────────────────────────────
@router.post("/register")
def register(
    req:      RegisterRequest,
    response: Response,
    db:       Session = Depends(get_db),
):
    _validate_password(req.password)

    if db.query(User).filter(User.email == req.email).first():
        raise HTTPException(400, "الإيميل مستخدم بالفعل")

    user = User(
        name          = req.name,
        email         = req.email,
        password_hash = hash_password(req.password),
        role          = "sales",   # ✅ ثابت — لا نثق أبداً في role من العميل
        team_id       = None,      # ✅ الفريق يحدده الأدمن لاحقاً
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_token(user.id, user.role)
    _set_auth_cookie(response, token)
    return {"message": "تم إنشاء الحساب", "user": user.to_dict(), "access_token": token, "token_type": "bearer"}


# ── POST /forgot-password ─────────────────────────────
@router.post("/forgot-password")
@limiter.limit("3/minute")
async def forgot_password(
    request: Request,
    req:     ForgotPasswordRequest,
    db:      Session = Depends(get_db),
):
    user = db.query(User).filter(
        User.email == req.email, User.is_active == True
    ).first()

    if user:
        token = secrets.token_urlsafe(32)
        redis_client.setex(f"reset_token:{token}", 3600, str(user.id))

        reset_link = f"{settings.FRONTEND_URL}/reset-password?token={token}"

        if settings.RESEND_API_KEY:
            try:
                import httpx
                with httpx.Client(timeout=15) as client:
                    client.post(
                        "https://api.resend.com/emails",
                        headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}", "Content-Type": "application/json"},
                        json={
                            "from": settings.FROM_EMAIL,
                            "to": user.email,
                            "subject": "إعادة تعيين كلمة المرور — Sales Intelligence",
                            "html": f"""
                            <div dir="rtl" style="font-family:Tahoma,sans-serif;max-width:500px;margin:auto">
                                <h2>إعادة تعيين كلمة المرور</h2>
                                <p>مرحباً {user.name}، الرابط صالح لساعة واحدة.</p>
                                <a href="{reset_link}" style="background:#2563eb;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;display:inline-block;margin:16px 0">
                                    إعادة تعيين كلمة المرور
                                </a>
                                <p style="color:#666;font-size:13px">إذا لم تطلب هذا، تجاهل الرسالة.</p>
                            </div>""",
                        },
                    )
            except Exception as e:
                print(f"Email error: {e}")

    return {"message": "إذا كان الإيميل موجود، ستصلك رسالة خلال دقائق"}


# ── POST /reset-password ──────────────────────────────
@router.post("/reset-password")
def reset_password(req: ResetPasswordRequest, db: Session = Depends(get_db)):
    user_id = redis_client.get(f"reset_token:{req.token}")
    if not user_id:
        raise HTTPException(400, "الرابط غير صالح أو منتهي الصلاحية")

    _validate_password(req.new_password)

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(404, "المستخدم غير موجود")

    user.password_hash = hash_password(req.new_password)
    db.commit()
    redis_client.delete(f"reset_token:{req.token}")
    return {"message": "تم تغيير كلمة المرور بنجاح"}


# ── GET /me ───────────────────────────────────────────
@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    return current_user.to_dict()


class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
 
@router.patch("/me")
def update_me(
    body:         UpdateProfileRequest,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    """تحديث بيانات المستخدم الحالي — الاسم فقط."""
    if body.name:
        body.name = body.name.strip()
        if len(body.name) < 2:
            raise HTTPException(400, "الاسم يجب أن يكون حرفين على الأقل")
        if len(body.name) > 100:
            raise HTTPException(400, "الاسم طويل جداً")
        current_user.name = body.name
 
    db.commit()
    db.refresh(current_user)
    return current_user.to_dict()


# ── POST /change-password ─────────────────────────────
@router.post("/change-password")
def change_password(
    req:          ChangePasswordRequest,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    if not verify_password(req.old_password, current_user.password_hash):
        raise HTTPException(400, "كلمة المرور الحالية غلط")

    _validate_password(req.new_password)

    current_user.password_hash = hash_password(req.new_password)
    db.commit()
    return {"message": "تم تغيير كلمة المرور"}
