"""Recorded Hermes request occurrences must preserve retries, gaps and native ownership."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from test_token_tools import ToolCase, SCRIPTS
import native_usage
import usage


class HermesHookTests(ToolCase):
    def setUp(self):
        super().setUp()
        path = SCRIPTS.parents[1] / "agent-hermes/plugins/orch-usage/recorder.py"
        spec = importlib.util.spec_from_file_location("orch_test_recorder", path)
        self.recorder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.recorder)
        self.db = self.root / "usage-hooks/events.sqlite3"

    def event(self, kind, turn="turn-1", request="request-1", **extra):
        return self.recorder.record_event(self.root, kind, dict(session_id="session-1", turn_id=turn,
            api_request_id=request, model="m", provider="p", **extra))

    def call(self, turn="turn-1", request="request-1", **extra):
        self.event("request_start", turn, request)
        self.event("request_end", turn, request, usage=dict(input_tokens=30, output_tokens=20,
            cache_read_tokens=60, cache_write_tokens=10, reasoning_tokens=8, prompt_tokens=100, total_tokens=120), **extra)

    def closed(self):
        self.event("turn_start")
        self.call()
        self.event("turn_end", completed=True, failed=False, interrupted=False)

    def inspect(self):
        return native_usage.inspect_log("hermes", str(self.db), "session-1")

    def manifest(self, turn="turn-1"):
        path = self.root / "import.json"
        path.write_text(json.dumps(dict(version=1, host="hermes", log_path=str(self.db), session_id="session-1",
            mappings=[dict(native_id=turn, request_path=self.info["request_path"], attempt_id="main", purpose="task")])) )
        return str(path)

    def test_closed_turn_imports_and_reimport_deduplicates(self):
        self.closed()
        report = self.inspect()
        self.assertEqual(report["turns"][0]["native_id"], "turn-1")
        self.assertFalse(report["coverage_complete"])
        self.assertEqual(native_usage.import_manifest(self.manifest(), True)["selected_calls"], 1)
        self.assertEqual(native_usage.import_manifest(self.manifest())["imported"], 1)
        self.assertEqual(native_usage.import_manifest(self.manifest())["duplicates"], 1)
        self.assertEqual(usage.summarize(self.info["request_path"])["totals"]["input_tokens"], 100)

    def test_reused_request_id_preserves_error_attempt_and_success(self):
        self.event("turn_start")
        self.event("request_start")
        self.event("request_error", error={"message": "SECRET"})
        self.call()
        self.event("turn_end")
        report = self.inspect()
        self.assertEqual(report["turns"][0]["calls"], 2)
        self.assertEqual(report["turns"][0]["unknown_calls"], 1)
        self.assertEqual(len(set(report["turns"][0]["sample_ids"])), 2)
        native_usage.import_manifest(self.manifest())
        self.assertIsNone(usage.summarize(self.info["request_path"])["totals"]["input_tokens"])

    def test_pending_and_unclosed_turns_reject_import(self):
        self.event("turn_start")
        self.event("request_start")
        self.assertTrue(self.inspect()["turns"][0]["pending_sample_ids"])
        with self.assertRaises(ValueError):
            native_usage.import_manifest(self.manifest())
        self.event("request_end", usage=None)
        with self.assertRaises(ValueError):
            native_usage.import_manifest(self.manifest())

    def test_unselected_live_turn_does_not_block_closed_turn(self):
        self.closed()
        self.event("turn_start", "live")
        self.event("request_start", "live")
        report = native_usage.import_manifest(self.manifest())
        self.assertEqual(report["imported"], 1)
        self.assertIn("live", report["pending_native_ids"])

    def test_orphan_and_duplicate_terminal_events_are_not_summed(self):
        self.event("turn_start")
        self.event("request_end", usage=None)
        self.event("turn_end")
        self.assertTrue(self.inspect()["integrity_issues"])
        with self.assertRaises(ValueError):
            native_usage.import_manifest(self.manifest())

    def test_late_event_after_close_invalidates_turn(self):
        self.closed()
        self.call(request="late")
        with self.assertRaises(ValueError):
            native_usage.import_manifest(self.manifest())

    def test_restart_reads_persisted_start_and_stable_identity(self):
        self.event("turn_start")
        self.event("request_start")
        self.event("request_end", usage=None)
        self.event("turn_end")
        first = self.inspect()
        self.assertEqual(first, self.inspect())
        self.assertEqual(first["turns"][0]["unknown_calls"], 1)

    def test_moa_aggregation_remains_unknown(self):
        self.event("turn_start")
        self.call(moa_references=[dict(model="advisor", tokens=999)])
        self.event("turn_end")
        self.assertEqual(self.inspect()["turns"][0]["unknown_calls"], 1)

    def test_no_chat_endpoint_credentials_or_error_text_written(self):
        self.event("turn_start", user_message="PRIVATE CHAT", base_url="SECRET ENDPOINT", api_key="SECRET KEY")
        self.call(response=dict(content="PRIVATE RESPONSE"))
        self.event("turn_end")
        contents = self.db.read_bytes()
        self.assertNotIn(b"PRIVATE", contents)
        self.assertNotIn(b"SECRET", contents)
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)

    def test_multiple_profiles_and_concurrent_sessions_are_isolated(self):
        other = self.root / "profile-b"
        other.mkdir()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.recorder.record_event(self.root, "turn_start",
                          dict(session_id="s"+str(i), turn_id="t")), range(8)))
        self.recorder.record_event(other, "turn_start", dict(session_id="other", turn_id="t"))
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM orch_usage_events").fetchone()[0], 8)
        with self.assertRaises(ValueError):
            native_usage.inspect_log("hermes", str(self.db), "other")

    def test_deleted_event_is_detected(self):
        self.closed()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DELETE FROM orch_usage_events WHERE seq=2")
        with self.assertRaises(ValueError):
            self.inspect()

    def test_invalid_usage_does_not_commit_success(self):
        self.event("turn_start")
        self.event("request_start")
        with self.assertRaises(ValueError):
            self.event("request_end", usage=dict(input_tokens=True))
        with self.assertRaises(ValueError):
            native_usage.import_manifest(self.manifest())

    def test_cli_inspection_no_native_dependency(self):
        self.closed()
        result = self.run_tool("usage.py", "inspect-native", "--host", "hermes", "--log", self.db,
                               "--session-id", "session-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["turns"][0]["calls"], 1)

    def test_hook_sources_bind_exact_turns_but_keep_hidden_coverage_unknown(self):
        import hashlib
        import runs
        import usage_sources
        self.closed()
        run = runs.init_run(self.root)
        worker = self.prepare(root=None, run=run["run_path"])
        path = self.root / "bindings.json"
        proof = self.root / "proof.json"
        proof.write_text('{"synthetic_receipt":true}')
        binding = dict(subject_id="round:" + worker["round_id"], host="hermes", session_id="session-1",
            log_path=str(self.db), native_ids=["turn-1"], evidence=dict(path=str(proof),
            sha256=hashlib.sha256(proof.read_bytes()).hexdigest()))
        path.write_text(json.dumps(dict(version=1, run_path=run["run_path"], bindings=[binding])))
        report = usage_sources.audit(str(path))
        self.assertTrue(any(i["code"] == "native_scope_incomplete" for i in report["issues"]))
        self.assertFalse(any(i["code"] == "cumulative_only" for i in report["issues"]))
        self.assertIsNone(report["usage"]["totals"]["input_tokens"])

    def test_duplicate_post_and_overlapping_pre_fail_closed(self):
        self.event("turn_start")
        self.call()
        self.event("request_end", usage=None)
        self.event("request_start", request="two")
        self.event("request_start", request="two")
        self.event("turn_end")
        codes = {i["code"] for i in self.inspect()["integrity_issues"]}
        self.assertIn("orphan_terminal_event", codes)
        self.assertIn("overlapping_request_start", codes)
        with self.assertRaises(ValueError):
            native_usage.import_manifest(self.manifest())

    def test_zero_usage_placeholder_is_not_verified_zero(self):
        self.event("turn_start")
        self.event("request_start")
        self.event("request_end", usage={key: 0 for key in self.recorder.COUNTERS})
        self.event("turn_end")
        self.assertEqual(self.inspect()["turns"][0]["unknown_calls"], 1)

    def test_copied_store_reimport_keeps_same_sample_identity(self):
        self.closed()
        native_usage.import_manifest(self.manifest())
        copy = self.root / "copy.sqlite3"
        copy.write_bytes(self.db.read_bytes())
        self.db = copy
        self.assertEqual(native_usage.import_manifest(self.manifest())["duplicates"], 1)

    def test_deleted_tail_is_detected_by_persisted_sequence(self):
        self.closed()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DELETE FROM orch_usage_events WHERE seq=(SELECT MAX(seq) FROM orch_usage_events)")
        with self.assertRaises(ValueError):
            self.inspect()

    def test_failed_database_write_does_not_produce_a_completion_receipt(self):
        self.event("turn_start")
        self.event("request_start")
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("BEGIN EXCLUSIVE")
            with self.assertRaises(sqlite3.OperationalError):
                self.event("request_end", usage=None)
        with self.assertRaises(ValueError):
            native_usage.import_manifest(self.manifest())

    def test_turn_receipt_links_exact_submitted_prompt_without_storing_text(self):
        import hashlib
        prompt = 'PRIVATE REQUEST={"round_id":"explicit-test-round"}\n'
        self.event("turn_start", user_message=prompt)
        self.call()
        self.event("turn_end")
        receipt = self.inspect()["turn_receipts"]["turn-1"]
        self.assertEqual(receipt["prompt_sha256"], hashlib.sha256(prompt.encode()).hexdigest())
        self.assertNotIn(b"PRIVATE", self.db.read_bytes())
