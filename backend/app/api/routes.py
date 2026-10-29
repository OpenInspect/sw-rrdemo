from fastapi import APIRouter, HTTPException, UploadFile

from app.schemas import JobCreated, JobStatus

router = APIRouter(prefix="/api")


@router.get("/health")
def health():
    return {"status": "ok"}


@router.post("/jobs", status_code=202, response_model=JobCreated)
async def create_job(files: list[UploadFile]):
    # TODO: validate count/size (see settings.max_images, settings.max_image_bytes),
    #       base64-encode files into a StitchRequest, then
    #       celery.send_task("rrdemo.stitch_images", args=[...]) and return the task id.
    raise HTTPException(501, "not implemented")


@router.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str):
    # TODO: look up AsyncResult(job_id, app=celery) and map its state/result/error to JobStatus.
    raise HTTPException(501, "not implemented")
