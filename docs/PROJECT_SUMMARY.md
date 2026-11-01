# rrdemo – Project Summary

## Purpose
Senior-design risk-reduction demo showing we can build an MVP of the backend: a browser uploads multiple
images, an orchestration layer dispatches a job, and a computer-vision worker stitches them together.

## Architecture
```
Browser (static page + JS)
   │  POST /api/jobs (multipart, N images)      GET /api/jobs/{id} (polled)
   ▼
orchestrator (FastAPI) ── Celery client ──► Redis (broker + results) ──► worker (Celery + OpenCV Stitcher)
```

- **Web UI** – simple static page; uploads multiple images and polls task status (no SSE/websockets).
  Multiple tasks may be in flight at once, so each job is polled independently.
- **Orchestrator** – FastAPI. Accepts uploads, base64-encodes them, enqueues a Celery task, and reports
  task status/results. Also serves the static web page (single origin, no CORS, no extra container).
- **CV worker** – Celery worker running image stitching (OpenCV `Stitcher`).
- **Redis** – message broker (db 0) and result backend (db 1).
- **Docker Compose** – orchestrates `redis`, `orchestrator`, and `worker`. Workers scale with
  `docker compose up --scale worker=N`.

## Key decisions
- **Shared codebase**: orchestrator and worker live in one `backend/` codebase and one Docker image, differing
  only by startup command, so the worker can reuse schemas/classes defined by the orchestrator
  (`app/schemas.py`, `app/celery_app.py`, `app/config.py`).
- **Base64 for image transport**: acceptable for a demo; S3 would be preferable long-term. Moving to S3 should
  only require changes to `schemas.py` and the task input/output.
- **Dedicated `cv` queue**: the worker only consumes CV tasks; routing is declared once in `celery_app.py`.
- **Task referenced by name** (`rrdemo.stitch_images`): the API sends it via `send_task`, so it never has to
  import OpenCV code.
- **Limits and expiry**: upload count/size is capped in the API, and results expire after 1 hour so base64
  payloads don't accumulate in Redis. Submitted-job registrations also expire after 1 hour from
  submission, allowing unknown/expired IDs to return 404 instead of looking queued forever.
- **No `pydantic-settings`**: config is a plain dataclass reading environment variables (`app/config.py`).

## Contract
| Item | Definition |
|---|---|
| `POST /api/jobs` | multipart field `files`, 2–10 images → `202 {job_id}` |
| `GET /api/jobs/{id}` | `JobStatus`: state (`PENDING`/`STARTED`/`PROGRESS`/`SUCCESS`/`FAILURE`), optional progress, result, error |
| `GET /api/health` | `{"status": "ok"}` |
| Celery task | `rrdemo.stitch_images`, queue `cv`; payload is a `StitchRequest`, returns a `StitchResult` (base64 JPEG + size) |

The polling endpoint returns JSON with HTTP 200 for known jobs, including pending and failed jobs.
Unknown/expired IDs return 404; unavailable backend services return 503. Uploads are decoded/validated
as JPEG, PNG, WebP, BMP, or TIFF before dispatch.

## Layout
```
rrdemo/
├─ docker-compose.yml
├─ .env.example
├─ backend/
│  ├─ Dockerfile, requirements.txt
│  └─ app/
│     ├─ config.py, schemas.py, celery_app.py      # shared
│     ├─ api/        (main.py, routes.py)          # orchestrator
│     └─ worker/     (tasks.py, stitching.py)      # CV worker
└─ web/          (index.html, app.js)
```

## Running
```
docker compose up --build            # UI/API at http://localhost:8000
docker compose up --scale worker=2   # more workers
```

## Implementation status

The web UI and FastAPI orchestration endpoints are implemented. The CV algorithm is present, but
`app/worker/tasks.py` still raises `NotImplementedError`, and `stitching.py` needs to return the
existing `StitchResult(image, width, height)` schema instead of its current incompatible fields.
The missing SciPy dependency is now declared. See [ORCHESTRATION.md](ORCHESTRATION.md) for the
worker handoff, tests, API behavior, and commit instructions.

## Verification checklist (after the worker handoff)
1. All three services start; `curl localhost:8000/api/health` returns ok.
2. Upload 2+ overlapping photos in the UI: the row goes PENDING → STARTED/PROGRESS → SUCCESS and the stitched
   image renders.
3. Submit several uploads rapidly: each row progresses independently.
4. Upload invalid or non-overlapping images: the row shows FAILURE with an error message and polling stops.
5. Repeat with `--scale worker=2` to demonstrate horizontal scaling.

## Out of scope / future work
Auth, S3 storage, stitching quality tuning and mode selection, automatic worker retries,
and persistent job history. Orchestration API tests are included in `backend/tests/`.
