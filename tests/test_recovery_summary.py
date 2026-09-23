"""Bounded recovery display retains queue identities and off-page diagnostics."""

import json
import os
from pathlib import Path
from unittest.mock import patch

from test_token_tools import ToolCase
import jobs
import protocol
import reviews
import runs
import watch


class RecoverySummaryTests(ToolCase):
    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.run = runs.init_run()

    def add_job(self):
        info = self.prepare(root=None, run=self.run["run_path"])
        jobs.register(self.run["index_path"], info["request_path"], now=100)
        return info

    def add_watch(self, infos):
        for i, info in enumerate(infos):
            if not Path(info["request_path"]).with_name("resources.json").exists():
                self.record(request=info["request_path"], pane=f"w{i}:p1")
        return watch.init_watch([info["request_path"] for info in infos])["watch_path"]

    def observe(self, handle, now=100):
        with patch.object(watch, "observe", return_value={
                "state": "working", "screen": "Command: inspect data\nAllow once / Deny"}):
            return watch.poll_watch(handle, now=now)["events"]

    def summary(self, **kwargs):
        return runs.recover_summary(self.run["run_path"], **kwargs)

    def test_cli_opt_in_and_full_output_compatible(self):
        self.add_job()
        plain = self.run_tool("runs.py", "recover", "--run", self.run["run_path"])
        value = json.loads(plain.stdout)
        self.assertIn("rounds", value)
        self.assertIn("completion", value["jobs"][0])
        self.assertNotIn("mode", value)
        compact = self.run_tool("runs.py", "recover", "--run", self.run["run_path"], "--summary")
        self.assertEqual(compact.returncode, 0, compact.stderr)
        value = json.loads(compact.stdout)
        self.assertEqual(value["mode"], "summary")
        self.assertNotIn("rounds", value)
        self.assertNotIn("completion", value["jobs"][0])
        self.assertFalse(value["executes_commands"])

    def test_scale_bounds_and_all_job_pages(self):
        for count in range(1, 101):
            self.add_job()
            if count not in (1, 10, 50, 100):
                continue
            full = runs.recover(self.run["run_path"], limit=1, now=101)
            summary = self.summary(limit=1, now=101)
            self.assertEqual(summary["counts"]["jobs"], count)
            self.assertEqual(summary["counts"]["rounds"], count)
            self.assertEqual(summary["job_counts"]["submission"], {"prepared": count})
            self.assertEqual(summary["job_counts"]["monitor_unknown"], count)
            self.assertEqual(len(summary["jobs"]), 1)
            self.assertEqual(summary["more_jobs"], count > 1)
            self.assertNotIn("rounds", summary)
            if count >= 10:
                self.assertLess(len(json.dumps(summary)), len(json.dumps(full)) / 2)
        pages, offset = [], 0
        while True:
            page = self.summary(limit=30, job_offset=offset, now=101)
            pages.extend(j["job_id"] for j in page["jobs"])
            if not page["more_jobs"]:
                break
            offset = page["next_job_offset"]
        self.assertEqual(pages, [j["job_id"] for j in full["jobs"]])
        self.assertEqual(len(set(pages)), 100)

    def test_watch_and_error_pages_remain_independent_and_partial(self):
        info = self.add_job()
        handles = [self.add_watch([info]) for _ in range(3)]
        for handle in handles:
            Path(handle).with_name("state.json").write_text("broken")
        first = self.summary(limit=1)
        second = self.summary(limit=1, watch_offset=1, error_offset=2, job_offset=10)
        self.assertFalse(first["complete"])
        self.assertEqual(first["counts"]["watches"], 3)
        self.assertEqual(first["counts"]["errors"], 3)
        self.assertEqual(first["health"]["unreadable_watches"], 3)
        self.assertTrue(first["more_watches"])
        self.assertTrue(first["more_errors"])
        self.assertNotEqual(first["watches"][0]["watch_id"], second["watches"][0]["watch_id"])
        self.assertEqual(second["next_watch_offset"], 2)
        self.assertEqual(second["next_error_offset"], 3)
        self.assertFalse(second["more_errors"])
        self.assertEqual(second["jobs"], [])
        outside = self.summary(limit=1, watch_offset=99, error_offset=99)
        self.assertEqual(outside["errors"], [])
        self.assertFalse(outside["complete"])
        self.assertEqual(outside["counts"]["errors"], 3)
        self.assertEqual(outside["health"], first["health"])

    def test_all_targets_and_off_page_health_are_visible(self):
        infos = [self.add_job() for _ in range(8)]
        handle = self.add_watch(infos)
        self.observe(handle)
        path = Path(handle).with_name("state.json")
        state = protocol.read_json(path)
        targets = [state["targets"][info["round_id"]] for info in infos]
        targets[0]["last_successful_check_at"] = None
        targets[1]["last_successful_check_at"] = 1
        targets[2]["last_successful_check_at"] = 200
        targets[3]["state"] = "dead"
        targets[4]["state"] = "unknown"
        targets[5]["state"] = {"unexpected": "state"}
        targets[6]["error"] = "transport offline"
        path.write_text(json.dumps(state))
        with watch.locked(Path(handle).parent):
            compact = self.summary(limit=1, now=101)
            self.assertEqual(compact["health"]["observer_inactive"], 0)
        health = self.summary(limit=1, watch_offset=10, now=101)["health"]
        self.assertTrue(compact["complete"])
        self.assertNotIn("targets", compact["watches"][0])
        self.assertEqual(health["targets"], 8)
        for field in ("success_missing", "success_stale", "success_future", "state_dead",
                      "state_unknown", "state_unrecognized", "read_errors", "observer_inactive"):
            self.assertEqual(health[field], 1, field)

    def test_clock_only_change_recomputes_monitor_and_success_expiry(self):
        info = self.add_job()
        handle = self.add_watch([info])
        self.observe(handle)
        revision = jobs.load(Path(self.run["index_path"]), info["job_id"])["revision"]
        state = jobs.claim(self.run["index_path"], info["job_id"], revision, "controller", 100, now=100)
        jobs.change(self.run["index_path"], info["job_id"], state["revision"], state["lease"]["token"],
                    "update", {"monitor": {"owner": "controller", "watch_path": handle,
                                         "expires_at": 130}}, now=100)
        before = self.summary(now=129)
        after = self.summary(now=130)
        self.assertEqual(before["health"]["success_stale"], 0)
        self.assertEqual(after["health"]["success_stale"], 1)
        self.assertEqual(before["job_counts"]["monitor_expired"], 0)
        self.assertEqual(after["job_counts"]["monitor_expired"], 1)
        self.assertEqual(after["checked_at"], 130)

    def test_filter_counts_keep_run_wide_integrity_errors(self):
        wanted, broken = self.add_job(), self.add_job()
        handle = self.add_watch([wanted, broken])
        self.observe(handle)
        (Path(self.run["index_path"]) / broken["job_id"] / "state.json").write_text("bad")
        summary = self.summary(job_id=wanted["job_id"], now=101)
        self.assertEqual(summary["counts"]["jobs"], 1)
        self.assertEqual(summary["counts"]["rounds"], 1)
        self.assertEqual(summary["health"]["targets"], 1)
        self.assertEqual(summary["job_counts"]["unreadable"], 0)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["counts"]["errors"], 1)
        self.assertEqual(self.summary()["job_counts"]["unreadable"], 1)

    def test_pending_queues_preserve_related_notes_and_exact_identities(self):
        info = self.add_job()
        old = self.add_watch([info])
        event = next(e for e in self.observe(old) if e["kind"] == "attention")
        reviews.record_review(old, event["seq"], "waiting_user", "已问一次，等待答复", 0, now=101)
        new = self.add_watch([info])
        self.observe(new, now=102)
        full = runs.recover(self.run["run_path"], now=103)
        compact = self.summary(limit=1, now=103)
        for field in ("action_required", "waiting_user"):
            self.assertEqual(compact[field], full[field])
        opened, waiting = compact["action_required"][0], compact["waiting_user"][0]
        self.assertEqual(opened["status"], "open")
        self.assertEqual(opened["revision"], 0)
        self.assertEqual(opened["related_reviews"][0]["note"], waiting["note"])
        self.assertNotEqual(opened["event_ref"], waiting["event_ref"])
        page = self.summary(limit=1, action_offset=1, waiting_offset=0, now=103)
        self.assertEqual(page["action_required"], [])
        self.assertEqual(page["waiting_user"], compact["waiting_user"])
        self.assertEqual(page["counts"]["open"], 1)
        Path(new).with_name("state.json").unlink()
        partial = self.summary(now=103)
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["action_required"], full["action_required"])

    def test_unregistered_round_remains_partial_without_adoption(self):
        info = self.add_job()
        member = Path(self.run["run_path"]).parent / "requests" / (info["round_id"] + ".json")
        member.unlink()
        summary = self.summary()
        self.assertFalse(summary["complete"])
        self.assertTrue(any("unregistered" in e["error"] for e in summary["errors"]))
        self.assertFalse(member.exists())

    def test_old_round_question_and_new_round_permission_stay_distinct(self):
        info = self.add_job()
        old = self.add_watch([info])
        event = next(e for e in self.observe(old) if e["kind"] == "attention")
        reviews.record_review(old, event["seq"], "waiting_user", "Old round question", 0, now=101)
        protocol.publish(info["result_path"], self.response(job_id=info["job_id"], round_id=info["round_id"]))
        follow = self.prepare(root=None, previous=info["request_path"], cwd=None, parent_depth=None)
        state = jobs.load(Path(self.run["index_path"]), info["job_id"])
        state = jobs.claim(self.run["index_path"], info["job_id"], state["revision"], "controller", 100, now=102)
        review = self.root / "round-review.txt"
        review.write_text("Fixture response reviewed before continuation")
        state = jobs.change(self.run["index_path"], info["job_id"], state["revision"], state["lease"]["token"],
                            "accept", {"kind": "answer", "verdict": "accepted", "evidence_path": str(review),
                                       "note": "Reviewed fixture answer"}, now=102)
        jobs.change(self.run["index_path"], info["job_id"], state["revision"], state["lease"]["token"],
                    "activate", {"request_path": follow["request_path"]}, now=102)
        new = self.add_watch([follow])
        self.observe(new, now=103)
        summary = self.summary(now=104)
        self.assertEqual(summary["counts"]["rounds"], 2)
        self.assertEqual(summary["counts"]["jobs"], 1)
        opened, waiting = summary["action_required"][0], summary["waiting_user"][0]
        self.assertFalse(waiting["is_current_round"])
        self.assertTrue(opened["is_current_round"])
        self.assertEqual(opened["related_reviews"], [])
        self.assertEqual(opened["status"], "open")
        self.assertEqual(waiting["note"], "Old round question")
        self.assertNotEqual(opened["event_ref"], waiting["event_ref"])

    def test_pending_pages_advance_independently_without_hiding_totals(self):
        infos = [self.add_job() for _ in range(3)]
        old = self.add_watch(infos)
        for event in self.observe(old):
            if event["kind"] == "attention":
                reviews.record_review(old, event["seq"], "waiting_user", "Already asked", 0, now=101)
        self.observe(self.add_watch(infos), now=102)
        full = runs.recover(self.run["run_path"], now=103)
        page = self.summary(limit=1, action_offset=1, waiting_offset=2, now=103)
        self.assertEqual(page["action_required"], full["action_required"][1:2])
        self.assertEqual(page["waiting_user"], full["waiting_user"][2:3])
        self.assertTrue(page["more_action_required"])
        self.assertFalse(page["more_waiting_user"])
        self.assertEqual(page["counts"]["open"], 3)
        self.assertEqual(page["counts"]["waiting_user"], 3)
        self.assertEqual(page["next_action_offset"], 2)
        self.assertEqual(page["next_waiting_offset"], 3)

    def test_summary_does_not_read_terminals_or_mutate_records(self):
        handle = self.add_watch([self.add_job()])
        self.observe(handle)
        directory = Path(self.run["run_path"]).parent
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.rglob("*") if p.is_file()}
        with patch.object(watch, "observe", side_effect=AssertionError("terminal read")):
            self.summary(now=101)
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_invalid_arguments_fail_and_summary_only_flags_are_not_ignored(self):
        for kwargs in ({"limit": 0}, {"limit": 201}, {"job_offset": -1}, {"watch_offset": True},
                       {"error_offset": 1.5}, {"waiting_offset": -1}, {"action_offset": -1},
                       {"now": float("nan")}, {"job_id": "bad"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.summary(**kwargs)
        for option in ("--job-offset", "--watch-offset", "--error-offset"):
            value = self.run_tool("runs.py", "recover", "--run", self.run["run_path"], option, "1")
            self.assertNotEqual(value.returncode, 0)
            self.assertIn("--summary", value.stderr)
