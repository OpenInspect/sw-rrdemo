# rrdemo

Scaffold for the risk-reduction demo: web UI → FastAPI orchestrator → Celery (Redis) → OpenCV stitching worker.

```
docker compose up --build            # UI/API at http://localhost:8000
docker compose up --scale worker=2   # more workers
```

## Layout
- `backend/` – one codebase/image; orchestrator and worker differ only by command in `docker-compose.yml`.
  - `app/schemas.py`, `app/config.py`, `app/celery_app.py` – shared.
  - `app/api/` – FastAPI (`routes.py` endpoints are stubs returning 501; also serves `web/`).
  - `app/worker/` – Celery task (`tasks.py`) and CV logic (`stitching.py`), both stubs.
- `web/` – static page; `app.js` is a TODO.

## Contract
- `POST /api/jobs` (multipart `files`, 2–10 images) → `202 {job_id}`
- `GET /api/jobs/{id}` → `JobStatus` (poll)
- Celery task `rrdemo.stitch_images`, queue `cv`; images/results are base64 (swap for S3 later).
