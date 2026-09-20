"""Auxiliary receipts stay separate from round ledgers and cumulative snapshots."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import sqlite3
import sys
import uuid

from test_token_tools import ToolCase, SCRIPTS


class HermesAuxTests(ToolCase):
    def setUp(self):
        super().setUp()
        plugin = SCRIPTS.parents[1] / "agent-hermes/plugins/orch-usage"
        spec = importlib.util.spec_from_file_location("aux_test_plugin", plugin / "__init__.py",
                                                   submodule_search_locations=[str(plugin)])
        self.plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.plugin
        spec.loader.exec_module(self.plugin)
        self.addCleanup(sys.modules.pop, spec.name, None)
        self.db = self.root / "usage-hooks/auxiliary.sqlite3"

    def event(self, **extra):
        data = dict(observation_id=uuid.uuid4().hex, session_id="s", turn_id=None,
                    task="approval", model="m", provider_hint=None,
                    usage=dict(input_tokens=40, output_tokens=20, cache_read_tokens=60,
                               cache_write_tokens=0, reasoning_tokens=8, prompt_tokens=100, total_tokens=120))
        data.update(extra)
        return data

    def write(self, data):
        return self.plugin.auxiliary.record_aux_event(self.root, data)

    def inspect(self, path=None):
        result = self.run_tool("usage.py", "hermes-aux", "--log", path or self.db, "--session-id", "s")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_concurrent_duplicate_delivery_and_backup_keep_one_receipt(self):
        data = self.event()
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(self.write, [data] * 8))
        self.assertEqual(len(set(receipts)), 1)
        report = self.inspect()
        self.assertEqual(report["observed_responses"], 1)
        self.assertEqual(report["observed_totals"]["input_tokens"], 100)
        self.assertEqual(report["observed_totals"]["output_tokens"], 20)
        self.assertEqual(report["unassigned_responses"], 1)
        self.assertFalse(report["coverage_complete"])
        self.assertIsNone(report["totals"])
        self.assertIsNone(report["receipts"][0]["native_turn_id"])
        backup = self.root / "backup.db"
        with closing(sqlite3.connect(self.db)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        self.assertEqual(report, self.inspect(backup))

    def test_conflicting_identity_is_rejected_without_overwriting(self):
        data = self.event()
        self.write(data)
        with self.assertRaises(ValueError):
            self.write(dict(data, task="title_generation"))
        self.assertEqual(self.inspect()["observed_responses"], 1)

    def test_missing_zero_partial_usage_stays_unknown(self):
        for value in (None, {}, {"input_tokens": 0, "output_tokens": 0}, {"input_tokens": 1}):
            self.write(self.event(usage=value))
        report = self.inspect()
        self.assertEqual(report["unknown_responses"], 4)
        self.assertIsNone(report["observed_totals"]["input_tokens"])

    def test_receipts_are_allowlisted_and_not_importable_as_turns(self):
        self.write(self.event(response={"text": "PRIVATE"}, base_url="SECRET", error="SECRET"))
        report = self.inspect()
        self.assertNotIn(b"PRIVATE", self.db.read_bytes())
        self.assertNotIn(b"SECRET", self.db.read_bytes())
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        import native_usage
        with self.assertRaises(ValueError):
            native_usage.inspect_log("hermes", str(self.db), "s")
        self.assertEqual(report["receipts"][0]["task"], "approval")

    def test_invalid_or_overlapping_receipts_never_commit(self):
        for change in (dict(usage={"input_tokens": True}), dict(turn_id="invented-turn"),
                       dict(task="moa_reference"), dict(task="moa_aggregator"), dict(observation_id="")):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.write(self.event(**change))
        self.assertFalse(self.db.exists())

    def test_deleted_tail_fails_closed(self):
        self.write(self.event())
        self.write(self.event())
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DELETE FROM orch_aux_events WHERE seq=2")
        result = self.run_tool("usage.py", "hermes-aux", "--log", self.db, "--session-id", "s")
        self.assertNotEqual(result.returncode, 0)

    def test_inconsistent_usage_and_schema_fail_closed(self):
        data = self.event()
        data["usage"]["prompt_tokens"] = 999
        self.write(data)
        import hermes_aux
        with self.assertRaises(ValueError):
            hermes_aux.inspect(str(self.db), "s")
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE orch_aux_meta SET version=2")
        with self.assertRaises(ValueError):
            hermes_aux.inspect(str(self.db), "s")
        with self.assertRaises(ValueError):
            self.write(self.event())

    def test_existing_main_loop_store_remains_independent(self):
        home = self.root
        self.plugin.record_event(home, "turn_start", dict(session_id="s", turn_id="t"))
        main = home / "usage-hooks/events.sqlite3"
        original = main.read_bytes()
        self.write(self.event())
        self.assertEqual(main.read_bytes(), original)
        self.assertEqual(self.inspect()["observed_responses"], 1)

    def test_other_session_and_profile_do_not_leak(self):
        self.write(self.event())
        self.write(self.event(session_id="other"))
        (self.root / "b").mkdir()
        self.plugin.auxiliary.record_aux_event(self.root / "b", self.event())
        self.assertEqual(self.inspect()["observed_responses"], 1)

    def test_symlink_store_rejected(self):
        target = self.root / "target"
        target.write_text("keep")
        self.db.parent.mkdir()
        self.db.symlink_to(target)
        with self.assertRaises((OSError, ValueError)):
            self.write(self.event())
        self.assertEqual(target.read_text(), "keep")
