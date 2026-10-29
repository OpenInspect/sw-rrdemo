from app.celery_app import celery


@celery.task(bind=True, name="rrdemo.stitch_images")
def stitch_images(self, payload: dict) -> dict:
    # TODO: parse payload as StitchRequest, report PROGRESS via self.update_state,
    #       call app.worker.stitching.stitch_b64, return StitchResult.model_dump().
    raise NotImplementedError
