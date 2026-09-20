"""Cumulative intervals are evidence, never round-owned additive calls."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from test_token_tools import ToolCase
import test_usage_adapters as fixtures


class HermesWindowTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.db = fixtures.UsageAdapterTests.hermes_db(self)

    def capture(self, name):
        import hermes_windows
        return hermes_windows.capture(str(self.db), "ses-1", str(self.root / (name + ".json")))

    def change(self, sql):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript(sql)

    def compare(self, *receipts):
        import hermes_windows
        manifest = self.root / "windows.json"
        manifest.write_text(json.dumps(dict(version=1, snapshots=receipts)))
        return hermes_windows.compare(str(manifest))

    def test_normal_delta_separates_session_from_model_views(self):
        first = self.capture("a")
        self.change("UPDATE sessions SET input_tokens=35, output_tokens=23;")
        last = self.capture("b")
        result = self.compare(first, last)
        self.assertEqual(result["windows"][0]["session"]["observed_delta"]["input_tokens"], 5)
        self.assertFalse(result["additive"])
        self.assertFalse(result["round_attributable"])
        self.assertIsNone(result["round_totals"]["input_tokens"])
        self.assertEqual(len(result["windows"][0]["models"]), 2)

    def test_reset_invalidates_all_row_counters(self):
        first = self.capture("a")
        self.change("UPDATE sessions SET input_tokens=1, output_tokens=100;")
        row = self.compare(first, self.capture("b"))["windows"][0]["session"]
        self.assertIn("counter_decreased", row["issues"])
        self.assertTrue(all(v is None for v in row["observed_delta"].values()))

    def test_new_and_deleted_buckets_have_no_zero_baseline(self):
        first = self.capture("a")
        self.change("DELETE FROM session_model_usage WHERE task='vision';"
                    "INSERT INTO session_model_usage VALUES('ses-1','new','title',9,2,0,0,0);")
        rows = self.compare(first, self.capture("b"))["windows"][0]["models"]
        self.assertTrue(any("missing_baseline" in r["issues"] for r in rows))
        self.assertTrue(any("missing_endpoint" in r["issues"] for r in rows))

    def test_null_stays_unknown_and_zero_does_not_prove_settling(self):
        self.change("UPDATE sessions SET cache_write_tokens=NULL;")
        result = self.compare(self.capture("a"), self.capture("b"))
        self.assertIsNone(result["windows"][0]["session"]["observed_delta"]["input_tokens"])
        self.assertIn("persistence_unverified", result["limitations"])

    def test_adjacent_intervals_and_repeated_receipt_rejected(self):
        first, second, third = self.capture("a"), self.capture("b"), self.capture("c")
        self.assertEqual(len(self.compare(first, second, third)["windows"]), 2)
        for values in ((first,), (first, second, first), (second, first)):
            with self.assertRaises(ValueError):
                self.compare(*values)

    def test_mixed_store_rejected(self):
        first = self.capture("a")
        other = self.root / "other.db"
        other.write_bytes(self.db.read_bytes())
        self.db = other
        with self.assertRaises(ValueError):
            self.compare(first, self.capture("b"))

    def test_hash_tampering_rejected(self):
        first, last = self.capture("a"), self.capture("b")
        Path(first["path"]).write_text('{}')
        with self.assertRaises(ValueError):
            self.compare(first, last)

    def test_snapshot_never_overwrites_and_database_unchanged(self):
        before = self.db.read_bytes()
        self.capture("a")
        with self.assertRaises(FileExistsError):
            self.capture("a")
        self.assertEqual(before, self.db.read_bytes())

    def test_changed_model_and_lineage_visible(self):
        first = self.capture("a")
        self.change("UPDATE sessions SET model='new';"
                    "INSERT INTO sessions VALUES('child','ses-1','m',0,0,0,0,0);")
        result = self.compare(first, self.capture("b"))
        self.assertIn("bucket_identity_changed", result["windows"][0]["session"]["issues"])
        self.assertIn("lineage_changed", result["windows"][0]["issues"])

    def test_late_writes_stay_in_later_observation_window(self):
        first, second = self.capture("a"), self.capture("b")
        self.change("UPDATE sessions SET input_tokens=input_tokens+7;")
        result = self.compare(first, second, self.capture("c"))
        self.assertEqual([r["session"]["observed_delta"]["input_tokens"] for r in result["windows"]], [0, 7])
        self.assertFalse(result["round_attributable"])

    def test_negative_or_forged_normalization_rejected_even_with_new_hash(self):
        first, last = self.capture("a"), self.capture("b")
        path = Path(last["path"])
        value = json.loads(path.read_text())
        value["snapshot"]["session_usage"]["counters"]["input_tokens"] = 999
        path.write_text(json.dumps(value))
        last["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            self.compare(first, last)

    def test_cli_capture_and_compare(self):
        out = self.root / "cli.json"
        result = self.run_tool("usage.py", "hermes-snapshot", "--log", self.db, "--session-id", "ses-1", "--output", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        first = json.loads(result.stdout)
        self.compare(first, self.capture("b"))
        result = self.run_tool("usage.py", "hermes-windows", "--manifest", self.root / "windows.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["additive"])

    def test_size_guard_uses_actual_published_encoding(self):
        first = self.capture("a")
        value = json.loads(Path(first["path"]).read_text())
        limit = len(json.dumps(value, ensure_ascii=False).encode()) + 50
        with patch("protocol.MAX_FILE_BYTES", limit):
            with self.assertRaises(ValueError):
                self.capture("oversized")
        self.assertFalse((self.root / "oversized.json").exists())

    def test_api_call_count_reset_invalidates_tokens(self):
        self.change("ALTER TABLE sessions ADD COLUMN api_call_count INTEGER;"
                    "UPDATE sessions SET api_call_count=5;")
        first = self.capture("a")
        self.change("UPDATE sessions SET api_call_count=2,input_tokens=100;")
        row = self.compare(first, self.capture("b"))["windows"][0]["session"]
        self.assertIn("counter_decreased", row["issues"])
        self.assertIsNone(row["observed_delta"]["input_tokens"])

    def test_missing_model_table_and_unknown_lineage_remain_visible(self):
        first = self.capture("a")
        self.change("DROP TABLE session_model_usage;")
        report = self.compare(first, self.capture("b"))
        self.assertIn("model_inventory_unknown", report["windows"][0]["issues"])

    def test_frozen_comparison_does_not_need_live_database(self):
        first, last = self.capture("a"), self.capture("b")
        self.db.unlink()
        self.assertFalse(self.compare(first, last)["additive"])

    def test_endpoint_secrets_are_not_emitted(self):
        self.change("ALTER TABLE sessions ADD COLUMN billing_base_url TEXT;"
                    "UPDATE sessions SET billing_base_url='https://SECRET/token';")
        first, last = self.capture("a"), self.capture("b")
        self.assertNotIn("SECRET", Path(first["path"]).read_text())
        self.assertNotIn("SECRET", json.dumps(self.compare(first, last)))
