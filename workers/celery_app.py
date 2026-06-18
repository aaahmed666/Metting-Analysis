"""
Module: Celery Application
Purpose: Constructs and configures the Celery app instance (broker, result
         backend, serialization) shared by all background tasks.
"""
from celery import Celery

from config.setting import get_settings

settings = get_settings()

celery_app = Celery(
    "media_pipeline",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    worker_prefetch_multiplier=1,
)
