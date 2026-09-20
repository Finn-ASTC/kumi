"""Persistent storage and cross-watch recovery retain identity and unanswered decisions."""

import json
import os
from pathlib import Path
from unittest.mock import patch

from test_token_tools import ToolCase
import protocol
import jobs
import reviews
import watch
import runs


class RunTests(ToolCase):
    def test_replacing_legacy_watch_preserves_waiting_user_and_first_publication(self):
        info, run = self.managed()
        self.record_for(info)
        handle = watch.init_watch([info['request_path']])['watch_path']
        protocol.publish(info['result_path'], self.response_for(info))
        event = next(e for e in self.observe(handle) if e['kind'] == 'attention')
        reviews.record_review(handle, event['seq'], 'waiting_user', 'Question already asked', 0, now=101)
        pin = Path(info['request_path']).parent / 'publication.json'
        original_pin = pin.read_bytes()
        # Model a pre-upgrade persisted manifest and its matching registry hash.
        config = protocol.read_json(handle)
        config['targets'][0].pop('request_sha256')
        Path(handle).write_text(json.dumps(config))
        registry = Path(run['run_path']).parent / 'watch-registry' / (config['watch_id'] + '.json')
        entry = protocol.read_json(registry)
        entry['config_sha256'] = runs.fingerprint(handle)
        registry.write_text(json.dumps(entry))
        self.assertIn('observation_error', [e['kind'] for e in self.observe(handle, now=102)])
        replacement = watch.init_watch([info['request_path']])['watch_path']
        self.observe(replacement, now=103)
        recovered = runs.recover(run['run_path'], now=104)
        self.assertTrue(recovered['complete'])
        self.assertEqual(recovered['waiting_user'][0]['watch_path'], handle)
        self.assertEqual(recovered['waiting_user'][0]['note'], 'Question already asked')
        self.assertEqual(pin.read_bytes(), original_pin)
        self.assertEqual(len(recovered['watches']), 2)

    def test_rewrite_alert_remains_recoverable_across_replacement_watches(self):
        info, run = self.managed()
        self.record_for(info)
        path = Path(info['result_path'])
        protocol.publish(path, self.response_for(info))
        first = watch.init_watch([info['request_path']])['watch_path']
        self.observe(first)
        path.write_text(json.dumps(self.response_for(info, output='rewritten answer')))
        second = watch.init_watch([info['request_path']])['watch_path']
        event = next(e for e in self.observe(second) if e['kind'] == 'result_changed')
        recovered = runs.recover(run['run_path'])
        self.assertTrue(recovered['complete'])
        item = next(x for x in recovered['action_required'] if x['kind'] == 'result_changed')
        self.assertEqual((item['watch_path'], item['seq'], item['priority']), (second, event['seq'], 0))
        self.assertEqual(protocol.read_json(item['evidence_path'])['publication']['path'],
                         str(Path(info['request_path']).parent / 'publication.json'))

    def setUp(self):
        super().setUp()
        self.state_home = self.root / "state home"
        self.environment = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def managed(self, **overrides):
        info = self.prepare(root=None, **overrides)
        return info, runs.load_run(info["run_path"])

    def record_for(self, info, pane="wA:p1"):
        result = self.cli("record", "--request", info["request_path"], "--mode", "isolated",
                          "--session", "fixture", "--agent", "worker", "--pane", pane)
        self.assertEqual(result.returncode, 0, result.stderr)

    def observe(self, handle, now=100, screen="Command: inspect data\nAllow once / Deny"):
        with patch.object(watch, "observe", return_value={"state": "working", "screen": screen}):
            return watch.poll_watch(handle, now=now)["events"]

    def response_for(self, info, **kwargs):
        return self.response(job_id=info["job_id"], round_id=info["round_id"], **kwargs)

    def test_correlated_replacement_keeps_question_note_without_inheriting_review(self):
        first, run = self.managed()
        self.record_for(first)
        old = watch.init_watch([first["request_path"]])["watch_path"]
        original = next(e for e in self.observe(old) if e["kind"] == "attention")
        reviews.record_review(old, original["seq"], "waiting_user", "Asked exactly once", 0, now=101)
        new = watch.init_watch([first["request_path"]])["watch_path"]
        current = next(e for e in self.observe(new, now=102) if e["kind"] == "attention")
        self.assertEqual(original["correlation_key"], current["correlation_key"])
        recovered = runs.recover(run["run_path"], now=103)
        opened = next(e for e in recovered["action_required"] if e["kind"] == "attention")
        self.assertEqual(opened["status"], "open")
        self.assertEqual(opened["revision"], 0)
        self.assertEqual(opened["related_reviews"][0]["note"], "Asked exactly once")
        self.assertEqual(opened["related_reviews"][0]["watch_path"], old)
        self.assertEqual(recovered["counts"], {"open": 1, "waiting_user": 1})
        reviews.record_review(old, original["seq"], "handled", "Old target checked", 1, now=104)
        again = runs.recover(run["run_path"], now=105)
        self.assertEqual(again["action_required"][0]["status"], "open")
        self.assertEqual(again["action_required"][0]["related_reviews"][0]["status"], "handled")
        changed = next(e for e in self.observe(new, now=106, screen="Command: delete data\nAllow once / Deny")
                       if e["kind"] == "attention")
        item = next(e for e in runs.recover(run["run_path"])["action_required"] if e["seq"] == changed["seq"])
        self.assertEqual(item["related_reviews"], [])
        # Follow-up rounds preserve historical waiting records, but cannot borrow a correlation key.
        protocol.publish(first["result_path"], self.response_for(first))
        follow, _ = self.managed(cwd=None, parent_depth=None, previous=first["request_path"])
        last = watch.init_watch([follow["request_path"]])["watch_path"]
        successor = next(e for e in self.observe(last) if e["kind"] == "attention")
        self.assertNotEqual(original["correlation_key"], successor["correlation_key"])

    def test_default_prepare_is_persistent_and_followup_inherits_run(self):
        info, run = self.managed()
        self.assertTrue(Path(info["request_path"]).is_relative_to(self.state_home))
        self.assertEqual(Path(info["request_path"]).parent.parent, Path(run["rounds_root"]))
        self.assertEqual(runs.run_for_request(info["request_path"])["run_id"], run["run_id"])
        protocol.publish(info["result_path"], self.response_for(info, status="blocked", blocked_reason="Format?"))
        follow, again = self.managed(cwd=None, parent_depth=None, previous=info["request_path"])
        self.assertEqual(again, run)
        self.assertNotEqual(follow["round_id"], info["round_id"])
        self.assertEqual(len(runs.recover(run["run_path"])["rounds"]), 2)

    def test_explicit_root_retains_legacy_behavior_and_temporary_is_explicit(self):
        self.assertIsNone(runs.run_for_request(self.info["request_path"]))
        info, run = self.managed(temporary=True)
        self.assertTrue(run["temporary"])
        self.assertFalse(Path(info["request_path"]).is_relative_to(self.state_home))
        # Test-created ephemeral run is owned exclusively by this test.
        import shutil
        self.addCleanup(shutil.rmtree, Path(run["run_path"]).parent)

    def test_shared_run_and_default_watch_registration(self):
        first, run = self.managed()
        second, same = self.managed(run=run["run_path"])
        self.record_for(first)
        self.record_for(second, "wB:p1")
        handle = watch.init_watch([first["request_path"], second["request_path"]])["watch_path"]
        self.assertEqual(same, run)
        self.assertEqual(Path(handle).parent.parent, Path(run["watches_root"]))
        snapshot = runs.recover(run["run_path"])
        self.assertEqual(len(snapshot["watches"]), 1)
        self.assertEqual(snapshot["watches"][0]["watch_path"], handle)
        self.assertTrue(snapshot["complete"])

    def test_old_waiting_and_new_same_seq_stay_distinct_after_process_restart(self):
        first, run = self.managed()
        self.record_for(first)
        state = jobs.register(run["index_path"], first["request_path"], now=100)
        state = jobs.claim(run["index_path"], first["job_id"], state["revision"], "controller", 100, now=100)
        old = watch.init_watch([first["request_path"]])["watch_path"]
        old_event = next(e for e in self.observe(old) if e["kind"] == "attention")
        reviews.record_review(old, old_event["seq"], "waiting_user", "已询问数据格式；等待答复", 0, now=101)
        protocol.publish(first["result_path"], self.response_for(first, status="blocked", blocked_reason="Format?"))
        follow, _ = self.managed(cwd=None, parent_depth=None, previous=first["request_path"])
        jobs.change(run["index_path"], first["job_id"], state["revision"], state["lease"]["token"],
                    "activate", {"request_path": follow["request_path"]}, now=102)
        new = watch.init_watch([follow["request_path"]])["watch_path"]
        new_event = next(e for e in self.observe(new, now=103) if e["kind"] == "attention")
        self.assertEqual(old_event["seq"], new_event["seq"])
        value = self.run_tool("runs.py", "recover", "--run", run["run_path"])
        self.assertEqual(value.returncode, 0, value.stderr)
        recovered = json.loads(value.stdout)
        self.assertEqual(recovered["counts"], {"open": 1, "waiting_user": 1})
        waiting, opened = recovered["waiting_user"][0], recovered["action_required"][0]
        self.assertNotEqual(waiting["event_ref"], opened["event_ref"])
        self.assertEqual(waiting["note"], "已询问数据格式；等待答复")
        self.assertFalse(waiting["is_current_round"])
        self.assertTrue(opened["is_current_round"])
        self.assertTrue(all(w["observer_active"] is False for w in recovered["watches"]))
        reviews.record_review(new, new_event["seq"], "handled", "checked current UI", 0)
        again = runs.recover(run["run_path"])
        self.assertEqual(again["counts"], {"open": 0, "waiting_user": 1})

    def test_pagination_job_filter_and_duplicate_attachment(self):
        first, run = self.managed()
        second, _ = self.managed(run=run["run_path"])
        for info, pane in ((first, "wA:p1"), (second, "wB:p1")):
            self.record_for(info, pane)
            handle = watch.init_watch([info["request_path"]])["watch_path"]
            self.observe(handle)
            runs.attach_watch(run["run_path"], handle)
        page1 = runs.recover(run["run_path"], limit=1)
        self.assertEqual(page1["counts"]["open"], 2)
        self.assertTrue(page1["more_action_required"])
        page2 = runs.recover(run["run_path"], limit=1, action_offset=page1["next_action_offset"])
        self.assertNotEqual(page1["action_required"][0]["event_ref"], page2["action_required"][0]["event_ref"])
        filtered = runs.recover(run["run_path"], job_id=first["job_id"])
        self.assertEqual(filtered["counts"]["open"], 1)
        self.assertEqual(filtered["action_required"][0]["job_id"], first["job_id"])

    def test_foreign_watch_and_mixed_runs_are_not_silently_attached(self):
        first, run = self.managed()
        foreign, _ = self.managed()
        self.record_for(first)
        self.record_for(foreign, "wB:p1")
        other = watch.init_watch([foreign["request_path"]])["watch_path"]
        with self.assertRaises(ValueError):
            runs.attach_watch(run["run_path"], other)
        with self.assertRaises(ValueError):
            watch.init_watch([first["request_path"], foreign["request_path"]])
        with self.assertRaises(ValueError):
            jobs.register(self.root / "wrong-index", first["request_path"])

    def test_missing_or_replaced_watch_is_partial_failure_not_empty_queue(self):
        first, run = self.managed()
        self.record_for(first)
        old = watch.init_watch([first["request_path"]])["watch_path"]
        new = watch.init_watch([first["request_path"]])["watch_path"]
        self.observe(old)
        self.observe(new)
        original = Path(old).read_text()
        Path(old).unlink()
        recovered = runs.recover(run["run_path"])
        self.assertFalse(recovered["complete"])
        self.assertEqual(len(recovered["errors"]), 1)
        self.assertEqual(recovered["counts"]["open"], 1)
        Path(old).write_text(original + "\n")
        self.assertFalse(runs.recover(run["run_path"])["complete"])

    def test_observer_lock_and_successful_check_time_are_independent(self):
        first, run = self.managed()
        self.record_for(first)
        handle = watch.init_watch([first["request_path"]])["watch_path"]
        self.observe(handle, now=100)
        with patch.object(watch, "observe", side_effect=OSError("disconnected")):
            watch.poll_watch(handle, now=110)
        with watch.locked(Path(handle).parent):
            value = self.run_tool("runs.py", "recover", "--run", run["run_path"])
        self.assertEqual(value.returncode, 0, value.stderr)
        health = json.loads(value.stdout)["watches"][0]
        self.assertTrue(health["observer_active"])
        self.assertEqual(health["last_checked_at"], 110)
        self.assertEqual(health["targets"][0]["last_successful_check_at"], 100)
        self.assertEqual(health["targets"][0]["last_error"], "disconnected")

    def test_default_catalog_is_read_only_and_missing_run_does_not_create_files(self):
        with self.assertRaises(FileNotFoundError):
            runs.recover(self.root / "absent" / "run.json")
        self.assertFalse((self.root / "absent").exists())
        self.assertEqual(runs.list_runs()["runs"], [])
        self.assertFalse(self.state_home.exists())
        info, run = self.managed()
        self.assertEqual(runs.list_runs()["runs"][0]["run_path"], run["run_path"])

    def test_relative_xdg_state_home_falls_back_to_user_state_directory(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": "relative-state"}), patch.object(Path, "home", return_value=self.root):
            self.assertEqual(runs.state_root(), self.root / ".local/state/agent-orchestrator")

    def test_run_manifests_cannot_redirect_storage_or_hide_lost_membership(self):
        info, run = self.managed()
        protocol.publish(info["result_path"], self.response_for(info))
        with self.assertRaises(ValueError):
            self.prepare(cwd=None, parent_depth=None, previous=info["request_path"], root=str(self.root))
        member = Path(run["run_path"]).parent / "requests" / (info["round_id"] + ".json")
        member.unlink()
        with self.assertRaises(ValueError):
            runs.run_for_request(info["request_path"])
        value = dict(run, index_path=str(self.root / "foreign"))
        Path(run["run_path"]).write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            runs.load_run(run["run_path"])

    def test_orphaned_managed_artifacts_are_visible_and_attachment_can_be_retried(self):
        info, run = self.managed()
        self.record_for(info)
        handle = watch.init_watch([info["request_path"]])["watch_path"]
        self.observe(handle)
        registry = Path(run["run_path"]).parent / "watch-registry"
        next(registry.glob("*.json")).unlink()
        result = runs.recover(run["run_path"])
        self.assertFalse(result["complete"])
        self.assertTrue(any(e["path"] == handle for e in result["errors"]))
        runs.attach_watch(run["run_path"], handle)
        self.assertEqual(runs.recover(run["run_path"])["counts"]["open"], 1)
        member = Path(run["run_path"]).parent / "requests" / (info["round_id"] + ".json")
        member.unlink()
        self.assertFalse(runs.recover(run["run_path"])["complete"])
        restored = self.run_tool("runs.py", "attach-request", "--run", run["run_path"], "--request", info["request_path"])
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertFalse(json.loads(restored.stdout)["submission_inferred"])
        self.assertTrue(runs.recover(run["run_path"])["complete"])

    def test_waiting_pagination_missing_health_and_read_only_recovery(self):
        info, run = self.managed()
        self.record_for(info)
        handles = []
        for number in range(3):
            handle = watch.init_watch([info["request_path"]])["watch_path"]
            handles.append(handle)
            event = next(e for e in self.observe(handle, now=100 + number) if e["kind"] == "attention")
            reviews.record_review(handle, event["seq"], "waiting_user", f"Asked question {number}", 0)
        (Path(handles[0]).parent / "state.json").unlink()
        before = {str(p): p.read_bytes() for p in Path(run["run_path"]).parent.rglob('*') if p.is_file()}
        with patch.object(watch, "observe", side_effect=AssertionError("recovery must not probe terminal")):
            first = runs.recover(run["run_path"], limit=1)
            second = runs.recover(run["run_path"], limit=1, waiting_offset=first["next_waiting_offset"])
        self.assertFalse(first["complete"])
        self.assertEqual(first["counts"]["waiting_user"], 3)
        self.assertNotEqual(first["waiting_user"][0]["event_ref"], second["waiting_user"][0]["event_ref"])
        after = {str(p): p.read_bytes() for p in Path(run["run_path"]).parent.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        (Path(handles[0]).parent / "state.json").write_text('{"targets": []}')
        corrupt = runs.recover(run["run_path"])
        self.assertFalse(corrupt["complete"])
        self.assertEqual(corrupt["counts"]["waiting_user"], 3)

    def test_run_index_rejects_unmanaged_requests_and_recovery_detects_lost_binding(self):
        info, run = self.managed()
        self.record_for(info)
        with self.assertRaises(ValueError):
            jobs.register(run["index_path"], self.info["request_path"])
        jobs.register(run["index_path"], info["request_path"])
        (Path(info["request_path"]).parent / "run-ref.json").unlink()
        with self.assertRaises(ValueError):
            jobs.recover(run["index_path"], info["job_id"])

    def test_corrupt_watch_identity_and_closed_job_pending_are_not_hidden(self):
        info, run = self.managed()
        self.record_for(info)
        handle = watch.init_watch([info["request_path"]])["watch_path"]
        self.observe(handle)
        state = jobs.register(run["index_path"], info["request_path"])
        state = jobs.claim(run["index_path"], info["job_id"], state["revision"], "owner")
        proof = self.root / "cancel.txt"
        proof.write_text("Confirmed no task was started")
        jobs.change(run["index_path"], info["job_id"], state["revision"], state["lease"]["token"], "close",
                    {"outcome": "cancelled", "evidence_path": str(proof), "note": "Stopped; historical review retained"})
        self.assertEqual(runs.recover(run["run_path"])["counts"]["open"], 1)
        entry_path = next((Path(run["run_path"]).parent / "watch-registry").glob("*.json"))
        record = protocol.read_json(entry_path)
        record["watch_id"] = "a" * 32
        entry_path.unlink()
        (entry_path.parent / ('a' * 32 + '.json')).write_text(json.dumps(record))
        self.assertFalse(runs.recover(run["run_path"])["complete"])
