"""services/report_service.py — التقرير اليومي للمدير"""
import json
from datetime import datetime, timedelta, date
from ..database import SessionLocal
from ..models import Meeting, User, DailyReport, Score, Analysis
from .email_service import send_email
from ..config import settings


class ReportService:

    def generate_and_send(self, target_date: date = None) -> dict:
        """توليد وإرسال التقرير اليومي."""
        db = SessionLocal()
        try:
            report_date = target_date or (datetime.utcnow() - timedelta(days=1)).date()

            # جلب اجتماعات الأمس المحللة
            start = datetime.combine(report_date, datetime.min.time())
            end   = datetime.combine(report_date, datetime.max.time())

            meetings = (
                db.query(Meeting)
                .filter(
                    Meeting.status     == "analyzed",
                    Meeting.created_at >= start,
                    Meeting.created_at <= end,
                )
                .all()
            )

            if not meetings:
                return {"status": "no_data", "date": str(report_date), "meetings": 0}

            # إحصائيات
            scores     = [m.score.total_score for m in meetings if m.score]
            avg_score  = round(sum(scores) / len(scores), 1) if scores else 0
            probs      = [m.analysis.closing_probability for m in meetings if m.analysis]
            avg_prob   = round(sum(probs) / len(probs), 1) if probs else 0

            # أفضل وأسوأ
            top_performer_id  = None
            needs_coaching_id = None
            if scores:
                user_scores: dict[int, list] = {}
                for m in meetings:
                    if m.score:
                        user_scores.setdefault(m.user_id, []).append(m.score.total_score)
                avg_per_user = {uid: sum(s) / len(s) for uid, s in user_scores.items()}
                top_performer_id  = max(avg_per_user, key=avg_per_user.get)
                needs_coaching_id = min(avg_per_user, key=avg_per_user.get)

            # أكثر الاعتراضات شيوعاً
            obj_counts: dict[str, int] = {}
            for m in meetings:
                if m.analysis and m.analysis.objections_raw:
                    for obj in m.analysis.objections_raw:
                        cat = obj.get("category", "other")
                        obj_counts[cat] = obj_counts.get(cat, 0) + 1

            top_objections = sorted(obj_counts.items(), key=lambda x: x[1], reverse=True)[:5]

            # بيانات المندوبين
            reps_data = self._build_reps_data(meetings, db)

            # حفظ في DB
            report = DailyReport(
                report_date        = report_date,
                total_meetings     = len(meetings),
                avg_score          = avg_score,
                avg_closing_prob   = avg_prob,
                top_performer_id   = top_performer_id,
                needs_coaching_id  = needs_coaching_id,
                top_objections     = top_objections,
                reps_data          = reps_data,
            )
            db.add(report)
            db.commit()

            # إرسال الإيميل
            if settings.admin_email_list:
                html  = self._render_html(report, reps_data)
                report.report_html = html
                db.commit()
                send_email(
                    settings.admin_email_list,
                    f"📊 تقرير يومي {report_date} — {len(meetings)} اجتماع | متوسط {avg_score}/100",
                    html,
                )

            return {"status": "success", "date": str(report_date), "meetings": len(meetings)}

        finally:
            db.close()

    def _build_reps_data(self, meetings: list, db) -> list:
        user_meetings: dict[int, list] = {}
        for m in meetings:
            user_meetings.setdefault(m.user_id, []).append(m)

        result = []
        for user_id, user_mtgs in user_meetings.items():
            user   = db.query(User).filter(User.id == user_id).first()
            scores = [m.score.total_score for m in user_mtgs if m.score]
            result.append({
                "user_id":    user_id,
                "name":       user.name if user else "غير معروف",
                "meetings":   len(user_mtgs),
                "avg_score":  round(sum(scores) / len(scores), 1) if scores else 0,
                "best_score": max(scores) if scores else 0,
            })

        result.sort(key=lambda r: r["avg_score"], reverse=True)
        return result

    def _render_html(self, report: DailyReport, reps_data: list) -> str:
        rows = ""
        for r in reps_data:
            color = "#22c55e" if r["avg_score"] >= 70 else "#f97316" if r["avg_score"] >= 50 else "#ef4444"
            rows += f"""
            <tr>
                <td style="padding:10px;border-bottom:1px solid #f1f5f9">{r['name']}</td>
                <td style="padding:10px;border-bottom:1px solid #f1f5f9;text-align:center">{r['meetings']}</td>
                <td style="padding:10px;border-bottom:1px solid #f1f5f9;text-align:center;color:{color};font-weight:700">{r['avg_score']}</td>
                <td style="padding:10px;border-bottom:1px solid #f1f5f9;text-align:center">{r['best_score']}</td>
            </tr>
            """

        return f"""
        <div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;max-width:680px;margin:auto;background:#f8fafc;padding:24px;border-radius:16px">
            <div style="background:#1e3a5f;color:#fff;padding:28px;border-radius:12px;text-align:center;margin-bottom:24px">
                <h1 style="margin:0;font-size:26px">📊 التقرير اليومي</h1>
                <p style="margin:8px 0 0;opacity:.8;font-size:14px">{report.report_date}</p>
            </div>

            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:24px">
                <div style="background:#fff;padding:20px;border-radius:10px;text-align:center;border:1px solid #e2e8f0">
                    <div style="font-size:36px;font-weight:700;color:#2563eb">{report.total_meetings}</div>
                    <div style="color:#64748b;font-size:13px;margin-top:4px">اجتماع محلل</div>
                </div>
                <div style="background:#fff;padding:20px;border-radius:10px;text-align:center;border:1px solid #e2e8f0">
                    <div style="font-size:36px;font-weight:700;color:#16a34a">{report.avg_score or 0}</div>
                    <div style="color:#64748b;font-size:13px;margin-top:4px">متوسط التقييم</div>
                </div>
                <div style="background:#fff;padding:20px;border-radius:10px;text-align:center;border:1px solid #e2e8f0">
                    <div style="font-size:36px;font-weight:700;color:#7c3aed">{report.avg_closing_prob or 0}%</div>
                    <div style="color:#64748b;font-size:13px;margin-top:4px">متوسط الإغلاق</div>
                </div>
            </div>

            <div style="background:#fff;border-radius:10px;border:1px solid #e2e8f0;overflow:hidden;margin-bottom:24px">
                <table style="width:100%;border-collapse:collapse">
                    <thead>
                        <tr style="background:#f8fafc">
                            <th style="padding:12px;text-align:right;font-size:13px;color:#64748b">المندوب</th>
                            <th style="padding:12px;text-align:center;font-size:13px;color:#64748b">اجتماعات</th>
                            <th style="padding:12px;text-align:center;font-size:13px;color:#64748b">متوسط التقييم</th>
                            <th style="padding:12px;text-align:center;font-size:13px;color:#64748b">أفضل</th>
                        </tr>
                    </thead>
                    <tbody>{rows}</tbody>
                </table>
            </div>

            <p style="color:#94a3b8;font-size:12px;text-align:center">Sales Intelligence System</p>
        </div>
        """


report_service = ReportService()
