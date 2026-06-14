"""services/email_service.py — إرسال الإيميلات بـ Resend.com"""
import httpx
from ..config import settings


def send_email(to: list[str], subject: str, html_content: str) -> bool:
    if not settings.RESEND_API_KEY or not to:
        print(f"⚠️ Email skipped (no API key or recipients)")
        return False

    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": settings.FROM_EMAIL,
                    "to":   to,
                    "subject": subject,
                    "html":    html_content,
                },
            )
            success = r.status_code in (200, 201)
            if success:
                print(f"📧 Email sent to {len(to)} recipients")
            else:
                print(f"❌ Email failed: {r.text}")
            return success
    except Exception as e:
        print(f"❌ Email error: {e}")
        return False


def send_analysis_ready(user_email: str, meeting_data: dict) -> bool:
    """إشعار المندوب باكتمال التحليل."""
    score      = meeting_data.get("score", 0)
    color      = "#22c55e" if score >= 70 else "#f97316" if score >= 50 else "#ef4444"
    grade      = meeting_data.get("grade", "")
    prob       = meeting_data.get("closing_probability", 0)
    customer   = meeting_data.get("customer_name", "")
    meeting_id = meeting_data.get("meeting_id", "")
    url        = f"{settings.FRONTEND_URL}/meetings/{meeting_id}"

    html = f"""
    <div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;max-width:580px;margin:auto;background:#f8fafc;padding:20px;border-radius:12px">
        <div style="background:#1e3a5f;color:#fff;padding:24px;border-radius:8px;text-align:center;margin-bottom:20px">
            <h2 style="margin:0;font-size:22px">✅ تم تحليل اجتماعك</h2>
            <p style="margin:8px 0 0;opacity:.85">{customer}</p>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px">
            <div style="background:#fff;padding:20px;border-radius:8px;text-align:center;border:1px solid #e2e8f0">
                <div style="font-size:42px;font-weight:700;color:{color}">{score}</div>
                <div style="color:#64748b;font-size:13px;margin-top:4px">التقييم / 100 ({grade})</div>
            </div>
            <div style="background:#fff;padding:20px;border-radius:8px;text-align:center;border:1px solid #e2e8f0">
                <div style="font-size:42px;font-weight:700;color:#7c3aed">{prob}%</div>
                <div style="color:#64748b;font-size:13px;margin-top:4px">احتمالية الإغلاق</div>
            </div>
        </div>
        <a href="{url}" style="display:block;background:#2563eb;color:#fff;text-align:center;padding:14px;border-radius:8px;text-decoration:none;font-weight:600;font-size:16px">
            عرض التقرير الكامل →
        </a>
        <p style="color:#94a3b8;font-size:12px;text-align:center;margin-top:16px">Sales Intelligence System</p>
    </div>
    """
    return send_email([user_email], f"✅ تحليل اجتماع {customer} جاهز — {score}/100", html)
