"""Run checkpoints exercise native SQLite, existing ledgers and CLI recovery without models."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from test_token_tools import ToolCase

# isort: split
import metering
import protocol
import runs
import usage
import usage_sources


class MeteringTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.run = runs.init_run(self.root)
        self.worker = self.prepare(root=None, run=self.run["run_path"])
        self.db = self.root / "native.db"
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript(
                "CREATE TABLE session(id TEXT PRIMARY KEY,parent_id TEXT);"
                "CREATE TABLE message(id TEXT PRIMARY KEY,session_id TEXT,data TEXT);"
            )
        self.receipt = self.root / "receipt.json"
        self.receipt.write_text('{"note":"PRIVATE evidence checked by caller"}')
        self.native_session("worker")
        self.native_session("controller")
        self.plan = dict(
            version=1, run_path=self.run["run_path"], bindings=[], imports=[]
        )
        self.actor(
            "round:" + self.worker["round_id"],
            "worker",
            self.worker["request_path"],
            "author",
        )
        self.actor(
            "controller:" + self.run["run_id"],
            "controller",
            self.info["request_path"],
            "controller",
        )

    def native_session(self, sid, parent=None, pending=False):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO session VALUES (?,?)", (sid, parent))
            db.execute(
                "INSERT INTO message VALUES (?,?,?)",
                (sid + "-u", sid, json.dumps(dict(role="user"))),
            )
        self.native_call(sid, sid + "-a", pending)

    def native_call(self, sid, mid, pending=False):
        data = dict(
            role="assistant",
            parentID=sid + "-u",
            providerID="p",
            modelID="m",
            time={} if pending else dict(completed=1),
            tokens=dict(
                input=30, output=12, reasoning=8, cache=dict(read=60, write=10)
            ),
        )
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(
                "INSERT INTO message VALUES (?,?,?)", (mid, sid, json.dumps(data))
            )

    def actor(self, subject, sid, request, role):
        binding = dict(
            subject_id=subject,
            host="opencode",
            session_id=sid,
            log_path=str(self.db),
            native_ids=[sid + "-u"],
            evidence=dict(
                path=str(self.receipt),
                sha256=hashlib.sha256(self.receipt.read_bytes()).hexdigest(),
            ),
        )
        if not subject.startswith("round:"):
            binding["ledger_request_paths"] = [request]
        self.plan["bindings"].append(binding)
        self.plan["imports"].append(
            dict(
                version=1,
                host="opencode",
                session_id=sid,
                log_path=str(self.db),
                mappings=[
                    dict(
                        native_id=sid + "-u",
                        request_path=request,
                        attempt_id="initial",
                        purpose="task",
                        role=role,
                        phase="implementation",
                    )
                ],
            )
        )

    def register(self):
        path = self.root / "plan-input.json"
        path.write_text(json.dumps(self.plan))
        return metering.register(str(path))["plan_path"]

    def test_initial_empty_plan_exposes_controller_and_prepared_round(self):
        self.plan.update(bindings=[], imports=[])
        report = metering.checkpoint(self.register(), "startup")
        self.assertTrue(report["collection_complete"])
        self.assertEqual(
            set(report["unbound_subjects"]),
            {"controller:" + self.run["run_id"], "round:" + self.worker["round_id"]},
        )
        self.assertIsNone(report["usage"]["totals"]["input_tokens"])
        self.assertEqual(
            report["usage"]["rounds_without_usage"][0]["round_id"],
            self.worker["round_id"],
        )

    def test_controller_child_retry_and_restart_use_existing_deduplication(self):
        self.native_session("nested", parent="worker")
        ledger = self.prepare()
        self.actor(
            "native:opencode:nested", "nested", ledger["request_path"], "verifier"
        )
        self.plan["imports"][-1]["mappings"][0].update(
            purpose="retry", attempt_id="retry-1", phase="verification"
        )
        path = self.register()
        self.assertEqual(path, self.register())
        first = metering.checkpoint(path, "baseline")
        self.assertEqual(first["imported"], 3)
        self.assertEqual(first["usage"]["totals"]["input_tokens"], 300)
        self.assertEqual(first["usage"]["non_cached_input"]["total"], 120)
        self.assertEqual(
            {g["role"] for g in first["usage"]["groups"]},
            {"author", "verifier", "controller"},
        )
        self.assertIn("retry", {g["purpose"] for g in first["usage"]["groups"]})
        result = self.run_tool(
            "usage.py", "checkpoint", "--plan", path, "--label", "after-restart"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        second = json.loads(result.stdout)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["duplicates"], 3)
        self.assertEqual(second["usage"], first["usage"])
        self.assertNotEqual(first["checkpoint_path"], second["checkpoint_path"])
        evidence = protocol.read_json(second["checkpoint_path"])
        self.assertEqual(evidence["audit"]["usage"]["samples"], 3)
        self.assertNotIn("PRIVATE", json.dumps(evidence))

    def test_register_cli_does_not_read_native_or_collect(self):
        self.db.unlink()
        path = self.root / "input.json"
        path.write_text(json.dumps(self.plan))
        result = self.run_tool("usage.py", "register-metering", "--manifest", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["collected"])
        self.assertEqual(usage.summarize(self.worker["request_path"])["samples"], 0)

    def test_dry_run_validates_but_writes_nothing_and_reports_current_gaps(self):
        path = self.register()
        before = set(self.root.rglob("*"))
        report = metering.checkpoint(path, "preview", True)
        self.assertEqual(before, set(self.root.rglob("*")))
        self.assertEqual(report["selected_calls"], 2)
        self.assertEqual(report["imported"], 0)
        self.assertIsNone(report["usage"]["totals"]["input_tokens"])
        self.assertNotIn("checkpoint_path", report)

    def test_appended_calls_increment_but_checkpoint_label_does_not_relabel(self):
        path = self.register()
        first = metering.checkpoint(path, "first")
        self.native_call("worker", "later")
        second = metering.checkpoint(path, "verification")
        self.assertEqual(second["imported"], 1)
        self.assertEqual(second["duplicates"], 2)
        self.assertEqual(second["usage"]["totals"]["input_tokens"], 300)
        self.assertEqual(
            {g["phase"] for g in second["usage"]["groups"]}, {"implementation"}
        )
        self.assertEqual(
            protocol.read_json(first["checkpoint_path"])["usage"]["totals"][
                "input_tokens"
            ],
            200,
        )

    def test_new_native_child_and_prepared_round_are_visible_without_guessing(self):
        path = self.register()
        metering.checkpoint(path, "before")
        self.native_session("new-child", parent="controller")
        prepared = self.prepare(root=None, run=self.run["run_path"])
        report = metering.checkpoint(path, "after")
        self.assertIn("native:opencode:new-child", report["unbound_subjects"])
        self.assertIn("round:" + prepared["round_id"], report["unbound_subjects"])
        self.assertEqual(report["usage"]["known_subtotals"]["input_tokens"], 200)
        self.assertIsNone(report["usage"]["totals"]["input_tokens"])
        self.assertIsNone(report["usage"]["non_cached_input"]["total"])

    def test_pending_source_does_not_prevent_other_valid_imports(self):
        self.native_call("worker", "pending", pending=True)
        report = metering.checkpoint(self.register(), "live")
        self.assertFalse(report["collection_complete"])
        self.assertEqual(report["imported"], 1)
        self.assertEqual(report["errors"][0]["stage"], "import")
        self.assertEqual(report["issue_counts"]["pending_native_calls"], 1)
        self.assertIsNone(report["usage"]["totals"]["input_tokens"])
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DELETE FROM message WHERE id='pending'")
        report = metering.checkpoint(self.register(), "settled")
        self.assertEqual(report["imported"], 1)
        self.assertEqual(report["duplicates"], 1)
        self.assertEqual(report["usage"]["totals"]["input_tokens"], 200)

    def test_missing_log_and_changed_receipt_produce_retained_failures(self):
        path = self.register()
        self.receipt.write_text("changed evidence")
        report = metering.checkpoint(path, "bad-proof")
        self.assertFalse(report["collection_complete"])
        self.assertIsNone(report["usage"])
        self.assertIn("hash mismatch", report["errors"][0]["error"])
        self.assertTrue(Path(report["checkpoint_path"]).is_file())
        for binding in self.plan["bindings"]:
            binding["evidence"]["sha256"] = hashlib.sha256(
                self.receipt.read_bytes()
            ).hexdigest()
        path = self.register()
        self.db.unlink()
        report = metering.checkpoint(path, "missing-log")
        self.assertFalse(report["collection_complete"])
        self.assertEqual(report["imported"], 0)
        self.assertIsNone(report["usage"])

    def test_bad_owner_or_duplicate_mapping_fails_before_publication(self):
        original = json.loads(json.dumps(self.plan))
        for change in ("scope", "unbound", "duplicate", "role"):
            self.plan = json.loads(json.dumps(original))
            mapping = self.plan["imports"][0]["mappings"][0]
            if change == "scope":
                mapping["request_path"] = self.info["request_path"]
            elif change == "unbound":
                mapping["native_id"] = "unknown"
            elif change == "duplicate":
                self.plan["imports"].append(self.plan["imports"][0])
            else:
                del mapping["role"]
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.register()
        self.assertFalse((Path(self.run["run_path"]).parent / "metering").exists())

    def test_conflicting_bindings_or_unreachable_descendant_never_import(self):
        self.plan["bindings"][1].update(session_id="worker", native_ids=["worker-u"])
        with self.assertRaisesRegex(ValueError, "conflicting native turn"):
            self.register()
        self.plan["bindings"][1].update(
            session_id="controller", native_ids=["controller-u"]
        )
        self.native_session("unrelated")
        ledger = self.prepare()
        self.actor(
            "native:opencode:unrelated", "unrelated", ledger["request_path"], "verifier"
        )
        report = metering.checkpoint(self.register(), "unrelated")
        self.assertFalse(report["collection_complete"])
        self.assertEqual(report["imported"], 0)
        self.assertIn("not a discovered descendant", report["errors"][0]["error"])

    def test_null_dimensions_are_visible_not_guessed(self):
        self.plan["imports"][0]["mappings"][0].update(role=None, phase=None)
        report = metering.checkpoint(self.register(), "unknown")
        self.assertIn(None, {g["role"] for g in report["usage"]["groups"]})

    def test_modified_or_unregistered_plan_cannot_collect(self):
        path = Path(self.register())
        self.plan["imports"][0]["mappings"][0]["phase"] = "changed"
        path.write_text(json.dumps(self.plan))
        with self.assertRaisesRegex(ValueError, "unchanged registered"):
            metering.checkpoint(str(path), "bad-plan")
        external = self.root / "unregistered.json"
        external.write_text(json.dumps(self.plan))
        with self.assertRaisesRegex(ValueError, "unchanged registered"):
            metering.checkpoint(str(external), "unregistered")

    def test_lost_post_import_audit_does_not_return_stale_success(self):
        path = self.register()
        audit = usage_sources.audit_data
        calls = 0

        def fail_second(value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("source disappeared")
            return audit(value)

        with patch.object(usage_sources, "audit_data", side_effect=fail_second):
            report = metering.checkpoint(path, "lost-source")
        self.assertFalse(report["collection_complete"])
        self.assertEqual(report["imported"], 2)
        self.assertIsNone(report["usage"])
        self.assertIsNone(protocol.read_json(report["checkpoint_path"])["audit"])
        self.assertEqual(metering.checkpoint(path, "recovered")["duplicates"], 2)

    def test_partial_import_failure_is_reported_and_retry_is_idempotent(self):
        self.native_call("worker", "worker-second")
        path = self.register()
        record = usage.record_usage
        calls = 0

        def fail_second(request, sample):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated ledger failure")
            return record(request, sample)

        with patch.object(usage, "record_usage", side_effect=fail_second):
            report = metering.checkpoint(path, "partial")
        self.assertFalse(report["collection_complete"])
        self.assertFalse(report["import_counts_complete"])
        self.assertEqual(report["usage"]["samples"], 2)
        recovered = metering.checkpoint(path, "recovered")
        self.assertEqual(recovered["duplicates"], 2)
        self.assertEqual(recovered["imported"], 1)
        self.assertEqual(recovered["usage"]["samples"], 3)

    def test_existing_wrong_owner_blocks_collection_instead_of_copying_sample(self):
        import native_usage

        mapping = json.loads(json.dumps(self.plan["imports"][0]))
        mapping["mappings"][0]["request_path"] = self.info["request_path"]
        native_usage.import_data(mapping)
        report = metering.checkpoint(self.register(), "wrong-owner")
        self.assertFalse(report["collection_complete"])
        self.assertEqual(report["imported"], 0)
        self.assertEqual(report["issue_counts"]["wrong_round"], 1)
        self.assertIsNone(report["usage"]["non_cached_input"]["total"])

    def test_redirected_metering_storage_is_rejected(self):
        directory = Path(self.run["run_path"]).parent / "metering"
        other = self.root / "elsewhere"
        other.mkdir()
        directory.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "redirected"):
            self.register()
        self.assertEqual(list(other.iterdir()), [])

    def test_non_cached_subtotal_requires_paired_known_counters(self):
        samples = [
            dict(input_tokens=100, cached_input_tokens=None),
            dict(input_tokens=None, cached_input_tokens=40),
            dict(input_tokens=80, cached_input_tokens=60),
        ]
        self.assertEqual(
            usage.non_cached_input(samples),
            dict(known_subtotal=20, total=None, observed_samples=1, unknown_samples=2),
        )
        self.assertIsNone(usage.non_cached_input([])["total"])

    def test_hermes_cumulative_binding_stays_unknown_and_is_not_imported(self):
        import test_usage_adapters as fixtures

        db = fixtures.UsageAdapterTests.hermes_db(self)
        self.plan["bindings"][0].update(
            host="hermes", session_id="ses-1", log_path=str(db), native_ids=None
        )
        self.plan["imports"] = self.plan["imports"][1:]
        report = metering.checkpoint(self.register(), "hermes")
        self.assertTrue(report["collection_complete"])
        self.assertEqual(report["imported"], 1)
        self.assertEqual(report["issue_counts"]["cumulative_only"], 1)
        self.assertIsNone(report["usage"]["totals"]["input_tokens"])
        self.assertEqual(report["usage"]["known_subtotals"]["input_tokens"], 100)

    def test_codex_and_omp_import_but_keep_unknown_lineage(self):
        import test_native_usage as fixtures

        original = json.loads(json.dumps(self.plan))
        for host, rows, native_id in (
            ("codex", fixtures.NativeUsageTests.codex(self), "turn-1"),
            ("omp", fixtures.NativeUsageTests.omp(self), "user-1"),
        ):
            self.plan = json.loads(json.dumps(original))
            worker = self.prepare(root=None, run=self.run["run_path"])
            log = self.root / (host + ".jsonl")
            log.write_text("".join(json.dumps(row) + "\n" for row in rows))
            self.plan["bindings"][0].update(
                subject_id="round:" + worker["round_id"],
                host=host,
                session_id="session-1",
                log_path=str(log),
                native_ids=[native_id],
            )
            manifest = self.plan["imports"][0]
            manifest.update(host=host, session_id="session-1", log_path=str(log))
            manifest["mappings"][0].update(
                native_id=native_id, request_path=worker["request_path"]
            )
            with self.subTest(host=host):
                report = metering.checkpoint(self.register(), host)
                self.assertTrue(report["collection_complete"], report["errors"])
                self.assertGreater(report["imported"], 0)
                self.assertEqual(report["issue_counts"]["native_lineage_unknown"], 1)
                self.assertIsNone(report["usage"]["totals"]["input_tokens"])
                self.assertIsNone(report["usage"]["non_cached_input"]["total"])
