"""Exercise real HTTP handlers against synthetic users in temporary storage."""

import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from web import app


class QuietHandler(app.StarHandler):
    def log_message(self, *_args):
        pass


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = [
            {"image_id": image_id, "dataset": "CPTAC", "gt_site": "Lung",
             "raw_tracking_id": "synthetic", "image_path": "unused.png"}
            for image_id in ("image_a", "image_b", "image_c")
        ]
        groups = {
            "group_a": {"label": "Group A", "image_count": 2,
                        "image_ids": ["image_a", "image_b"],
                        "phase_1_image_ids": ["image_a"], "phase_2_image_ids": ["image_b"]},
            "group_f": {"label": "Group F", "image_count": 2,
                        "image_ids": ["image_a", "image_c"],
                        "phase_1_image_ids": ["image_a"], "phase_3_image_ids": ["image_c"]},
        }
        self.patches = [
            patch.object(app, "USERS_DIR", self.root / "users"),
            patch.object(app, "ANSWERS_DIR", self.root / "answers"),
            patch.object(app, "GROUP_REGISTRY_PATH", self.root / "registry.json"),
            patch.object(app, "load_manifest", return_value=self.manifest),
            patch.object(app, "load_radiologist_groups", return_value={"version": 5, "groups": groups}),
            patch.object(app, "load_mllm_split", return_value={"image_ids": ["image_a", "image_b"]}),
        ]
        for replacement in self.patches:
            replacement.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.user_id = "synthetic_reader"
        self.seed_user()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        for replacement in reversed(self.patches):
            replacement.stop()
        self.temp.cleanup()

    def seed_user(self, **overrides):
        user = {"user_id": self.user_id, "user_type": "human", "stage": "q1",
                "current_image_id": "image_a", "current_answers": {}, "completed_image_ids": []}
        user.update(overrides)
        app.save_user(user)

    def request(self, path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        req = Request(f"http://127.0.0.1:{self.server.server_port}{path}", data=body,
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def current(self):
        status, task = self.request(f"/api/current?user_id={self.user_id}")
        self.assertEqual(status, 200)
        return task

    def payload(self, task):
        return {"user_id": self.user_id, "image_id": task["image_id"],
                "stage": task["stage"], "task_revision": task["task_revision"]}

    def answer(self, task, value):
        fields = {"q1": "q1_region", "q_lymph_node": "q_lymph_node",
                  "q2": "q2_site", "q4": "q4_quality_issue"}
        data = self.payload(task)
        data[fields[task["stage"]]] = value
        status, result = self.request("/api/answer", data)
        self.assertEqual(status, 200, result)
        return result["task"]

    def back(self, task):
        status, result = self.request("/api/back", self.payload(task))
        self.assertEqual(status, 200, result)
        return result["task"]

    def reach_q4(self):
        task = self.current()
        for value in ("Chest", "No", "Lung"):
            task = self.answer(task, value)
        self.assertEqual(task["stage"], "q4")
        return task

    def assert_rejected_without_writes(self, path, payload, expected=400):
        user_before = app.user_path(self.user_id).read_bytes()
        log_path = app.answer_path(self.user_id)
        log_before = log_path.read_bytes() if log_path.exists() else b""
        status, response = self.request(path, payload)
        self.assertEqual(status, expected, response)
        self.assertEqual(app.user_path(self.user_id).read_bytes(), user_before)
        self.assertEqual(log_path.read_bytes() if log_path.exists() else b"", log_before)

    def test_all_back_steps_preserve_selections_and_progress(self):
        task = self.reach_q4()
        original_answers = copy.deepcopy(task["answers"])
        for stage in ("q2", "q_lymph_node", "q1"):
            task = self.back(task)
            self.assertEqual(task["stage"], stage)
            self.assertEqual(task["image_id"], "image_a")
            self.assertEqual(task["answers"], original_answers)
            self.assertEqual(task["completed"], 0)
        self.assertFalse(task["can_go_back"])
        self.assert_rejected_without_writes("/api/back", self.payload(task))
        self.assertEqual(self.current()["answers"], original_answers)
        for value in ("Chest", "No", "Lung"):
            task = self.answer(task, value)
        self.assertEqual(task["answers"], original_answers)
        task = self.answer(task, "No issue")
        self.assertEqual(task["image_id"], "image_b")
        self.assertFalse(task["can_go_back"])
        events = app.answer_events(self.user_id)
        self.assertEqual(sum(e["stage"] == "back" for e in events), 3)
        self.assertEqual(sum(e["stage"] == "complete" for e in events), 1)
        self.assertEqual(app.user_stats(self.user_id)["completed"], 1)
        self.assertEqual(app.user_stats(self.user_id)["q2_accuracy"], 1)

    def test_changed_region_clears_dependent_answers(self):
        task = self.reach_q4()
        for _ in range(3):
            task = self.back(task)
        task = self.answer(task, "Abdomen")
        self.assertEqual(task["answers"], {"q1_region": "Abdomen"})
        task = self.answer(task, "No")
        self.assertIn("Liver", task["q2_choices"])
        self.assertNotIn("Lung", task["q2_choices"])
        task = self.answer(task, "Liver")
        self.answer(task, "No issue")
        final = [e for e in app.answer_events(self.user_id) if e["stage"] == "complete"]
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0]["q1_region"], "Abdomen")
        self.assertEqual(final[0]["q2_site"], "Liver")
        self.assertFalse(final[0]["q1_correct"])
        self.assertFalse(final[0]["q2_correct"])

    def test_changed_lymph_node_clears_site_and_validates_new_choices(self):
        task = self.back(self.back(self.reach_q4()))
        task = self.answer(task, "Yes")
        self.assertNotIn("q2_site", task["answers"])
        self.assertNotIn("Lung", task["q2_choices"])
        invalid = self.payload(task) | {"q2_site": "Lung"}
        self.assert_rejected_without_writes("/api/answer", invalid)
        self.answer(task, task["q2_choices"][0])

    def test_completed_image_and_next_image_boundary(self):
        old_task = self.reach_q4()
        next_task = self.answer(old_task, "No issue")
        self.assert_rejected_without_writes("/api/back", self.payload(next_task))
        self.assert_rejected_without_writes("/api/back", self.payload(old_task), 409)
        self.assert_rejected_without_writes("/api/answer", self.payload(old_task) | {"q4_quality_issue": "No issue"}, 409)
        self.assertEqual(app.load_user(self.user_id)["completed_image_ids"], ["image_a"])

    def test_old_revision_cannot_overwrite_same_stage_after_back(self):
        stale = self.current()
        task = self.back(self.answer(stale, "Chest"))
        self.assertEqual(task["stage"], stale["stage"])
        self.assert_rejected_without_writes("/api/answer", self.payload(stale) | {"q1_region": "Pelvis"}, 409)
        stale_back = self.answer(task, "Chest")
        task = self.back(stale_back)
        task = self.answer(task, "Chest")
        self.assert_rejected_without_writes("/api/back", self.payload(stale_back), 409)

    def test_concurrent_duplicate_submission_only_saves_once(self):
        payload = self.payload(self.current()) | {"q1_region": "Chest"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.request("/api/answer", payload), range(2)))
        self.assertEqual(sorted(status for status, _ in responses), [200, 409])
        self.assertEqual(len(app.answer_events(self.user_id)), 1)

    def test_existing_session_without_revision_remains_compatible(self):
        self.seed_user(stage="q_lymph_node", current_answers={"q1_region": "Chest"})
        task = self.back(self.current())
        legacy_payload = self.payload(task) | {"q1_region": "Chest"}
        legacy_payload.pop("task_revision")
        status, result = self.request("/api/answer", legacy_payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["task"]["stage"], "q_lymph_node")
        status, result = self.request("/api/users", {"user_id": self.user_id})
        self.assertEqual(status, 200)
        self.assertEqual(result["task"]["image_id"], "image_a")
        self.assertEqual(result["task"]["stage"], "q_lymph_node")

    def test_mllm_uses_same_back_rules_with_frozen_split(self):
        self.seed_user(user_type="mllm")
        task = self.reach_q4()
        self.assertEqual(task["total"], 2)
        task = self.back(task)
        self.assertEqual(task["stage"], "q2")
        self.assertEqual(task["image_id"], "image_a")
        self.assertEqual(task["answers"]["q2_site"], "Lung")

    def test_back_and_final_submit_cannot_both_apply(self):
        payload = self.payload(self.reach_q4())
        requests = [("/api/back", payload),
                    ("/api/answer", payload | {"q4_quality_issue": "No issue"})]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda args: self.request(*args), requests))
        self.assertEqual(sorted(status for status, _ in responses), [200, 409])
        events = app.answer_events(self.user_id)
        self.assertEqual(len(events), 4)
        final_ids = {e["image_id"] for e in events if e["stage"] == "complete"}
        self.assertEqual(set(app.load_user(self.user_id)["completed_image_ids"]), final_ids)

    def test_phase2_and_phase3_boundaries_and_done_user(self):
        for group, phase, image_id in (("group_a", "phase_2", "image_b"), ("group_f", "phase_3", "image_c")):
            with self.subTest(group=group):
                self.user_id = f"synthetic_{group}"
                self.seed_user(user_type="radiologist", assignment_group=group)
                task = self.reach_q4()
                task = self.back(task)
                self.assertEqual(task["study_phase"], "phase_1")
                self.assertFalse(task["show_accuracy"])
                task = self.answer(task, "Lung")
                task = self.answer(task, "No issue")
                self.assertEqual(task["study_phase"], phase)
                self.assertEqual(task["image_id"], image_id)
                self.assertEqual(task["overall_completed"], 1)
                self.assert_rejected_without_writes("/api/back", self.payload(task))
                for value in ("Chest", "No", "Lung", "No issue"):
                    task = self.answer(task, value)
                self.assertTrue(task["done"])
                self.assertEqual(task["overall_completed"], 2)
                final = [e for e in app.answer_events(self.user_id) if e["stage"] == "complete"]
                self.assertEqual(len(final), 2)
                self.assertEqual(final[-1]["study_phase"], phase)
                self.assert_rejected_without_writes("/api/back", {"user_id": self.user_id, "image_id": image_id, "stage": "q4"}, 409)
                self.assertFalse(self.current().get("can_go_back", False))


if __name__ == "__main__":
    unittest.main()
