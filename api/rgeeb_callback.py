"""
api/rgeeb_callback.py — Rgeeb Post-Installation Webhook
=========================================================

بعد تثبيت Rgeeb عند عميل جديد، Rgeeb يبعت POST هنا
بيانات العميل والنظام → نحدّث deal_stage تلقائياً.

Flow:
  Rgeeb يثبّت النظام عند عميل
    ↓
  Rgeeb يبعت POST على /api/rgeeb/callback
    ↓
  نجيب آخر اجتماع مع نفس اسم الشركة أو الإيميل
    ↓
  نحدّث deal_stage = "won"
    ↓
  نحفظ بيانات التثبيت لقياس الـ ROI لاحقاً

لربط Rgeeb: تطلب منهم يضيفوا webhook URL في إعدادات التثبيت:
POST https://your-domain.com/api/rgeeb/callback
Header: X-Rgeeb-Secret: YOUR_SECRET
"""
import hmac
import hashlib
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import Meeting
from ..models.analysis import Analysis
from ..config import settings

router = APIRouter()


@router.post("/callback")
async def rgeeb_callback(request: Request, background: BackgroundTasks):
    """
    يستقبل webhook من Rgeeb بعد تثبيت النظام عند عميل.

    Expected payload من Rgeeb:
    {
        "event": "client.installed",
        "client": {
            "name":    "اسم الشركة",
            "email":   "مدير@شركة.com",
            "phone":   "05xxxxxxxx",
            "plan":    "basic|pro|enterprise",
            "cameras": 5
        },
        "installed_at": "2025-01-15T10:30:00Z",
        "installer_id": "rgeeb_sales_rep_id"
    }
    """
    body      = await request.body()
    signature = request.headers.get("x-rgeeb-secret", "")

    # تحقق من الـ secret (لو مضبوط)
    if settings.RGEEB_WEBHOOK_SECRET:
        expected = hmac.new(
            settings.RGEEB_WEBHOOK_SECRET.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(401, "Invalid Rgeeb signature")

    import json
    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    event = payload.get("event", "")
    if event != "client.installed":
        return {"message": f"Event {event} ignored"}

    background.add_task(_handle_rgeeb_install, payload)
    return {"message": "Received — updating pipeline"}


def _handle_rgeeb_install(payload: dict):
    """
    يبحث عن آخر اجتماع مع هذا العميل ويحدّث deal_stage = won.

    ✅ FIX: الدالة كانت async وبتعمل استعلامات DB متزامنة (blocking)
    على الـ event loop → كانت بتجمّد كل الـ requests أثناء التنفيذ.
    دلوقتي sync `def` → BackgroundTasks بتشغّلها في threadpool.
    """
    client       = payload.get("client", {})
    client_name  = client.get("name", "").strip()
    client_email = client.get("email", "").strip()
    installed_at = payload.get("installed_at", "")
    cameras      = client.get("cameras", 0)
    plan         = client.get("plan", "")

    if not client_name and not client_email:
        print("⚠️ Rgeeb callback: no client name or email")
        return

    db = SessionLocal()
    try:
        # ابحث عن الاجتماعات المحتملة بالاسم أو الإيميل
        query = db.query(Meeting).filter(Meeting.status == "analyzed")

        if client_name:
            # بحث جزئي في اسم العميل أو اسم الشركة
            from sqlalchemy import or_, func
            candidates = query.filter(
                or_(
                    func.lower(Meeting.customer_name).contains(client_name.lower()[:20]),
                    func.lower(Meeting.customer_company).contains(client_name.lower()[:20]),
                )
            ).order_by(Meeting.created_at.desc()).limit(5).all()
        else:
            candidates = []

        if not candidates:
            print(f"⚠️ Rgeeb callback: No meeting found for client '{client_name}'")
            # سنحفظ الحدث كمعلومة للمستقبل
            return

        # آخر اجتماع مع هذا العميل
        meeting = candidates[0]

        # تحديث deal_stage لـ "won"
        if meeting.analysis:
            old_stage = meeting.analysis.deal_stage
            meeting.analysis.deal_stage = "won"

            # حفظ بيانات التثبيت في meeting_signals للـ ROI analysis
            signals = meeting.analysis.meeting_signals or {}
            signals["rgeeb_install"] = {
                "installed_at": installed_at,
                "cameras":      cameras,
                "plan":         plan,
                "client_email": client_email,
                "auto_won_at":  datetime.utcnow().isoformat(),
            }
            meeting.analysis.meeting_signals = signals

            db.commit()
            print(f"✅ Rgeeb callback: Meeting #{meeting.id} → deal_stage won "
                  f"(was: {old_stage}) | client: {client_name} | {cameras} cameras | plan: {plan}")

            # بعت إشعار للمندوب
            from ..models import User
            from ..services.email_service import send_email

            user = db.query(User).filter(User.id == meeting.user_id).first()
            if user and user.email:
                html = f"""
                <div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;max-width:560px;margin:auto;background:#f8fafc;padding:20px;border-radius:14px">
                    <div style="background:linear-gradient(135deg,#16a34a,#15803d);color:#fff;padding:24px;border-radius:10px;text-align:center;margin-bottom:16px">
                        <div style="font-size:36px;margin-bottom:8px">🎉</div>
                        <h2 style="margin:0;font-size:20px">تم تثبيت Rgeeb عند العميل!</h2>
                        <p style="margin:8px 0 0;opacity:.9;font-size:14px">الصفقة تحوّلت إلى "فاز" تلقائياً</p>
                    </div>
                    <div style="background:#fff;border-radius:10px;padding:16px;border:1px solid #e2e8f0;margin-bottom:12px">
                        <p style="margin:0 0 8px;font-size:14px"><strong>العميل:</strong> {client_name}</p>
                        <p style="margin:0 0 8px;font-size:14px"><strong>عدد الكاميرات:</strong> {cameras}</p>
                        <p style="margin:0 0 8px;font-size:14px"><strong>الباقة:</strong> {plan}</p>
                        <p style="margin:0;font-size:14px;color:#16a34a"><strong>تاريخ التثبيت:</strong> {installed_at[:10] if installed_at else 'الآن'}</p>
                    </div>
                    <a href="{settings.FRONTEND_URL}/meetings/{meeting.id}"
                       style="display:block;background:#16a34a;color:#fff;text-align:center;padding:12px;border-radius:8px;text-decoration:none;font-weight:700">
                        عرض تقرير الاجتماع الكامل ←
                    </a>
                </div>"""
                send_email([user.email], f"🎉 تهانينا! {client_name} وقّع مع Rgeeb", html)

    except Exception as e:
        print(f"❌ Rgeeb callback error: {e}")
    finally:
        db.close()
