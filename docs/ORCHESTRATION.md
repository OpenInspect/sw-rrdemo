# Orchestration implementation and commit instructions

This change implements Kavi's FastAPI/task-management portion of the demo. It keeps
the existing frontend and shared request/response schemas. It does not modify CV
algorithm code or the unfinished Celery worker wrapper.

## Apply the files

Use the existing team repository. Its root is the folder containing
`docker-compose.yml`, `backend/`, and `web/`. Do not create another GitHub repository
or place these files in a new nested folder.

1. In your local clone, start from your team's current base branch with a clean
   working tree. Create your branch before copying the files:

   ```bash
   git switch -c kavi/orchestration
   ```

2. Extract `orchestration-changes.zip` into the repository root, preserving paths
   and replacing the matching files. The archive contains only the eight files
   listed below. It is based on the supplied `sw-rrdemo-main.zip` snapshot
   (`f3687109760d8a637cdfc1f4e1b7ca504b4dbd70`). If teammates changed one of these
   same files after that snapshot, merge their edits with these changes.

3. Review and test the changes:

   ```bash
   git status --short
   git diff --check
   git diff
   python -m pip install -r backend/requirements-dev.txt
   cd backend
   python -m unittest discover -s tests -v
   cd ..
   ```

   Use your normal Python environment, or create/activate a virtual environment
   first. The application image uses Python 3.12.

4. Stage exactly these files, commit, and push your branch:

   ```bash
   git add backend/app/api/routes.py backend/app/celery_app.py backend/requirements.txt backend/requirements-dev.txt backend/tests/test_orchestration.py
   git add README.md docs/PROJECT_SUMMARY.md docs/ORCHESTRATION.md
   git commit -m "Implement FastAPI job orchestration"
   git push -u origin kavi/orchestration
   ```

5. On GitHub, open a pull request from `kavi/orchestration` into the team's base
   branch (normally `main` for this repository). Suggested title:
   **Implement FastAPI job orchestration**.

Suggested PR description:

> Implements multipart image validation, asynchronous Celery dispatch, and job
> status/result polling using the existing UI contract. Adds expiring job-ID
> tracking, readable backend/worker errors, JSON serialization settings, and
> orchestration tests. Adds Pillow for upload validation and SciPy for the existing
> CV imports. The worker task wrapper and stitching result-schema fixes remain
> with the CV integration. Validation: 34 API tests and a real Redis/Celery smoke
> test with a synthetic worker; actual image stitching and Docker Compose were
> not exercised.

## Files to commit

| Repository path | Purpose |
| --- | --- |
| `backend/app/api/routes.py` | Implements uploads, dispatch, polling, validation, job-ID tracking, and errors. |
| `backend/app/celery_app.py` | Makes JSON serialization explicit and bounds backend connection/retry behavior. |
| `backend/requirements.txt` | Adds Pillow for validation and the SciPy dependency already imported by the CV code. |
| `backend/requirements-dev.txt` | Adds the HTTP client needed by FastAPI's test client. |
| `backend/tests/test_orchestration.py` | Tests the API and queue contract without external services. |
| `README.md` | Updates run/test instructions and current implementation status. |
| `docs/PROJECT_SUMMARY.md` | Updates API behavior and the remaining integration steps. |
| `docs/ORCHESTRATION.md` | This guide and the worker handoff. |

Do not commit the downloaded ZIP, a virtual environment, Python caches, or local
credentials. No `.env` changes or Docker Compose changes are required.

## Implemented behavior

### Upload and dispatch

`POST /api/jobs` accepts repeated multipart fields named `files`. The API preserves
image order, checks the configured count and per-file byte limit, and uses Pillow
to decode each upload before enqueueing it. Supported formats are JPEG, PNG,
WebP, BMP, and TIFF; the file's actual content is checked rather than trusting
its filename or declared MIME type.

The API reads at most `MAX_IMAGE_BYTES + 1` bytes from each upload. It closes every
upload stream even when validation fails. Blocking upload processing, Redis
operations, and task publication run in FastAPI's thread pool.

After validation, it registers a UUID in Redis's result-backend database and
submits a JSON `StitchRequest` to task `rrdemo.stitch_images` on queue `cv` with
that UUID as `task_id`. It returns `202 {"job_id": "..."}` after dispatch, without
waiting for the worker. Failed publication returns `503` and attempts to remove
the job registration.

### Polling and results

`GET /api/jobs/{job_id}` returns JSON for the existing frontend to consume:

```json
{
  "job_id": "the-submitted-job-id",
  "state": "SUCCESS",
  "progress": null,
  "result": {
    "image": "base64-encoded-JPEG-bytes",
    "width": 1024,
    "height": 768
  },
  "error": null
}
```

While a job is queued/running, the same endpoint returns `200` with state
`PENDING`, `STARTED`, or `PROGRESS`, and no image result. The UI handles decoding
the final base64 image. The API does not return raw image bytes or use the rough
diagram's `/createTask` or `/task?id=...` routes.

