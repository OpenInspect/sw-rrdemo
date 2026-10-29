"""Schemas shared by the orchestrator and the CV worker."""
from typing import Literal

from pydantic import BaseModel

# TODO: when moving to S3, replace b64 payloads with object keys / presigned URLs.

JobState = Literal["PENDING", "STARTED", "PROGRESS", "SUCCESS", "FAILURE"]


class StitchRequest(BaseModel):
    images: list[str]  # base64-encoded image bytes


class StitchResult(BaseModel):
    image: str  # base64-encoded JPEG
    width: int
    height: int


class JobCreated(BaseModel):
    job_id: str


class JobStatus(BaseModel):
    job_id: str
    state: JobState
    progress: str | None = None
    result: StitchResult | None = None
    error: str | None = None
