"""Incremental recovery must preserve pending work and conservative invalidation."""

import json
import os
from pathlib import Path
from unittest.mock import patch

from test_token_tools import ToolCase
import test_recovery_summary as fixtures
import jobs
import protocol
import reviews
import runs
import watch


class RecoveryDeltaTests(ToolCase):
    add_job = fixtures.RecoverySummaryTests.add_job
    add_watch = fixtures.RecoverySummaryTests.add_watch
    observe = fixtures.RecoverySummaryTests.observe

    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.run = runs.init_run()

    def delta(self, previous=None, **kwargs):
        if previous is not None:
            kwargs.setdefault("cursor_path", previous["cursor_path"])
            kwargs.setdefault("since", previous["next_cursor"])
        kwargs.setdefault("now", 101)
        return runs.recover_delta(self.run["run_path"], **kwargs)

    def test_first_page_and_unchanged_resume_across_processes(self):
        self.add_job()
        command = ("recover", "--run", self.run["run_path"], "--delta", "--limit", "1")
        first = self.run_tool("runs.py", *command)
        self.assertEqual(first.returncode, 0, first.stderr)
        first = json.loads(first.stdout)
        self.assertTrue(first["reset"])
        self.assertEqual(first["job_changes"][0]["op"], "upsert")
        second = self.run_tool("runs.py", *command, "--cursor", first["cursor_path"],
                               "--since", first["next_cursor"])
        self.assertEqual(second.returncode, 0, second.stderr)
        second = json.loads(second.stdout)
        self.assertFalse(second["reset"])
        self.assertEqual(second["job_changes"], [])
        self.assertEqual(second["counts"]["jobs"], 1)
        self.assertGreater(second["read_stats"]["cache_hits"], 0)
        self.assertLess(second["read_stats"]["source_reads"], first["read_stats"]["source_reads"])

    def test_bounded_changes_drain_without_skipping_and_with_fair_order(self):
        for _ in range(10):
            self.add_job()
        page = self.delta(limit=1)
        ids = [page["job_changes"][0]["job_id"]]
        self.assertEqual(page["change_counts"]["jobs"], 10)
        first_id = ids[0]
        for _ in range(9):
            state = jobs.load(Path(self.run["index_path"]), first_id)
            if state["lease"] is None:
                jobs.claim(self.run["index_path"], first_id, state["revision"], "controller", 100, now=100)
            else:
                jobs.change(self.run["index_path"], first_id, state["revision"], state["lease"]["token"],
                            "renew", {"lease_seconds": 100}, now=100)
            page = self.delta(page, limit=1)
            ids.append(page["job_changes"][0]["job_id"])
        self.assertEqual(len(set(ids)), 10)
        final = self.delta(page, limit=1)
        self.assertEqual(final["job_changes"][0]["job_id"], first_id)
        self.assertFalse(final["more_job_changes"])

    def test_lost_output_stale_missing_corrupt_and_scope_reset(self):
        wanted = self.add_job()
        first = self.delta()
        newer = self.delta(first)
        lost = self.delta(first)
        self.assertTrue(lost["reset"])
        self.assertEqual(lost["reset_reason"], "token_mismatch")
        self.assertEqual(len(lost["job_changes"]), 1)
        no_token = self.delta(cursor_path=lost["cursor_path"])
        self.assertTrue(no_token["reset"])
        scoped = self.delta(no_token, job_id=wanted["job_id"])
        self.assertEqual(scoped["reset_reason"], "scope_changed")
        path = Path(scoped["cursor_path"])
        path.write_text("broken")
        broken = self.delta(scoped)
        self.assertEqual(broken["reset_reason"], "cursor_invalid")
        path.unlink()
        missing = self.delta(broken)
        self.assertEqual(missing["reset_reason"], "cursor_missing")
        self.assertNotEqual(newer["next_cursor"], missing["next_cursor"])

    def test_rewrite_with_restored_mtime_atomic_replace_and_symlink_invalidate(self):
        info = self.add_job()
        path = Path(info["request_path"])
        original = path.read_bytes()
        previous = self.delta()
        stamp = path.stat()
        path.write_bytes(original.replace(b"Review", b"review"))
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        changed = self.delta(previous)
        self.assertFalse(changed["complete"])
        self.assertGreater(changed["counts"]["errors"], 0)
        replacement = path.with_name("replace.json")
        replacement.write_bytes(original)
        os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        replacement.replace(path)
        repaired = self.delta(changed)
        self.assertTrue(repaired["complete"])
        replacement.write_bytes(original)
        path.unlink()
        path.symlink_to(replacement)
        redirected = self.delta(repaired)
        self.assertFalse(redirected["complete"])

    def test_pending_events_and_notes_repeat_even_when_no_row_changes(self):
        info = self.add_job()
        old = self.add_watch([info])
        event = next(e for e in self.observe(old) if e["kind"] == "attention")
        reviews.record_review(old, event["seq"], "waiting_user", "已经询问", 0, now=101)
        newer = self.add_watch([info])
        self.observe(newer, now=102)
        first = self.delta(now=103)
        second = self.delta(first, now=103)
        self.assertEqual(second["job_changes"], [])
        self.assertEqual(second["watch_changes"], [])
        full = runs.recover(self.run["run_path"], now=103)
        for field in ("action_required", "waiting_user"):
            self.assertEqual(second[field], full[field])
        opened = second["action_required"][0]
        self.assertEqual(opened["status"], "open")
        self.assertEqual(opened["related_reviews"][0]["note"], "已经询问")
        reviews.record_review(newer, opened["seq"], "handled", "Reconciled live UI", 0, now=104)
        third = self.delta(second, now=105)
        self.assertEqual(third["counts"]["open"], 0)
        self.assertEqual(third["counts"]["waiting_user"], 1)

    def test_time_only_expiry_and_observer_lock_are_live(self):
        info = self.add_job()
        handle = self.add_watch([info])
        self.observe(handle)
        state = jobs.load(Path(self.run["index_path"]), info["job_id"])
        state = jobs.claim(self.run["index_path"], info["job_id"], state["revision"], "owner", 100, now=100)
        jobs.change(self.run["index_path"], info["job_id"], state["revision"], state["lease"]["token"],
                    "update", {"monitor": {"owner": "owner", "watch_path": handle, "expires_at": 130}}, now=100)
        with watch.locked(Path(handle).parent):
            first = self.delta(now=129)
        after = self.delta(first, now=130)
        self.assertEqual(after["health"]["observer_inactive"], 1)
        self.assertEqual(after["health"]["success_stale"], 1)
        self.assertEqual(after["job_counts"]["monitor_expired"], 1)
        self.assertEqual(len(after["job_changes"]), 1)
        self.assertEqual(len(after["watch_changes"]), 1)

    def test_new_round_watch_unregistered_artifact_and_partial_errors(self):
        info = self.add_job()
        first = self.delta()
        handle = self.add_watch([info])
        self.observe(handle)
        watch_added = self.delta(first)
        self.assertEqual(len(watch_added["watch_changes"]), 1)
        protocol.publish(info["result_path"], self.response(job_id=info["job_id"], round_id=info["round_id"]))
        follow = self.prepare(root=None, previous=info["request_path"], cwd=None, parent_depth=None)
        state = jobs.load(Path(self.run["index_path"]), info["job_id"])
        state = jobs.claim(self.run["index_path"], info["job_id"], state["revision"], "owner", 100, now=100)
        jobs.change(self.run["index_path"], info["job_id"], state["revision"], state["lease"]["token"],
                    "activate", {"request_path": follow["request_path"]}, now=100)
        advanced = self.delta(watch_added)
        self.assertEqual(advanced["job_changes"][0]["value"]["round_id"], follow["round_id"])
        Path(handle).with_name("state.json").unlink()
        unregistered = Path(self.run["rounds_root"]) / "orphan"
        unregistered.mkdir()
        broken = self.delta(advanced, error_offset=99)
        self.assertFalse(broken["complete"])
        self.assertGreaterEqual(broken["counts"]["errors"], 2)
        self.assertEqual(broken["errors"], [])
        self.assertTrue(broken["action_required"])

    def test_cursor_writes_are_isolated_and_full_summary_stays_read_only(self):
        self.add_job()
        directory = Path(self.run["run_path"]).parent
        before = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
        with patch.object(watch, "observe", side_effect=AssertionError("terminal read")):
            first = self.delta()
            self.delta(first)
            runs.recover_summary(self.run["run_path"])
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)
        added = set(p for p in directory.rglob("*") if p.is_file()) - before.keys()
        self.assertTrue(added)
        self.assertTrue(all(p.is_relative_to(directory / "recovery") for p in added))
        self.assertEqual(Path(first["cursor_path"]).stat().st_mode & 0o777, 0o600)

    def test_cursor_path_rejects_source_file_and_symlink(self):
        info = self.add_job()
        with self.assertRaises(ValueError):
            self.delta(cursor_path=info["request_path"])
        first = self.delta()
        path = Path(first["cursor_path"])
        original = path.read_bytes()
        path.unlink()
        target = self.root / "foreign.json"
        target.write_bytes(original)
        path.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            self.delta(first)
        self.assertEqual(target.read_bytes(), original)

    def test_changed_filters_keep_run_errors_and_no_removals_on_partial_reads(self):
        wanted, broken = self.add_job(), self.add_job()
        handle = self.add_watch([broken])
        first = self.delta()
        path = Path(handle)
        path.write_text("invalid")
        partial = self.delta(first)
        self.assertFalse(partial["complete"])
        self.assertFalse(any(x["op"] == "remove" for x in partial["watch_changes"]))
        filtered = self.delta(partial, job_id=wanted["job_id"])
        self.assertTrue(filtered["reset"])
        self.assertEqual(filtered["counts"]["jobs"], 1)
        self.assertFalse(filtered["complete"])

    def test_cli_rejects_incompatible_flags(self):
        for args in (("--cursor", "/unused"), ("--since", "a" * 32),
                     ("--delta", "--summary"), ("--delta", "--job-offset", "1"),
                     ("--delta", "--watch-offset", "1"), ("--delta", "--limit", "0")):
            result = self.run_tool("runs.py", "recover", "--run", self.run["run_path"], *args)
            self.assertNotEqual(result.returncode, 0)

    def test_cursor_checksum_version_and_malformed_cache_fall_back(self):
        import recovery_delta
        self.add_job()
        previous = self.delta()
        path = Path(previous["cursor_path"])
        envelope = protocol.read_json(path)
        envelope["state"]["files"] = {}
        path.write_text(json.dumps(envelope))
        previous = self.delta(previous)
        self.assertEqual(previous["reset_reason"], "cursor_invalid")
        for field, value, reason in (("version", 0, "version_changed"),
                                     ("files", {"/invalid": {"signature": []}}, "cursor_invalid")):
            envelope = protocol.read_json(path)
            envelope["state"][field] = value
            envelope["sha256"] = recovery_delta.digest(envelope["state"])
            path.write_text(json.dumps(envelope))
            previous = self.delta(previous)
            self.assertEqual(previous["reset_reason"], reason)
            self.assertTrue(previous["complete"])
            self.assertEqual(len(previous["job_changes"]), 1)

    def test_bounded_cache_falls_back_to_reads_without_dropping_rows(self):
        import recovery_delta
        for _ in range(3):
            self.add_job()
        with patch.object(recovery_delta, "MAX_CACHE_BYTES", 1):
            first = self.delta()
            second = self.delta(first)
        self.assertTrue(second["complete"])
        self.assertEqual(second["counts"]["jobs"], 3)
        self.assertEqual(second["job_changes"], [])
        self.assertEqual(second["read_stats"]["retained_bytes"], 0)
        self.assertGreater(second["read_stats"]["source_reads"], 1)

    def test_removal_is_emitted_only_after_complete_recovery(self):
        info = self.add_job()
        first = self.delta()
        (Path(self.run["index_path"]) / info["job_id"]).rename(self.root / "retired-job")
        second = self.delta(first)
        self.assertTrue(second["complete"])
        self.assertEqual(second["job_changes"], [{"op": "remove", "job_id": info["job_id"]}])
        self.assertEqual(second["counts"]["unindexed_rounds"], 1)
        self.assertEqual(self.delta(second)["job_changes"], [])

    def test_cursor_lock_contention_leaves_previous_cursor_intact(self):
        import recovery_delta
        self.add_job()
        first = self.delta()
        path = Path(first["cursor_path"])
        original = path.read_bytes()
        with recovery_delta.cursor_lock(path), self.assertRaises(BlockingIOError):
            self.delta(first)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.delta(first)["reset"])

    def test_cache_does_not_bypass_file_type_size_or_context_boundaries(self):
        import recovery_delta
        path = self.root / "cached.json"
        path.write_text('{"ok": true}')
        cache = recovery_delta.ReadCache()
        with protocol.cached_reads(cache):
            self.assertEqual(protocol.read_json(path), {"ok": True})
            with self.assertRaises(ValueError):
                protocol.read_bytes(path, max_bytes=1)
            path.unlink()
            os.mkfifo(path)
            with self.assertRaises(ValueError):
                protocol.read_json(path)
        self.assertIsNone(protocol._READ_CACHE.get())

    def test_failed_cursor_save_does_not_advance_delivery(self):
        import recovery_delta
        self.add_job()
        first = self.delta()
        self.add_job()
        before = Path(first["cursor_path"]).read_bytes()
        with patch.object(recovery_delta.os, "replace", side_effect=OSError("disk failure")), self.assertRaises(OSError):
            self.delta(first)
        self.assertEqual(Path(first["cursor_path"]).read_bytes(), before)
        resumed = self.delta(first)
        self.assertFalse(resumed["reset"])
        self.assertEqual(len(resumed["job_changes"]), 1)
        self.assertFalse(list(Path(first["cursor_path"]).parent.glob(".cursor-*")))

    def test_unchanged_content_is_not_rewritten_and_interrupted_pair_resets(self):
        import recovery_delta
        self.add_job()
        first = self.delta()
        content_path = Path(first["cursor_path"]).with_name("contents.json")
        before = (content_path.read_bytes(), content_path.stat().st_mtime_ns)
        second = self.delta(first)
        self.assertEqual(second["read_stats"]["cache_write_bytes"], 0)
        self.assertEqual((content_path.read_bytes(), content_path.stat().st_mtime_ns), before)
        self.add_job()
        original = recovery_delta.write_atomic

        def interrupted(path, raw):
            if path.name == "cursor.json":
                raise OSError("interrupted after content publication")
            return original(path, raw)

        with patch.object(recovery_delta, "write_atomic", side_effect=interrupted), self.assertRaises(OSError):
            self.delta(second)
        repaired = self.delta(second)
        self.assertEqual(repaired["reset_reason"], "cursor_invalid")
        self.assertEqual(len(repaired["job_changes"]), 2)
        self.assertTrue(repaired["complete"])
