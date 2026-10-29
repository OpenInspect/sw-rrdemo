from app.schemas import StitchResult


def stitch_b64(images_b64: list[str]) -> StitchResult:
    # TODO: decode b64 -> cv2 images, run cv2.Stitcher, encode result as JPEG b64.
    raise NotImplementedError
