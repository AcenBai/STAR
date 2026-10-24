"""Exercise the shipped Nginx config on loopback with synthetic files only."""
import os
import shutil
import socket
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import test_image_delivery as images
from web import app


@unittest.skipUnless(shutil.which("nginx") and hasattr(os, "geteuid") and os.geteuid() == 0,
                     "Requires Linux Nginx and root for the isolated www-data worker")
class NginxImageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = images.ImageDeliveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        offload = patch.object(app, "IMAGE_OFFLOAD_ENABLED", True)
        offload.start()
        self.addCleanup(offload.stop)
        root = self.fixture.fixture.root
        root.chmod(0o755)
        for parent in [self.fixture.data_dir, self.fixture.image.parent.parent, self.fixture.image.parent]:
            parent.chmod(0o755)
        self.fixture.image.chmod(0o644)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        server = (Path(__file__).resolve().parents[1] / "ops/nginx/star.conf").read_text()
        server = server.replace("listen 80 default_server;", f"listen 127.0.0.1:{self.port};")
        server = server.replace("listen [::]:80 default_server;", "")
        server = server.replace("/srv/star-data/", self.fixture.data_dir.as_posix() + "/")
        server = server.replace("127.0.0.1:8765", f"127.0.0.1:{self.fixture.fixture.server.server_port}")
        config = root / "nginx.conf"
        config.write_text(f"user www-data;\nworker_processes 1;\npid {root}/nginx.pid;\n"
                          f"error_log {root}/nginx-error.log error;\n"
                          "events { worker_connections 64; }\n"
                          "http { include /etc/nginx/mime.types; access_log off;\n" + server + "\n}\n")
        self.process = subprocess.Popen([shutil.which("nginx"), "-p", str(root), "-c", str(config),
                                         "-g", "daemon off;"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_nginx)
        for _ in range(40):
            try:
                if self.get("/")[0] == 200:
                    break
            except (URLError, ConnectionError):
                time.sleep(0.05)
        else:
            self.fail("Isolated Nginx did not become ready")

    def stop_nginx(self):
        self.process.terminate()
        try:
            self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate(timeout=5)

    def get(self, path="/image/image_a", headers=None):
        req = Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_original_png_and_internal_filename_stay_private(self):
        before = self.fixture.runtime_bytes()
        status, headers, body = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(body, self.fixture.png)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers.get_all("Cache-Control"), ["private, max-age=3600"])
        self.assertIn("nginx", headers["Server"])
        self.assertNotIn("X-Accel-Redirect", headers)
        self.assertNotIn("Lung", str(headers))
        self.assertNotIn("_star_image_files", str(headers))
        self.assertEqual(self.fixture.runtime_bytes(), before)

    def test_browser_cache_validation_and_manual_bypass(self):
        _, headers, _ = self.get()
        for conditional in [{"If-None-Match": headers["ETag"]},
                            {"If-Modified-Since": headers["Last-Modified"]}]:
            status, _, body = self.get(headers=conditional)
            self.assertEqual(status, 304)
            self.assertEqual(body, b"")
        status, _, body = self.get("/image/image_a?reload=manual")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.fixture.png)
        status, headers, body = self.get(headers={"Range": "bytes=0-15"})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.fixture.png[:16])
        self.assertEqual(headers["Cache-Control"], "private, max-age=3600")

    def test_direct_internal_url_and_errors_are_not_cached(self):
        internal = app.IMAGE_INTERNAL_PREFIX + quote("CPTAC/vis_check/" + self.fixture.image.name, safe="/")
        for path in [internal, "/_star_image_files/", "/image/unknown"]:
            status, headers, _ = self.get(path)
            self.assertEqual(status, 404)
            self.assertNotIn("Cache-Control", headers)

    def test_answer_api_is_not_cached(self):
        status, headers, _ = self.get(f"/api/current?user_id={self.fixture.fixture.user_id}")
        self.assertEqual(status, 200)
        self.assertNotIn("Cache-Control", headers)
        self.assertNotIn("ETag", headers)


if __name__ == "__main__":
    unittest.main()
