# rrdemo

Risk-reduction demo: web UI → FastAPI orchestrator → Celery/Redis → image-stitching worker.

The upload/polling UI and orchestration API are implemented. The CV algorithm is present,
but the worker task wrapper and its result-schema integration still need completion;
see [the worker handoff](docs/ORCHESTRATION.md#worker-handoff).

## Run

```bash
docker compose up --build
```

Open http://localhost:8000. The same FastAPI service serves the UI and `/api/*`.
Until the worker handoff is completed, accepted uploads will report a worker failure
rather than produce a stitched image.

```bash
docker compose up --scale worker=2
```

## API contract

- `POST /api/jobs`: multipart field `files`, 2–10 images, at most 10 MiB per image;
  returns `202 {"job_id": "..."}` without waiting for CV processing.
- `GET /api/jobs/{job_id}`: returns `200` with `JobStatus`, including state and optional
  progress, result, or error. A successful result contains base64 JPEG `image`, `width`,
  and `height`. Unknown/expired IDs return `404`; unavailable backend services return `503`.
- `GET /api/health`: API liveness, `{"status": "ok"}`. This does not check worker availability.
- Celery task: `rrdemo.stitch_images`, queue `cv`, JSON `StitchRequest` → JSON `StitchResult`.

## Layout

- `backend/app/api/`: FastAPI orchestration endpoints and static UI hosting.
- `backend/app/config.py`, `schemas.py`, `celery_app.py`: shared settings, contract, and queue setup.
- `backend/app/worker/`: worker task wrapper and CV implementation, owned by the CV integration.
- `backend/tests/`: orchestration API tests with mocked queue/backend dependencies.
- `web/`: upload, polling, and results UI.

## Test

From the repository root, using your Python environment:

```bash
python -m pip install -r backend/requirements-dev.txt
cd backend
python -m unittest discover -s tests -v
```

The tests do not require Docker or a running Redis server. See
[ORCHESTRATION.md](docs/ORCHESTRATION.md) for changed files, commit instructions,
verification details, configuration, and the worker contract.
