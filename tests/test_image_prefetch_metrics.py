"""Lookahead and diagnostics must never mutate annotation state."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import test_question_navigation as navigation
from web import app


class PrefetchMetricTests(unittest.TestCase):
    def setUp(self):
        self.fixture = navigation.NavigationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.log = self.fixture.root / "monitor" / "image-client.jsonl"
        self.log.parent.mkdir()
        replacement = patch.object(app, "IMAGE_METRICS_LOG", self.log)
        replacement.start()
        self.addCleanup(replacement.stop)

    def state(self):
        return {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                for directory in [app.USERS_DIR, app.ANSWERS_DIR]
                for path in directory.glob("*") if path.is_file()}

    def metric(self, **updates):
        body = {"user_id": "synthetic_reader", "image_id": "image_a",
                "request_id": "testpage-1", "kind": "display", "outcome": "loaded",
                "client_time": "2026-10-27T07:00:00.000Z", "elapsed_ms": 1245.5,
                "resource": {"duration_ms": 1240, "ttfb_ms": 120, "download_ms": 1120,
                             "encoded_bytes": 250000, "protocol": "http/1.1"}}
        body.update(updates)
        return body

    def post(self, data):
        body = json.dumps(data).encode()
        request = Request(f"http://127.0.0.1:{self.fixture.server.server_port}/api/image-metrics",
                          data=body, headers={"Content-Type": "application/json"})
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    def test_human_lookahead_skips_completed_images_without_mutating_user(self):
        user = app.load_user(self.fixture.user_id)
        user["completed_image_ids"] = ["image_b"]
        before = copy.deepcopy(user)
        with patch.object(app, "save_user") as save, patch.object(app, "append_jsonl") as append:
            result = app.next_image_preview(user, self.fixture.manifest)
        self.assertEqual(result, {"image_id": "image_c", "image_url": "/image/image_c"})
        self.assertEqual(user, before)
        save.assert_not_called()
        append.assert_not_called()

    def test_public_payload_provides_one_opaque_next_image_with_no_writes(self):
        before = self.state()
        task = self.fixture.current()
        self.assertEqual(task["next_image"], {"image_id": "image_b", "image_url": "/image/image_b"})
        self.assertEqual(set(task["next_image"]), {"image_id", "image_url"})
        self.assertEqual(self.state(), before)

    def test_phase1_last_image_previews_followup_in_the_same_group(self):
        for group, expected in [("group_a", "image_b"), ("group_f", "image_c")]:
            with self.subTest(group=group):
                self.fixture.seed_user(user_type="radiologist", assignment_group=group)
                before = self.state()
                task = self.fixture.current()
                self.assertEqual(task["study_phase"], "phase_1")
                self.assertEqual(task["next_image"]["image_id"], expected)
                self.assertEqual(self.state(), before)

    def test_last_image_in_each_assignment_has_no_lookahead(self):
        for user_type, group, current in [("human", None, "image_c"), ("mllm", None, "image_b"),
                                          ("radiologist", "group_a", "image_b"),
                                          ("radiologist", "group_f", "image_c")]:
            with self.subTest(user_type=user_type, group=group):
                completed = [x["image_id"] for x in self.fixture.manifest if x["image_id"] != current]
                self.fixture.seed_user(user_type=user_type, assignment_group=group,
                                       current_image_id=current, completed_image_ids=completed)
                before = self.state()
                task = self.fixture.current()
                self.assertIsNone(task["next_image"])
                self.assertEqual(self.state(), before)

    def test_mllm_lookahead_stays_inside_frozen_split(self):
        self.fixture.seed_user(user_type="mllm")
        task = self.fixture.current()
        self.assertEqual(task["next_image"]["image_id"], "image_b")
        self.assertNotIn("image_c", json.dumps(task["next_image"]))

    def test_backtracking_retains_the_same_preview_until_final_submission(self):
        task = self.fixture.reach_q4()
        preview = task["next_image"]
        task = self.fixture.back(task)
        self.assertEqual(task["next_image"], preview)
        self.assertEqual(task["completed"], 0)
        task = self.fixture.answer(task, "Lung")
        task = self.fixture.answer(task, "No issue")
        self.assertEqual(task["image_id"], preview["image_id"])
        self.assertEqual(task["completed"], 1)

    def test_metric_records_only_allowlisted_fields_and_never_answers(self):
        before = self.state()
        data = self.metric(answers={"q1_region": "secret"}, gt_site="secret", url="secret")
        data["resource"]["unknown"] = "secret"
        self.assertEqual(self.post(data), (204, b""))
        row = json.loads(self.log.read_text())
        self.assertEqual(row["elapsed_ms"], 1245.5)
        self.assertIn("server_time", row)
        self.assertNotIn("secret", self.log.read_text())
        self.assertEqual(self.state(), before)

    def test_invalid_and_oversized_metrics_cannot_affect_state(self):
        before = self.state()
        for data in [None, self.metric(elapsed_ms=float("nan")), self.metric(elapsed_ms=-1),
                     self.metric(outcome="submit"), self.metric(client_time="bad"),
                     self.metric(resource={"encoded_bytes": float("inf")})]:
            with self.subTest(data_type=type(data).__name__):
                self.assertEqual(self.post(data)[0], 400)
        self.assertEqual(self.post(self.metric(extra="x" * 5000))[0], 413)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.state(), before)

    def test_disabled_unwritable_or_full_diagnostic_log_is_nonfatal(self):
        before = self.state()
        with patch.object(app, "IMAGE_METRICS_LOG", None):
            self.assertEqual(self.post(self.metric())[0], 204)
        with patch.object(app, "IMAGE_METRICS_LOG", self.log / "missing" / "file"):
            self.assertEqual(self.post(self.metric())[0], 204)
        self.log.write_text("log at cap")
        with patch.object(app, "IMAGE_METRICS_MAX_BYTES", 1):
            self.assertEqual(self.post(self.metric())[0], 204)
        self.assertEqual(self.log.read_text(), "log at cap")
        self.assertEqual(self.state(), before)


if __name__ == "__main__":
    unittest.main()
