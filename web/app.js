// Web UI for the stitching demo. Plain JS, no build step; served by the orchestrator.
//
// API contract (README.md / docs/PROJECT_SUMMARY.md):
//   POST /api/jobs        multipart field "files", 2–10 images  -> 202 {job_id}
//   GET  /api/jobs/{id}   JobStatus {job_id, state, progress?, result?, error?}  (polled)
//   GET  /api/health      {status: "ok"}
// result = StitchResult {image: base64 JPEG, width, height}
"use strict";

const API = "/api";

// Mirrors the defaults in backend/app/config.py. The server enforces the real
// limits; these only give quicker feedback before uploading.
const MIN_IMAGES = 2;
const MAX_IMAGES = 10;
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;

const POLL_INTERVAL_MS = 1000;
const POLL_TIMEOUT_MS = 10 * 60 * 1000; // stop polling a job after 10 minutes
const MAX_POLL_ERRORS = 5;              // consecutive failed polls before giving up
const HEALTH_INTERVAL_MS = 10000;

// Celery states, plus two client-side ones (TIMEOUT, ERROR) for when polling stops.
const STATE_LABELS = {
  PENDING: "Queued",
  STARTED: "Running",
  PROGRESS: "Running",
  RETRY: "Retrying",
  SUCCESS: "Done",
  FAILURE: "Failed",
  REVOKED: "Cancelled",
  TIMEOUT: "Timed out",
  ERROR: "Error",
};

const $ = (id) => document.getElementById(id);
const els = {
  apiStatus: $("api-status"),
  dropzone: $("dropzone"),
  fileInput: $("files"),
  selection: $("selection"),
  summary: $("selection-summary"),
  clear: $("clear"),
  upload: $("upload"),
  message: $("upload-message"),
  empty: $("empty"),
  table: $("jobs-table"),
  jobs: $("jobs"),
  announcer: $("announcer"),
};

/* ------------------------------------------------------------------ helpers */

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "dataset") Object.assign(node.dataset, value);
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.filter((c) => c != null));
  return node;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const shortId = (id) => String(id).slice(0, 8);

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDuration(ms) {
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}

function errorText(err) {
  // fetch() rejects with a TypeError when the server can't be reached at all.
  if (err instanceof TypeError) return "couldn't reach the server. Is the orchestrator running?";
  return err && err.message ? err.message : String(err);
}

// Turns a non-2xx response into a readable message, using FastAPI's {"detail": ...} when present.
async function describeError(res) {
  let detail = "";
  try {
    const text = await res.text();
    try {
      const data = JSON.parse(text);
      const d = data && data.detail !== undefined ? data.detail : data;
      if (typeof d === "string") detail = d;
      else if (Array.isArray(d)) {
        detail = d.map((e) => (e && e.msg ? (e.loc ? `${e.loc.join(".")}: ${e.msg}` : e.msg) : JSON.stringify(e))).join("; ");
      } else if (d) detail = JSON.stringify(d);
    } catch {
      detail = text.trim().slice(0, 200);
    }
  } catch {
    /* body unreadable; fall back to the status text */
  }
  return `HTTP ${res.status}: ${detail || res.statusText || "request failed"}`;
}

