"""Explicit run/native bindings expose omitted actors without guessing ownership."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil

from test_token_tools import ToolCase
import jobs
import protocol
import runs
import usage


class UsageSourceTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.run = runs.init_run(self.root)
        self.worker = self.prepare(root=None, run=self.run["run_path"])
        self.db = self.root / "native.db"
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript("CREATE TABLE session(id TEXT PRIMARY KEY,parent_id TEXT);"
                             "CREATE TABLE message(id TEXT PRIMARY KEY,session_id TEXT,data TEXT);")
        self.native_session("worker")
        self.receipt = self.root / "receipt.json"
        self.receipt.write_text('{"note":"explicit native submission receipt; PRIVATE"}')

    def native_session(self, sid, parent=None, pending=False):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO session VALUES (?,?)", (sid, parent))
            for mid, info in ((sid + "-u", dict(role="user")), (sid + "-a", dict(
                role="assistant", parentID=sid + "-u", providerID="p", modelID="m",
                time={} if pending else dict(completed=1),
                tokens=dict(input=30, output=12, reasoning=8, cache=dict(read=60, write=10))))):
                db.execute("INSERT INTO message VALUES (?,?,?)", (mid, sid, json.dumps(info)))

    def indexed(self, info=None, session_id="worker"):
        info = info or self.worker
        state = jobs.register(self.run["index_path"], info["request_path"], now=100)
        state = jobs.claim(self.run["index_path"], state["job_id"], state["revision"], "controller", 100, now=100)
        return jobs.change(self.run["index_path"], state["job_id"], state["revision"], state["lease"]["token"],
                           "update", dict(native=dict(session_id=session_id, turn_id=session_id + "-u")), now=101)

    def subject(self, info=None):
        return "round:" + (info or self.worker)["round_id"]

    def binding(self, subject=None, session_id="worker", **changes):
        value = dict(subject_id=subject or self.subject(), host="opencode", session_id=session_id,
                     log_path=str(self.db), native_ids=[session_id + "-u"], evidence=dict(
                         path=str(self.receipt), sha256=hashlib.sha256(self.receipt.read_bytes()).hexdigest()))
        value.update(changes)
        return value

    def manifest(self, bindings):
        path = self.root / "source-bindings.json"
        path.write_text(json.dumps(dict(version=1, run_path=self.run["run_path"], bindings=bindings)))
        return path

    def record(self, sid="worker", request=None):
        return usage.record_usage(request or self.worker["request_path"], dict(source="opencode:" + sid,
            sample_id=sid + "-a", measurement="delta", attempt_id="a", purpose="task", provider="p", model="m",
            input_tokens=100, cached_input_tokens=60, output_tokens=20))

    def test_discovery_includes_controller_unindexed_round_and_no_chat(self):
        import usage_sources
        self.indexed()
        other = self.prepare(root=None, run=self.run["run_path"])
        before = set(self.root.rglob("*"))
        report = usage_sources.discover(self.run["run_path"])
        subjects = {s["subject_id"]: s for s in report["subjects"]}
        self.assertTrue(report["complete"])
        self.assertIn("controller:" + self.run["run_id"], subjects)
        self.assertFalse(subjects[self.subject(other)]["indexed_job"])
        self.assertEqual(subjects[self.subject()]["native_candidates"][0]["session_id"], "worker")
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertEqual(before, set(self.root.rglob("*")))

    def test_omitted_controller_forces_unknown_totals(self):
        import usage_sources
        self.indexed()
        self.record()
        report = usage_sources.audit(str(self.manifest([self.binding()])))
        self.assertIn("controller:" + self.run["run_id"], report["unbound_subjects"])
        self.assertFalse(report["binding_coverage_complete"])
        self.assertEqual(report["usage"]["known_subtotals"]["input_tokens"], 100)
        self.assertIsNone(report["usage"]["totals"]["input_tokens"])

    def test_complete_explicit_controller_and_worker_bindings(self):
        import usage_sources
        self.indexed()
        self.record()
        self.native_session("controller")
        self.record("controller", self.info["request_path"])
        controller = self.binding("controller:" + self.run["run_id"], "controller",
                                  ledger_request_paths=[self.info["request_path"]])
        report = usage_sources.audit(str(self.manifest([self.binding(), controller])))
        self.assertTrue(report["binding_coverage_complete"])
        self.assertEqual(report["usage"]["totals"]["input_tokens"], 200)
        self.assertFalse(report["unbound_subjects"])

    def test_child_session_is_discovered_without_adoption(self):
        import usage_sources
        self.native_session("child", parent="worker")
        report = usage_sources.audit(str(self.manifest([self.binding()])))
        self.assertIn("native:opencode:child", report["unbound_subjects"])
        self.assertTrue(any(s["source"] == "unbound:native:opencode:child" for s in report["project_manifest"]["sources"]))

    def test_bound_child_discovers_grandchild_and_reuses_round_binding(self):
        import usage_sources
        self.native_session("child", parent="worker")
        self.native_session("grandchild", parent="child")
        child_job = self.prepare(root=None, run=self.run["run_path"])
        report = usage_sources.audit(str(self.manifest([self.binding(), self.binding(self.subject(child_job), "child")])))
        self.assertNotIn("native:opencode:child", report["unbound_subjects"])
        self.assertIn("native:opencode:grandchild", report["unbound_subjects"])

    def test_unrecognized_subject_or_bad_receipt_or_session_rejected(self):
        import usage_sources
        for b in (self.binding("round:invented"), self.binding(session_id="missing"),
                  self.binding(evidence=dict(path=str(self.receipt), sha256="0" * 64))):
            with self.assertRaises(ValueError):
                usage_sources.audit(str(self.manifest([b])))

    def test_wrong_round_ownership_does_not_pass_even_when_counts_match(self):
        import usage_sources
        other = self.prepare(root=None, run=self.run["run_path"])
        self.record(request=other["request_path"])
        report = usage_sources.audit(str(self.manifest([self.binding()])))
        self.assertTrue(any(i["code"] == "wrong_round" for i in report["issues"]))
        self.assertIsNone(report["usage"]["totals"]["input_tokens"])

    def test_shared_source_conflicting_turn_owners_rejected(self):
        import usage_sources
        other = self.prepare(root=None, run=self.run["run_path"])
        report = usage_sources.audit(str(self.manifest([self.binding(), self.binding(self.subject(other))])))
        self.assertTrue(any(i["code"] == "conflicting_turn_owners" for i in report["issues"]))

    def test_unselected_native_turn_and_pending_are_explicit(self):
        import usage_sources
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO message VALUES ('extra-u','worker',?)", (json.dumps(dict(role="user")),))
            db.execute("INSERT INTO message VALUES ('pending','worker',?)", (json.dumps(dict(role="assistant", parentID="extra-u", time={})),))
        report = usage_sources.audit(str(self.manifest([self.binding()])))
        self.assertTrue(any(i["code"] == "unassigned_native_turns" for i in report["issues"]))
        self.assertTrue(any(i["code"] == "pending_native_calls" for i in report["issues"]))

    def test_missing_registry_and_corrupt_job_are_not_silent(self):
        import usage_sources
        state = self.indexed()
        registry = Path(self.run["run_path"]).parent / "requests" / (self.worker["round_id"] + ".json")
        registry.unlink()
        report = usage_sources.discover(self.run["run_path"])
        self.assertFalse(report["complete"])
        self.assertTrue(report["errors"])
        with self.assertRaises(ValueError):
            usage_sources.audit(str(self.manifest([])))

    def test_historical_native_session_is_not_lost_on_session_change(self):
        import usage_sources
        state = self.indexed()
        jobs.change(self.run["index_path"], state["job_id"], state["revision"], state["lease"]["token"], "update",
                    dict(native=dict(session_id="later-session", turn_id="later-turn")), now=102)
        report = usage_sources.discover(self.run["run_path"])
        subject = next(s for s in report["subjects"] if s["subject_id"] == self.subject())
        self.assertEqual({c["session_id"] for c in subject["native_candidates"]}, {"worker", "later-session"})
        audit = usage_sources.audit(str(self.manifest([self.binding()])))
        self.assertTrue(any(i["code"] == "unbound_native_candidate" for i in audit["issues"]))

    def test_cli_discovery_and_audit_are_readonly(self):
        manifest = self.manifest([self.binding()])
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        for argv in (("discover-sources", "--run", self.run["run_path"]), ("audit-sources", "--manifest", manifest)):
            result = self.run_tool("usage.py", *argv)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("PRIVATE", result.stdout)
        after = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_native_counters_disagreeing_with_ledger_are_flagged(self):
        import usage_sources
        self.record()
        with closing(sqlite3.connect(self.db)) as db, db:
            raw = json.loads(db.execute("SELECT data FROM message WHERE id='worker-a'").fetchone()[0])
            raw["tokens"]["input"] = 31
            db.execute("UPDATE message SET data=? WHERE id='worker-a'", (json.dumps(raw),))
        report = usage_sources.audit(str(self.manifest([self.binding()])))
        self.assertTrue(any(i["code"] == "native_counter_mismatch" for i in report["issues"]))

    def test_controller_calls_cannot_borrow_another_rounds_ledger(self):
        import usage_sources
        self.native_session("controller")
        self.record("controller")
        controller = self.binding("controller:" + self.run["run_id"], "controller",
                                  ledger_request_paths=[self.info["request_path"]])
        report = usage_sources.audit(str(self.manifest([self.binding(), controller])))
        self.assertTrue(any(i["code"] == "wrong_ledger_scope" for i in report["issues"]))

    def test_native_lineage_cycle_is_not_a_complete_inventory(self):
        import usage_sources
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE session SET parent_id='worker' WHERE id='worker'")
        report = usage_sources.audit(str(self.manifest([self.binding()])))
        self.assertTrue(any(i["code"] == "native_lineage_cycle" for i in report["issues"]))

    def test_native_turn_with_no_calls_remains_unknown(self):
        import usage_sources
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DELETE FROM message WHERE id='worker-a'")
        report = usage_sources.audit(str(self.manifest([self.binding()])))
        self.assertTrue(any(i["code"] == "no_native_calls" for i in report["issues"]))

    def test_hermes_binding_is_cumulative_and_not_round_metering(self):
        import usage_sources
        import test_usage_adapters as fixtures
        hermes = fixtures.UsageAdapterTests.hermes_db(self)
        binding = self.binding(host="hermes", session_id="ses-1", log_path=str(hermes), native_ids=None)
        report = usage_sources.audit(str(self.manifest([binding])))
        self.assertTrue(any(i["code"] == "cumulative_only" for i in report["issues"]))
        self.assertIsNone(next(s for s in report["project_manifest"]["sources"] if s["source"] == "hermes:ses-1")["sample_ids"])

    def test_codex_and_omp_explicit_logs_keep_lineage_unknown(self):
        import usage_sources
        import test_native_usage as fixtures
        for host, rows, native_id in (("codex", fixtures.NativeUsageTests.codex(self), "turn-1"),
                                       ("omp", fixtures.NativeUsageTests.omp(self), "user-1")):
            path = self.root / (host + ".jsonl")
            path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
            binding = self.binding(host=host, session_id="session-1", log_path=str(path), native_ids=[native_id])
            report = usage_sources.audit(str(self.manifest([binding])))
            self.assertIn(host + ":session-1", report["native_lineage_unknown"])
            self.assertIsNone(report["usage"]["totals"]["input_tokens"])

    def test_export_does_not_prove_no_children(self):
        import usage_sources
        import test_usage_adapters as fixtures
        path, _ = fixtures.UsageAdapterTests.export(self)
        binding = self.binding(log_path=str(path), session_id="ses-1", native_ids=["u1"])
        report = usage_sources.audit(str(self.manifest([binding])))
        self.assertIn("opencode:ses-1", report["native_lineage_unknown"])

    def test_duplicate_binding_invalid_ids_and_unrelated_background_rejected(self):
        import usage_sources
        self.native_session("unrelated")
        for bindings in ([self.binding(), self.binding()], [self.binding(native_ids=[])],
                         [self.binding(native_ids=[True])], [self.binding(native_ids=["missing"])],
                         [self.binding(), self.binding("native:opencode:unrelated", "unrelated")]):
            with self.assertRaises(ValueError):
                usage_sources.audit(str(self.manifest(bindings)))

    def test_corrupt_old_job_revision_is_reported_even_when_latest_valid(self):
        import usage_sources
        state = self.indexed()
        first = Path(self.run["index_path"]) / state["job_id"] / '000000000001.json'
        value = json.loads(first.read_text())
        value["job_id"] = 'f' * 32
        first.write_text(json.dumps(value))
        report = usage_sources.discover(self.run["run_path"])
        self.assertFalse(report["complete"])

    def test_missing_entire_indexed_job_is_not_reclassified_as_unindexed(self):
        import usage_sources
        state = self.indexed()
        shutil.rmtree(Path(self.run["index_path"]) / state["job_id"])
        report = usage_sources.discover(self.run["run_path"])
        self.assertFalse(report["complete"])
        self.assertTrue(report["errors"])

    def test_corrupt_historical_version_is_reported(self):
        import usage_sources
        state = self.indexed()
        path = Path(self.run["index_path"]) / state["job_id"] / '000000000001.json'
        value = json.loads(path.read_text())
        value["version"] = 2
        path.write_text(json.dumps(value))
        self.assertFalse(usage_sources.discover(self.run["run_path"])["complete"])

    def test_empty_run_still_requires_controller(self):
        import usage_sources
        empty = runs.init_run(self.root)
        path = self.root / 'empty.json'
        path.write_text(json.dumps(dict(version=1, run_path=empty['run_path'], bindings=[])))
        report = usage_sources.audit(str(path))
        self.assertEqual(report['unbound_subjects'], ['controller:' + empty['run_id']])
        self.assertIsNone(report['usage']['totals']['input_tokens'])

    def test_child_binding_cannot_substitute_a_conflicting_database(self):
        import usage_sources
        self.native_session("child", parent="worker")
        other = self.root / "other.db"
        shutil.copyfile(self.db, other)
        with closing(sqlite3.connect(other)) as db, db:
            db.execute("UPDATE session SET parent_id='unrelated' WHERE id='child'")
        report = usage_sources.audit(str(self.manifest([
            self.binding(), self.binding("native:opencode:child", "child", log_path=str(other))])))
        self.assertTrue(any(i["code"] == "native_parent_mismatch" for i in report["issues"]))

    def test_followup_discovery_and_shared_session_calls_stay_in_their_round(self):
        import usage_sources
        request = protocol.load_request(self.worker["request_path"])
        protocol.publish(request["result_path"], self.response(**{k: request[k] for k in protocol.IDENTITY_FIELDS}))
        later = self.prepare(root=None, cwd=None, parent_depth=None, previous=self.worker["request_path"])
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO message VALUES ('later-u','worker',?)", (json.dumps(dict(role="user")),))
            raw = json.loads(db.execute("SELECT data FROM message WHERE id='worker-a'").fetchone()[0])
            raw["parentID"] = "later-u"
            db.execute("INSERT INTO message VALUES ('later-a','worker',?)", (json.dumps(raw),))
        self.record()
        sample = {k: v for k, v in usage.stored_samples(usage.request_chain(self.worker["request_path"]))[0][1].items()
                  if k not in protocol.IDENTITY_FIELDS}
        sample["sample_id"] = "later-a"
        usage.record_usage(later["request_path"], sample)
        report = usage_sources.audit(str(self.manifest([
            self.binding(), self.binding(self.subject(later), native_ids=["later-u"])])))
        self.assertEqual(len(report["subjects"]), 3)
        self.assertFalse(report["issues"])
        self.assertEqual(report["usage"]["samples"], 2)
        self.assertEqual(report["usage"]["known_subtotals"]["input_tokens"], 200)

    def test_unknown_native_counters_cannot_validate_known_ledger_counters(self):
        import usage_sources
        self.record()
        with closing(sqlite3.connect(self.db)) as db, db:
            raw = json.loads(db.execute("SELECT data FROM message WHERE id='worker-a'").fetchone()[0])
            raw.pop("tokens")
            db.execute("UPDATE message SET data=? WHERE id='worker-a'", (json.dumps(raw),))
        report = usage_sources.audit(str(self.manifest([self.binding()])))
        self.assertTrue(any(i["code"] == "native_counter_mismatch" for i in report["issues"]))
        self.assertIsNone(report["usage"]["totals"]["input_tokens"])

    def test_multi_session_cycle_is_reported_without_looping(self):
        import usage_sources
        self.native_session("child", parent="worker")
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE session SET parent_id='child' WHERE id='worker'")
        report = usage_sources.audit(str(self.manifest([
            self.binding(), self.binding("native:opencode:child", "child")])))
        self.assertTrue(any(i["code"] == "native_lineage_cycle" for i in report["issues"]))

    def test_corrected_effective_counts_are_compared_instead_of_original(self):
        import usage_sources
        import usage_corrections
        receipt = self.record()
        original = Path(receipt["path"]).read_bytes()
        sample = json.loads(original)
        sample = {k: v for k, v in sample.items() if k not in protocol.IDENTITY_FIELDS}
        sample["input_tokens"] = 101
        with closing(sqlite3.connect(self.db)) as db, db:
            raw = json.loads(db.execute("SELECT data FROM message WHERE id='worker-a'").fetchone()[0])
            raw["tokens"]["input"] = 31
            db.execute("UPDATE message SET data=? WHERE id='worker-a'", (json.dumps(raw),))
        spec = self.root / "correction.json"
        revision = usage_corrections.history(self.worker["request_path"], "opencode:worker", "worker-a")["revision"]
        spec.write_text(json.dumps(dict(version=1, request_path=self.worker["request_path"],
            correction_id="fix-count", reason="Native evidence changed", evidence=[self.binding()["evidence"]],
            updates=[dict(request_path=self.worker["request_path"], sample=sample, expected_revision=revision)])))
        usage_corrections.correct_manifest(str(spec))
        report = usage_sources.audit(str(self.manifest([self.binding()])))
        self.assertFalse(report["issues"])
        self.assertEqual(report["usage"]["known_subtotals"]["input_tokens"], 101)
        self.assertEqual(Path(receipt["path"]).read_bytes(), original)
