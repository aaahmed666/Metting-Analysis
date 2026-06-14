"""
workers/queue.py — Celery dispatcher خفيف للـ API process
==========================================================

المشكلة اللي بيحلها:
- api/meetings.py و api/zoom_webhook.py كانوا بيعملوا
  `from ..workers.tasks import process_meeting_task`
- ده بيستورد tasks.py كاملة → whisper_service → torch + whisper
  → مئات الميجابايت RAM زيادة في الـ API container بدون أي فايدة
  (الـ API عمره ما بيعمل inference).

الحل:
- Celery app خفيف بنفس الـ broker، بيبعت الـ tasks بالاسم
  (send_task) بدون استيراد كود الـ task نفسه.
"""
from celery import Celery
from ..config import settings

# dispatcher فقط — مفيش tasks متعرّفة هنا
queue = Celery(
    "sales_intelligence_dispatch",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

queue.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
)


def enqueue_process_meeting(meeting_id: int):
    """يبعت task معالجة الاجتماع للـ worker بالاسم — بدون استيراد torch."""
    return queue.send_task("process_meeting", args=[meeting_id])