function base64ToBlob(b64) {
  let mime = "image/jpeg";
  const prefix = /^data:([^;,]+)?(;base64)?,/.exec(b64);
  if (prefix) {
    if (prefix[1]) mime = prefix[1];
    b64 = b64.slice(prefix[0].length);
  }
  const bin = atob(b64.replace(/\s/g, ""));
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

function announce(text) {
  els.announcer.textContent = text;
}

/* ---------------------------------------------------------------- API health */

async function checkHealth() {
  let state = "down";
  let label = "API unreachable";
  try {
    const res = await fetch(`${API}/health`, { cache: "no-store" });
    const body = res.ok ? await res.json().catch(() => null) : null;
    if (body && body.status === "ok") {
      state = "ok";
      label = "API online";
    } else {
      label = `API error (HTTP ${res.status})`;
    }
  } catch {
    /* unreachable */
  }
  els.apiStatus.dataset.state = state;
  els.apiStatus.textContent = label;
  setTimeout(checkHealth, HEALTH_INTERVAL_MS);
}

/* ----------------------------------------------------------------- selection */

let selected = []; // [{ file, url }]
let submitting = false;

function validationProblem() {
  const n = selected.length;
  const tooBig = selected.filter((s) => s.file.size > MAX_IMAGE_BYTES);
  if (tooBig.length) {
    const names = tooBig.map((s) => s.file.name).join(", ");
    return `${names} ${tooBig.length === 1 ? "is" : "are"} over the ${formatBytes(MAX_IMAGE_BYTES)} limit per photo. Remove ${tooBig.length === 1 ? "it" : "them"} to continue.`;
  }
  if (n > MAX_IMAGES) return `Remove ${plural(n - MAX_IMAGES, "photo")}. A job takes at most ${MAX_IMAGES}.`;
  if (n > 0 && n < MIN_IMAGES) return `Add at least ${MIN_IMAGES - n} more photo. Stitching needs two or more overlapping images.`;
  return "";
}

function showMessage(text) {
  els.message.textContent = text || "";
}

function renderSelection() {
  els.selection.replaceChildren(
    ...selected.map((s, i) =>
      el(
        "li",
        {
          className: s.file.size > MAX_IMAGE_BYTES ? "thumb is-invalid" : "thumb",
          title: `${s.file.name} (${formatBytes(s.file.size)})`,
        },
        el("img", { src: s.url, alt: s.file.name }),
        el("button", {
          type: "button",
          className: "thumb-remove",
          textContent: "×",
          disabled: submitting,
          "aria-label": `Remove ${s.file.name}`,
          onclick: () => removeAt(i),
        }),
      ),
    ),
  );
  els.selection.hidden = selected.length === 0;

  const total = selected.reduce((sum, s) => sum + s.file.size, 0);
  els.summary.textContent = selected.length
    ? `${plural(selected.length, "photo")} selected, ${formatBytes(total)}`
    : "No photos selected.";

  const ready = selected.length >= MIN_IMAGES && !validationProblem();
  els.upload.disabled = submitting || !ready;
  els.upload.textContent = submitting
    ? "Uploading…"
    : selected.length >= MIN_IMAGES ? `Stitch ${selected.length} photos` : "Stitch photos";
  els.clear.disabled = submitting || selected.length === 0;
  els.fileInput.disabled = submitting;
  els.dropzone.classList.toggle("is-disabled", submitting);
}

function addFiles(fileList) {
  const skipped = [];
  for (const file of fileList) {
    if (!file.type.startsWith("image/")) {
      skipped.push(file.name);
      continue;
    }
    const dupe = selected.some(
      (s) => s.file.name === file.name && s.file.size === file.size && s.file.lastModified === file.lastModified,
    );
    if (!dupe) selected.push({ file, url: URL.createObjectURL(file) });
  }
  renderSelection();
  const skippedNote = skipped.length ? `Skipped ${skipped.join(", ")}: not an image file.` : "";
  showMessage([skippedNote, validationProblem()].filter(Boolean).join(" "));
}

function removeAt(index) {
  const [removed] = selected.splice(index, 1);
  if (removed) URL.revokeObjectURL(removed.url);
  renderSelection();
  showMessage(validationProblem());
}

function clearSelection() {
  for (const s of selected) URL.revokeObjectURL(s.url);
  selected = [];
  renderSelection();
  showMessage("");
}

/* -------------------------------------------------------------------- upload */

async function submitJob() {
  if (submitting || selected.length < MIN_IMAGES || validationProblem()) return;

  const batch = selected.slice();
  const form = new FormData();
  for (const s of batch) form.append("files", s.file, s.file.name);

  submitting = true;
  renderSelection();
  showMessage("");
  try {
    const res = await fetch(`${API}/jobs`, { method: "POST", body: form });
    if (!res.ok) throw new Error(await describeError(res));
    const body = await res.json().catch(() => null);
    if (!body || body.job_id == null) throw new Error("the server accepted the upload but didn't return a job_id.");
    // The job row now owns these photos (and their preview URLs).
    selected = selected.filter((s) => !batch.includes(s));
    addJob(String(body.job_id), batch);
  } catch (err) {
    showMessage(`Upload failed: ${errorText(err)}`);
  } finally {
    submitting = false;
    renderSelection();
  }
}

/* ---------------------------------------------------------------------- jobs */

const jobs = new Map();

function addJob(id, photos) {
  const job = {
    id,
    photos,
    submittedAt: Date.now(),
    finishedAt: null,
    state: "PENDING",
    trail: [],
    progress: "",
    note: "",
    errors: 0,
    done: false,
  };

  const badge = el("span", { className: "badge" });
  const progress = el("p", { className: "status-progress" });
  const trail = el("p", { className: "status-trail" });
  const elapsed = el("p", { className: "status-elapsed" });
  const result = el("td", { className: "col-result" }, el("span", { className: "placeholder", textContent: "Waiting for result" }));

  const tr = el(
    "tr",
    {},
    el(
      "td",
      { className: "col-job" },
      el("code", { className: "job-id", textContent: id, title: id }),
      el("p", {
        className: "job-meta",
        textContent: `${plural(photos.length, "photo")}, submitted ${new Date(job.submittedAt).toLocaleTimeString()}`,
      }),
      el(
        "ul",
        { className: "thumbs thumbs-job", "aria-label": "Input photos" },
        ...photos.map((p) => el("li", { className: "thumb", title: p.file.name }, el("img", { src: p.url, alt: p.file.name }))),
      ),
    ),
    el("td", { className: "col-status" }, badge, progress, trail, elapsed),
    result,
  );

  job.dom = { tr, badge, progress, trail, elapsed, result };
  jobs.set(id, job);
  els.jobs.prepend(tr);
  els.table.hidden = false;
  els.empty.hidden = true;

  renderStatus(job);
  poll(job);
}

async function poll(job) {
  if (job.done) return;

  if (Date.now() - job.submittedAt > POLL_TIMEOUT_MS) {
    finish(job, "TIMEOUT", `No result after ${formatDuration(POLL_TIMEOUT_MS)}, so polling stopped. A job stuck in PENDING usually means no worker is running.`);
    return;
  }

  try {
    const res = await fetch(`${API}/jobs/${encodeURIComponent(job.id)}`, { cache: "no-store" });
    if (!res.ok) {
      const msg = await describeError(res);
      // Client errors (bad or unknown id) and 501 won't fix themselves; 5xx and 429 might.
      const permanent = (res.status >= 400 && res.status < 500 && res.status !== 429) || res.status === 501;
      if (permanent) {
        finish(job, "ERROR", `Couldn't get job status. ${msg}`);
        return;
      }
      throw new Error(msg);
    }
    // Any 2xx (200, or 202 while still running) carries a JobStatus body.
    const status = await res.json();
    job.errors = 0;
    job.note = "";
    applyStatus(job, status);
  } catch (err) {
    job.errors += 1;
    if (job.errors >= MAX_POLL_ERRORS) {
      finish(job, "ERROR", `Polling stopped after ${MAX_POLL_ERRORS} failed requests. Last error: ${errorText(err)}`);
      return;
    }
    job.note = `Retrying (${job.errors}/${MAX_POLL_ERRORS}): ${errorText(err)}`;
    renderStatus(job);
  }

  if (!job.done) setTimeout(() => poll(job), POLL_INTERVAL_MS);
}

function applyStatus(job, status) {
  const state = status && typeof status.state === "string" ? status.state : "";
  if (!state) {
    finish(job, "ERROR", "The server's response has no state field.");
    return;
  }
  if (job.trail[job.trail.length - 1] !== state) job.trail.push(state);
  job.state = state;
  job.progress = status.progress == null ? "" : typeof status.progress === "string" ? status.progress : JSON.stringify(status.progress);

  if (state === "SUCCESS") {
    if (status.result && status.result.image) finish(job, "SUCCESS", "", status.result);
    else finish(job, "ERROR", "The job finished but the response had no result image.");
  } else if (state === "FAILURE" || state === "REVOKED") {
    finish(job, state, status.error || "The worker didn't report a reason.");
  } else {
    renderStatus(job);
  }
}

function finish(job, state, errorMessage, result) {
  job.done = true;
  job.state = state;
  job.progress = "";
  job.note = "";
  job.finishedAt = Date.now();
  if (state === "TIMEOUT" || state === "ERROR") job.trail.push(state);
  renderStatus(job);

  if (result) renderResult(job, result);
  else renderError(job, errorMessage);

  announce(`Job ${shortId(job.id)}: ${STATE_LABELS[state] || state}.`);
}

function renderStatus(job) {
  const { badge, progress, trail, elapsed } = job.dom;
  badge.textContent = STATE_LABELS[job.state] || job.state;
  badge.dataset.state = job.state;
  badge.title = job.state;
  progress.textContent = job.note || job.progress;
  progress.classList.toggle("is-warning", Boolean(job.note));
  trail.textContent = job.trail.join(" → ");
  const ms = (job.finishedAt || Date.now()) - job.submittedAt;
  elapsed.textContent = job.done ? `Finished after ${formatDuration(ms)}` : `Elapsed ${formatDuration(ms)}`;
}

function renderError(job, message) {
  job.dom.result.replaceChildren(el("p", { className: "result-error", textContent: message }));
}

function renderResult(job, result) {
  let url;
  try {
    url = URL.createObjectURL(base64ToBlob(result.image));
  } catch {
    renderError(job, "The result image isn't valid base64, so it can't be shown.");
    return;
  }

  const size = el("span", { className: "result-size" });
  const showSize = (w, h) => {
    if (w && h) size.textContent = `${w} × ${h} px`;
  };
  showSize(result.width, result.height);

  const img = el("img", { className: "result-img", src: url, alt: `Stitched result of job ${shortId(job.id)}` });
  img.addEventListener("load", () => {
    if (!result.width || !result.height) showSize(img.naturalWidth, img.naturalHeight);
  });
  img.addEventListener("error", () => renderError(job, "The result came back, but the browser couldn't decode it as an image."));

  job.dom.result.replaceChildren(
    el("a", { href: url, target: "_blank", rel: "noopener", className: "result-link", title: "Open full size in a new tab" }, img),
    el(
      "p",
      { className: "result-meta" },
      size,
      el("a", { href: url, download: `stitched-${shortId(job.id)}.jpg`, textContent: "Download" }),
    ),
  );
}

/* -------------------------------------------------------------------- wiring */

els.fileInput.addEventListener("change", () => {
  addFiles(els.fileInput.files);
  els.fileInput.value = ""; // lets the same file be picked again after removing it
});

for (const type of ["dragenter", "dragover"]) {
  els.dropzone.addEventListener(type, (e) => {
    e.preventDefault();
    if (!submitting) els.dropzone.classList.add("is-over");
  });
}
for (const type of ["dragleave", "drop"]) {
  els.dropzone.addEventListener(type, (e) => {
    e.preventDefault();
    els.dropzone.classList.remove("is-over");
  });
}
els.dropzone.addEventListener("drop", (e) => {
  if (!submitting && e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
});
// Stop the browser from opening a photo that misses the drop zone.
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

els.clear.addEventListener("click", clearSelection);
els.upload.addEventListener("click", submitJob);

renderSelection();
checkHealth();
