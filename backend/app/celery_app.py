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
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    broker_connection_timeout=3,
    broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
    redis_socket_connect_timeout=3,
    redis_socket_timeout=3,
    result_backend_transport_options={"retry_policy": {"max_retries": 0}},
)