Worker exceptions become `FAILURE` with a readable error. A malformed successful
worker result becomes `FAILURE` with an invalid-result message. `RECEIVED` and
`RETRY` map to `PENDING`; `REVOKED` maps to `FAILURE`, keeping the shared schema
unchanged. Unexpected worker states become a terminal failure instead of polling
indefinitely.

### Errors and retention

| HTTP status | Meaning |
| --- | --- |
| `400` | Wrong image count or an empty file. |
| `413` | A file exceeds the configured byte limit, or Pillow rejects extremely large image dimensions. |
| `415` | Unsupported, corrupt, or undecodable image data. |
| `422` | Required multipart `files` field missing or malformed request. |
| `404` | Unknown or expired job ID. |
| `503` | Redis/backend connection or task-publication failure. |

The job registry uses keys `rrdemo:job:{job_id}` in the result-backend Redis
database. Registrations expire `RESULT_TTL_SECONDS` after submission (default:
one hour); Celery's results expire that interval after completion. Consequently,
polling access ends one hour after submission, even for a job that runs longer.
This is adequate for the demo; no persistent job-history database is introduced.
Redis restarts clear jobs because the existing Compose service disables Redis
persistence.

The health endpoint is API liveness only. An offline worker can leave accepted
jobs queued until it starts or their registration expires. HTTP publication
retries are disabled to avoid holding a request through repeated broker retry
cycles. A connection failure after publication can still leave a job running
without a successful HTTP acknowledgement; durable/idempotent submission is
outside this demo.

## Worker handoff

These two existing worker files are deliberately unchanged in this package.
The full image-stitching demo requires the CV owner to finish both:

1. **`backend/app/worker/tasks.py`** still raises `NotImplementedError`. It needs
   to validate the received payload as `StitchRequest`, report progress, call
   `stitch_b64`, and return `StitchResult.model_dump()`. Keep the task name
   `rrdemo.stitch_images` and the `cv` queue.
2. **`backend/app/worker/stitching.py`** currently constructs
   `StitchResult(success=..., image_b64=..., error=...)`. Those fields do not match
   `app/schemas.py`. On success, return `StitchResult(image=result_b64,
   width=result_img.shape[1], height=result_img.shape[0])`. On failure, raise a
   readable exception, such as `ValueError`, so Celery records `FAILURE`.
   Returning an error dictionary as a normal task result would mark the task
   `SUCCESS` and violate the image-result contract.

The exact task input is one positional dictionary argument:

```json
{"images": ["base64-image-1", "base64-image-2"]}
```

The successful return value is a JSON-serializable dictionary:

```json
{"image": "base64-JPEG", "width": 1024, "height": 768}
```

Use unprefixed base64 strings, not binary bytes or NumPy arrays. For a progress
message, use `self.update_state(state="PROGRESS", meta={"progress": "Matching tiles"})`.
No frontend modifications or new endpoints are necessary for this contract.

## Configuration

The existing `.env.example` and Compose wiring remain usable:

| Variable | Default | Purpose |
| --- | --- | --- |
| `REDIS_BROKER_URL` | `redis://localhost:6379/0` outside Compose | Celery queue connection. |
| `REDIS_BACKEND_URL` | `redis://localhost:6379/1` outside Compose | Celery results and submitted-job registry. |
| `MAX_IMAGES` | `10` | Maximum images per upload; minimum is 2. |
| `MAX_IMAGE_BYTES` | `10485760` | Per-image limit of 10 MiB. |
| `RESULT_TTL_SECONDS` | `3600` | Submitted-job registration and Celery result TTL. |
| `WEB_DIR` | `web` outside Compose | Static UI directory. |

Compose already sets the Redis hostname to `redis` and the UI path to `/web`.
If count/size limits change, coordinate the corresponding validation constants
in `web/app.js`; the server always enforces its own values.

## Verification

The included 34 tests cover dispatch payloads, file formats and limits, empty and
corrupt files, upload cleanup, unavailable services, ID handling, every supported
state/result case, concurrent submissions, and independent job results. These
tests mock Redis/Celery dependencies and need no running external services.

A separate smoke check was also run against a real local Redis server and a
real Celery worker using a synthetic image-returning task. It verified actual
queue publication, pending/progress/success polling, returned JPEG decoding,
worker exceptions, invalid worker results, expiration, and broker/Redis outages.
The synthetic task was only a verification aid and is not included in the
application. Docker was unavailable in the implementation environment, and the
unfinished CV worker was not replaced with a mock in the delivered code.

After the CV owner finishes the handoff, run the complete demo from the repository
root:

```bash
docker compose up --build
```

Open http://localhost:8000, upload overlapping tiles, and check that the final
stitched image appears. Submit two jobs to verify their statuses stay separate.
Submit non-overlapping images to check worker failure reporting. To exercise
multiple worker containers:

```bash
docker compose up --scale worker=2
```

For direct API checks, use the multipart field `files` and substitute your own
image paths/job ID (`curl.exe` in Windows PowerShell):

```bash
curl -F "files=@tile1.jpg" -F "files=@tile2.jpg" http://localhost:8000/api/jobs
curl http://localhost:8000/api/jobs/YOUR_JOB_ID
```
