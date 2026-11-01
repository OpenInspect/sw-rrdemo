import base64
import logging
from io import BytesIO
from typing import Annotated
from uuid import uuid4

from celery.result import AsyncResult
from fastapi import APIRouter, File, HTTPException, UploadFile
from kombu.exceptions import OperationalError
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError
from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from app.celery_app import celery
from app.config import settings
from app.schemas import JobCreated, JobStatus, StitchRequest, StitchResult

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)
job_registry = Redis.from_url(
    settings.redis_backend_url,
    socket_connect_timeout=3,
    socket_timeout=3,
    retry=Retry(NoBackoff(), 0),
)
backend_errors = (RedisError, OperationalError, OSError, RuntimeError)


def _job_key(job_id: str) -> str:
    return f"rrdemo:job:{job_id}"


def _encode_upload(upload: UploadFile) -> str:
    data = upload.file.read(settings.max_image_bytes + 1)
    if not data:
        raise HTTPException(400, "Uploaded images must not be empty.")
    if len(data) > settings.max_image_bytes:
        raise HTTPException(
            413, f"Each image must be at most {settings.max_image_bytes} bytes."
        )
    try:
        with Image.open(
            BytesIO(data), formats=("JPEG", "PNG", "WEBP", "BMP", "TIFF")
        ) as image:
            image.load()
    except Image.DecompressionBombError:
        raise HTTPException(413, "Image dimensions are too large.") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError):
        raise HTTPException(
            415, "Upload valid JPEG, PNG, WebP, BMP, or TIFF images."
        ) from None
    return base64.b64encode(data).decode("ascii")


@router.get("/health")
def health():
    return {"status": "ok"}


@router.post("/jobs", status_code=202, response_model=JobCreated)
def create_job(files: Annotated[list[UploadFile], File()]):
    try:
        if not 2 <= len(files) <= settings.max_images:
            raise HTTPException(
                400, f"Upload between 2 and {settings.max_images} images."
            )
        payload = StitchRequest(images=[_encode_upload(upload) for upload in files])
    finally:
        for upload in files:
            upload.file.close()

    job_id = str(uuid4())
    try:
        job_registry.set(_job_key(job_id), "1", ex=settings.result_ttl_seconds)
        celery.send_task(
            "rrdemo.stitch_images",
            args=[payload.model_dump()],
            task_id=job_id,
            queue="cv",
            serializer="json",
            retry=False,
        )
    except backend_errors:
        logger.exception("Could not submit job %s", job_id)
        try:
            job_registry.delete(_job_key(job_id))
        except backend_errors:
            logger.warning("Could not remove job registration %s", job_id)
        raise HTTPException(
            503, "The job service is unavailable. Please try again shortly."
        ) from None
    return JobCreated(job_id=job_id)


@router.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str):
    try:
        if not job_registry.exists(_job_key(job_id)):
            raise HTTPException(404, "Job not found or expired. Submit a new job.")
        task = AsyncResult(job_id, app=celery)
        state = task.state
        info = task.info
    except backend_errors:
        logger.exception("Could not read job %s", job_id)
        raise HTTPException(
            503, "The job service is unavailable. Please try again shortly."
        ) from None

    if state == "SUCCESS":
        try:
            result = StitchResult.model_validate(info)
        except ValidationError:
            logger.exception("Invalid result for job %s", job_id)
            return JobStatus(
                job_id=job_id,
                state="FAILURE",
                error="The worker returned an invalid image result.",
            )
        return JobStatus(job_id=job_id, state="SUCCESS", result=result)

    if state in {"FAILURE", "REVOKED"}:
        error = (
            "The job was cancelled."
            if state == "REVOKED"
            else str(info or "Image processing failed.")
        )
        return JobStatus(job_id=job_id, state="FAILURE", error=error)

    if state == "RETRY":
        return JobStatus(
            job_id=job_id,
            state="PENDING",
            progress="The worker is retrying this job.",
        )

    if state == "RECEIVED":
        state = "PENDING"
    if state not in {"PENDING", "STARTED", "PROGRESS"}:
        return JobStatus(
            job_id=job_id,
            state="FAILURE",
            error="The worker reported an unsupported job state.",
        )

    progress = info.get("progress") if isinstance(info, dict) else None
    return JobStatus(
        job_id=job_id,
        state=state,
        progress=str(progress) if progress is not None else None,
    )
