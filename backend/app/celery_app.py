from celery import Celery

from app.config import settings

celery = Celery(
    "rrdemo",
    broker=settings.redis_broker_url,
    backend=settings.redis_backend_url,
    include=["app.worker.tasks"],  # lazily imported; only the worker loads OpenCV
)
celery.conf.update(
    task_default_queue="cv",
    task_routes={"rrdemo.*": {"queue": "cv"}},
    task_track_started=True,
    result_expires=settings.result_ttl_seconds,
    worker_prefetch_multiplier=1,
)
