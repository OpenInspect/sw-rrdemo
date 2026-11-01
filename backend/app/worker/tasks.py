from app.celery_app import celery
from app.schemas import StitchRequest
from app.worker.stitching import stitch_b64


@celery.task(bind=True, name="rrdemo.stitch_images")
def stitch_images(self, payload: dict) -> dict:
    request = StitchRequest.model_validate(payload)
    self.update_state(
        state="PROGRESS",
        meta={"progress": f"stitching {len(request.images)} images"},
    )
    result = stitch_b64(request.images)
    return result.model_dump()
