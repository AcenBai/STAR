"""Read-only image HTTP tests with synthetic PNGs and disposable user state."""
import base64
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import test_question_navigation as navigation
from web import app


class ImageDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = navigation.NavigationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.data_dir = self.fixture.root / "image-data"
        self.image = self.data_dir / "CPTAC" / "vis_check" / "Lung lesion # synthetic.png"
        self.image.parent.mkdir(parents=True)
        self.png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWZkAAAAASUVORK5CYII="
        )
        self.image.write_bytes(self.png)
        self.fixture.manifest[0]["image_path"] = str(self.image)
        for name, value in [("DATA_DIR", self.data_dir), ("IMAGE_OFFLOAD_ENABLED", False)]:
            replacement = patch.object(app, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def get(self, path="/image/image_a", headers=None):
        req = Request(f"http://127.0.0.1:{self.fixture.server.server_port}{path}", headers=headers or {})
        try:
            response = urlopen(req, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def runtime_bytes(self):
        return {str(p.relative_to(self.fixture.root)): p.read_bytes()
                for folder in [app.USERS_DIR, app.ANSWERS_DIR]
                for p in folder.glob("*") if p.is_file()}

    def test_original_bytes_and_private_cache_headers(self):
        status, headers, body = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(body, self.png)
        self.assertEqual(int(headers["Content-Length"]), len(self.png))
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["Cache-Control"], "private, max-age=3600")
        self.assertTrue(headers["ETag"])
        self.assertTrue(headers["Last-Modified"])
        self.assertNotIn("X-Accel-Redirect", headers)
        self.assertNotIn("Lung", str(headers))

    def test_etag_last_modified_and_manual_reload(self):
        _, headers, _ = self.get()
        for conditional in [{"If-None-Match": headers["ETag"]},
                            {"If-None-Match": "W/" + headers["ETag"]},
                            {"If-Modified-Since": headers["Last-Modified"]}]:
            status, cached_headers, body = self.get(headers=conditional)
            self.assertEqual(status, 304)
            self.assertEqual(body, b"")
            self.assertEqual(cached_headers["Cache-Control"], "private, max-age=3600")
        status, _, body = self.get("/image/image_a?reload=new-request")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.png)
        status, _, _ = self.get(headers={"If-None-Match": '"outdated"',
                                       "If-Modified-Since": headers["Last-Modified"]})
        self.assertEqual(status, 200)

    def test_reloads_never_write_q2_q3_q4_state_or_logs(self):
        for stage in ["q_lymph_node", "q2", "q4"]:
            with self.subTest(stage=stage):
                self.fixture.seed_user(stage=stage, current_answers={"q1_region": "Chest", "q_lymph_node": "No"})
                before = self.runtime_bytes()
                for attempt in range(4):
                    status, _, body = self.get(f"/image/image_a?reload={stage}-{attempt}")
                    self.assertEqual(status, 200)
                    self.assertEqual(body, self.png)
                self.assertEqual(self.runtime_bytes(), before)

    def test_offload_uses_encoded_internal_uri_without_reading_image_body(self):
        with patch.object(app, "IMAGE_OFFLOAD_ENABLED", True), patch.object(app.shutil, "copyfileobj") as copy:
            before = self.runtime_bytes()
            status, headers, body = self.get("/image/image_a?reload=offload")
            self.assertEqual(status, 200)
            expected = app.IMAGE_INTERNAL_PREFIX + quote("CPTAC/vis_check/" + self.image.name, safe="/")
            self.assertEqual(headers["X-Accel-Redirect"], expected)
            self.assertEqual(body, b"")
            copy.assert_not_called()
            self.assertEqual(self.runtime_bytes(), before)

    def test_outside_root_and_non_png_are_rejected_without_cache(self):
        outside = self.fixture.root / "outside.png"
        outside.write_bytes(self.png)
        non_png = self.image.with_suffix(".json")
        non_png.write_text("not an image")
        for path in [outside, non_png, self.image.parent]:
            with self.subTest(path=path), patch.object(app, "IMAGE_OFFLOAD_ENABLED", True):
                self.fixture.manifest[0]["image_path"] = str(path)
                status, headers, _ = self.get()
                self.assertEqual(status, 404)
                self.assertNotIn("X-Accel-Redirect", headers)
                self.assertNotIn("Cache-Control", headers)

    def test_unknown_and_missing_files_are_not_cached(self):
        self.fixture.manifest[0]["image_path"] = str(self.image.with_name("missing.png"))
        for path in ["/image/image_a", "/image/unknown", "/image/../private"]:
            status, headers, _ = self.get(path)
            self.assertEqual(status, 404)
            self.assertNotIn("Cache-Control", headers)

    def test_task_api_does_not_receive_image_cache_headers(self):
        status, headers, _ = self.get(f"/api/current?user_id={self.fixture.user_id}")
        self.assertEqual(status, 200)
        self.assertNotIn("Cache-Control", headers)
        self.assertNotIn("ETag", headers)


if __name__ == "__main__":
    unittest.main()
