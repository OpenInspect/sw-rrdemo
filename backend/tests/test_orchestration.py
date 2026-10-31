import base64
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError
from PIL import Image
from redis.exceptions import ConnectionError as RedisConnectionError

from app.api import routes


def image_bytes(format="PNG"):
    buffer = BytesIO()
    Image.new("RGB", (16, 12), "green").save(buffer, format=format)
    return buffer.getvalue()


class OrchestrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = image_bytes()
        cls.result = {
            "image": base64.b64encode(image_bytes("JPEG")).decode("ascii"),
            "width": 16,
            "height": 12,
        }

    def setUp(self):
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.registry = Mock()
        self.registry.exists.return_value = True
        for target, value in [("job_registry", self.registry), ("logger", Mock())]:
            patcher = patch.object(routes, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.send = self.start_patch(patch.object(routes.celery, "send_task"))
        self.async_result = self.start_patch(patch.object(routes, "AsyncResult"))
        self.async_result.return_value = SimpleNamespace(state="PENDING", info=None)

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def upload(self, count=2, data=None, content_type="image/png"):
        data = self.image if data is None else data
        return self.client.post(
            "/api/jobs",
            files=[("files", (f"tile-{i}.png", data, content_type)) for i in range(count)],
        )

    def test_health(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_upload_returns_id_and_dispatches_expected_payload(self):
        response = self.upload()
        self.assertEqual(response.status_code, 202)
        job_id = response.json()["job_id"]
        self.assertEqual(str(UUID(job_id)), job_id)
        self.registry.set.assert_called_once_with(
            f"rrdemo:job:{job_id}", "1", ex=routes.settings.result_ttl_seconds
        )
        self.send.assert_called_once_with(
            "rrdemo.stitch_images",
            args=[{"images": [base64.b64encode(self.image).decode("ascii")] * 2}],
            task_id=job_id,
            queue="cv",
            serializer="json",
            retry=False,
        )

    def test_maximum_image_count_is_accepted(self):
        self.assertEqual(self.upload(count=10).status_code, 202)

    def test_invalid_counts_are_rejected_before_dispatch(self):
        for count in (1, 11):
            with self.subTest(count=count):
                self.assertEqual(self.upload(count=count).status_code, 400)
        self.send.assert_not_called()
        self.registry.set.assert_not_called()

    def test_missing_upload_field_is_rejected(self):
        response = self.client.post("/api/jobs")
        self.assertEqual(response.status_code, 422)
        self.send.assert_not_called()

    def test_empty_image_is_rejected(self):
        self.assertEqual(self.upload(data=b"").status_code, 400)
        self.send.assert_not_called()

    def test_non_image_disguised_as_png_is_rejected(self):
        self.assertEqual(self.upload(data=b"not an image").status_code, 415)
        self.send.assert_not_called()

    def test_truncated_jpeg_is_rejected(self):
        self.assertEqual(self.upload(data=image_bytes("JPEG")[:-20]).status_code, 415)
        self.send.assert_not_called()

    def test_valid_image_with_generic_mime_type_is_accepted(self):
        self.assertEqual(self.upload(content_type="application/octet-stream").status_code, 202)

    def test_supported_image_formats(self):
        for format in ("JPEG", "PNG", "WEBP", "BMP", "TIFF"):
            with self.subTest(format=format):
                self.assertEqual(self.upload(data=image_bytes(format)).status_code, 202)

    def test_gif_is_rejected_as_unsupported(self):
        self.assertEqual(self.upload(data=image_bytes("GIF")).status_code, 415)

    def test_size_limit_is_enforced_and_exact_limit_is_accepted(self):
        with patch.object(routes, "settings", replace(routes.settings, max_image_bytes=len(self.image))):
            self.assertEqual(self.upload().status_code, 202)
            self.assertEqual(self.upload(data=self.image + b"x").status_code, 413)
        self.assertEqual(self.send.call_count, 1)

    def test_upload_read_is_bounded(self):
        stream = Mock()
        stream.read.return_value = b"x" * 9
        upload = UploadFile(file=stream, filename="large.png")
        with patch.object(routes, "settings", replace(routes.settings, max_image_bytes=8)):
            with self.assertRaises(routes.HTTPException) as error:
                routes._encode_upload(upload)
        self.assertEqual(error.exception.status_code, 413)
        stream.read.assert_called_once_with(9)

    def test_uploads_close_even_when_validation_fails(self):
        streams = [BytesIO(b"invalid"), BytesIO(self.image)]
        uploads = [UploadFile(file=stream, filename="tile.png") for stream in streams]
        with self.assertRaises(routes.HTTPException):
            routes.create_job(uploads)
        self.assertTrue(all(stream.closed for stream in streams))
        self.send.assert_not_called()

    def test_custom_maximum_count_is_used(self):
        with patch.object(routes, "settings", replace(routes.settings, max_images=3)):
            self.assertEqual(self.upload(count=4).status_code, 400)
        self.send.assert_not_called()

    def test_registry_unavailable_returns_503_without_dispatch(self):
        self.registry.set.side_effect = RedisConnectionError("unavailable")
        self.assertEqual(self.upload().status_code, 503)
        self.send.assert_not_called()

    def test_dispatch_failure_returns_503_and_removes_registration(self):
        self.send.side_effect = OperationalError("broker unavailable")
        self.assertEqual(self.upload().status_code, 503)
        key = self.registry.set.call_args.args[0]
        self.registry.delete.assert_called_once_with(key)

    def test_cleanup_failure_preserves_503(self):
        self.send.side_effect = OperationalError("broker unavailable")
        self.registry.delete.side_effect = RedisConnectionError("unavailable")
        self.assertEqual(self.upload().status_code, 503)

    def test_celery_result_consumer_reconnect_failure_returns_503(self):
        self.send.side_effect = RuntimeError("Retry limit exceeded while reconnecting to Redis")
        self.assertEqual(self.upload().status_code, 503)
        self.registry.delete.assert_called_once()

    def test_unknown_or_expired_job_returns_404(self):
        self.registry.exists.return_value = False
        response = self.client.get("/api/jobs/unknown")
        self.assertEqual(response.status_code, 404)
        self.async_result.assert_not_called()

    def test_registry_failure_during_poll_returns_503(self):
        self.registry.exists.side_effect = RedisConnectionError("unavailable")
        self.assertEqual(self.client.get("/api/jobs/job-1").status_code, 503)

    def test_result_backend_failure_returns_503(self):
        self.async_result.side_effect = RedisConnectionError("unavailable")
        self.assertEqual(self.client.get("/api/jobs/job-1").status_code, 503)

    def test_pending_and_started(self):
        for state in ("PENDING", "STARTED"):
            with self.subTest(state=state):
                self.async_result.return_value = SimpleNamespace(state=state, info=None)
                response = self.client.get("/api/jobs/job-1")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["state"], state)
                self.assertIsNone(response.json()["result"])
        self.async_result.assert_called_with("job-1", app=routes.celery)

    def test_progress_message(self):
        self.async_result.return_value = SimpleNamespace(
            state="PROGRESS", info={"progress": "Matching tiles"}
        )
        body = self.client.get("/api/jobs/job-1").json()
        self.assertEqual(body["state"], "PROGRESS")
        self.assertEqual(body["progress"], "Matching tiles")

    def test_progress_without_metadata(self):
        self.async_result.return_value = SimpleNamespace(state="PROGRESS", info=None)
        self.assertEqual(self.client.get("/api/jobs/job-1").json()["state"], "PROGRESS")

    def test_success_returns_image_and_dimensions(self):
        self.async_result.return_value = SimpleNamespace(state="SUCCESS", info=self.result)
        body = self.client.get("/api/jobs/job-1").json()
        self.assertEqual(body["state"], "SUCCESS")
        self.assertEqual(body["result"], self.result)
        self.assertIsNone(body["error"])

    def test_invalid_worker_result_is_reported_as_failure(self):
        self.async_result.return_value = SimpleNamespace(
            state="SUCCESS", info={"success": True, "image_b64": "old-format"}
        )
        response = self.client.get("/api/jobs/job-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "FAILURE")
        self.assertIn("invalid image result", response.json()["error"])

    def test_worker_exception_is_reported_as_failure(self):
        self.async_result.return_value = SimpleNamespace(
            state="FAILURE", info=ValueError("Could not align the images")
        )
        body = self.client.get("/api/jobs/job-1").json()
        self.assertEqual(body["state"], "FAILURE")
        self.assertEqual(body["error"], "Could not align the images")

    def test_failure_without_exception_has_readable_error(self):
        self.async_result.return_value = SimpleNamespace(state="FAILURE", info=None)
        self.assertEqual(
            self.client.get("/api/jobs/job-1").json()["error"], "Image processing failed."
        )

    def test_retry_and_received_map_to_pending(self):
        for state in ("RETRY", "RECEIVED"):
            with self.subTest(state=state):
                self.async_result.return_value = SimpleNamespace(state=state, info=None)
                self.assertEqual(self.client.get("/api/jobs/job-1").json()["state"], "PENDING")

    def test_revoked_maps_to_failure(self):
        self.async_result.return_value = SimpleNamespace(state="REVOKED", info=None)
        body = self.client.get("/api/jobs/job-1").json()
        self.assertEqual(body["state"], "FAILURE")
        self.assertEqual(body["error"], "The job was cancelled.")

    def test_unknown_worker_state_stops_polling_with_failure(self):
        self.async_result.return_value = SimpleNamespace(state="UNEXPECTED", info=None)
        self.assertEqual(self.client.get("/api/jobs/job-1").json()["state"], "FAILURE")

    def test_simultaneous_uploads_receive_independent_ids(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.upload(), range(4)))
        self.assertTrue(all(response.status_code == 202 for response in responses))
        ids = {response.json()["job_id"] for response in responses}
        self.assertEqual(len(ids), 4)
        self.assertEqual({call.kwargs["task_id"] for call in self.send.call_args_list}, ids)

    def test_polling_keeps_job_results_separate(self):
        other_result = {**self.result, "width": 32}
        self.async_result.side_effect = lambda job_id, app: SimpleNamespace(
            state="SUCCESS", info=self.result if job_id == "first" else other_result
        )
        self.assertEqual(self.client.get("/api/jobs/first").json()["result"]["width"], 16)
        self.assertEqual(self.client.get("/api/jobs/second").json()["result"]["width"], 32)


if __name__ == "__main__":
    unittest.main()
