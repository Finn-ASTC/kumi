"""Sanitized SQLite/export fixtures for attribution, coverage and native semantics."""

import copy
from contextlib import closing
import json
import sqlite3

from test_token_tools import ToolCase


class UsageAdapterTests(ToolCase):
    def export(self):
        value = {"info": {"id": "ses-1", "parentID": "parent"}, "messages": [
            {"info": {"id": "u1", "sessionID": "ses-1", "role": "user"},
             "parts": [{"text": "PRIVATE PROMPT"}]},
            {"info": {"id": "a1", "sessionID": "ses-1", "role": "assistant",
                      "parentID": "u1", "modelID": "m", "providerID": "p",
                      "time": {"completed": 2}, "tokens": {
                          "input": 30, "output": 12, "reasoning": 8,
                          "cache": {"read": 60, "write": 10}}},
             "parts": [{"type": "step-finish", "tokens": {"input": 9999}}]}]}
        path = self.root / "export.json"
        path.write_text(json.dumps(value))
        return path, value

    def opencode_db(self, value):
        path = self.root / "opencode.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("CREATE TABLE session(id TEXT, parent_id TEXT); "
                             "CREATE TABLE message(id TEXT, session_id TEXT, data TEXT);")
            db.executemany("INSERT INTO session VALUES (?,?)", [("ses-1", "parent"), ("child", "ses-1")])
            for item in value["messages"]:
                info = copy.deepcopy(item["info"])
                db.execute("INSERT INTO message VALUES (?,?,?)",
                           (info.pop("id"), info.pop("sessionID"), json.dumps(info)))
            db.execute("INSERT INTO message VALUES ('child-a','child',?)", ('{"role":"assistant"}',))
        return path

    def manifest(self, path, host="opencode", **mapping_fields):
        value = dict(version=1, host=host, log_path=str(path), session_id="ses-1",
                     mappings=[dict(native_id="u1", request_path=self.info["request_path"],
                                    attempt_id="try-1", purpose="retry", **mapping_fields)])
        manifest = self.root / "import.json"
        manifest.write_text(json.dumps(value))
        return manifest

    def sample(self, **changes):
        return dict(sample_id="a1", source="opencode:ses-1", measurement="delta",
                    attempt_id="try-1", purpose="retry", provider="p", model="m",
                    input_tokens=100, cached_input_tokens=60, output_tokens=20, **changes)

    def test_opencode_export_db_parity_and_native_reasoning(self):
        import native_usage
        import usage
        path, value = self.export()
        db = self.opencode_db(value)
        a = native_usage.read_log("opencode", str(path), "ses-1")
        b = native_usage.read_log("opencode", str(db), "ses-1")
        self.assertEqual(a["calls"], b["calls"])
        report = native_usage.inspect_log("opencode", str(db), "ses-1")
        self.assertEqual(report["child_session_ids"], ["child"])
        self.assertEqual(report["parent_session_id"], "parent")
        self.assertEqual(report["turns"][0]["sample_ids"], ["a1"])
        self.assertNotIn("PRIVATE PROMPT", json.dumps(report))
        result = native_usage.import_manifest(str(self.manifest(path)))
        self.assertEqual(result["imported"], 1)
        result = native_usage.import_manifest(str(self.manifest(db)))
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(usage.summarize(self.info["request_path"])["totals"],
                         dict(input_tokens=100, cached_input_tokens=60, output_tokens=20))

    def test_opencode_bad_identity_parent_counters_and_partial_rejected(self):
        import native_usage
        for mutate in (
            lambda i: i.update(sessionID="other"),
            lambda i: i.update(parentID="missing"),
            lambda i: i.update(time={}),
            lambda i: i["tokens"].update(input=True),
            lambda i: i["tokens"]["cache"].update(read=-1),
        ):
            path, value = self.export()
            mutate(value["messages"][1]["info"])
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                native_usage.import_manifest(str(self.manifest(path)))
            from pathlib import Path
            self.assertFalse((Path(self.info["request_path"]).parent / "usage").exists())
        with self.assertRaises(ValueError):
            native_usage.read_log("opencode", str(path), "wrong-session")

    def test_opencode_missing_counters_are_unknown_and_unordered_parent_resolves(self):
        import native_usage
        path, value = self.export()
        value["messages"][1]["info"].pop("tokens")
        value["messages"].reverse()
        path.write_text(json.dumps(value))
        report = native_usage.inspect_log("opencode", str(path), "ses-1")
        self.assertEqual(report["turns"][0]["unknown_calls"], 1)

    def hermes_db(self, legacy=False):
        path = self.root / "hermes.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("CREATE TABLE sessions(id TEXT, parent_session_id TEXT, "
                             "model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
                             "cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER);"
                             "INSERT INTO sessions VALUES ('ses-1',NULL,'m',30,20,60,10,8);")
            if not legacy:
                db.executescript("CREATE TABLE session_model_usage(session_id TEXT, model TEXT, "
                                 "task TEXT, input_tokens INTEGER, output_tokens INTEGER, "
                                 "cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER);"
                                 "INSERT INTO session_model_usage VALUES ('ses-1','m','',30,20,60,10,8);"
                                 "INSERT INTO session_model_usage VALUES ('ses-1','vision','vision',3,2,0,0,0);")
        return path

    def test_hermes_snapshot_keeps_nonadditive_views_and_refuses_round_import(self):
        import native_usage
        path = self.hermes_db()
        report = native_usage.inspect_log("hermes", str(path), "ses-1")
        self.assertEqual(report["measurement"], "cumulative_snapshot")
        self.assertFalse(report["round_attributable"])
        self.assertEqual(report["session_usage"]["counters"]["input_tokens"], 100)
        self.assertEqual(report["session_usage"]["counters"]["output_tokens"], 20)
        self.assertEqual(len(report["model_usage"]), 2)
        self.assertNotIn("totals", report)
        with self.assertRaisesRegex(ValueError, "cumulative|snapshot"):
            native_usage.import_manifest(str(self.manifest(path, host="hermes")))

    def test_hermes_legacy_unknown_columns_and_cli_readonly(self):
        import native_usage
        path = self.hermes_db(legacy=True)
        before = path.read_bytes()
        result = self.run_tool("usage.py", "inspect-native", "--host", "hermes", "--log", path,
                               "--session-id", "ses-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertIsNone(report["model_usage"])
        self.assertIsNone(report["session_usage"]["api_call_count"])
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(ValueError):
            native_usage.inspect_log("hermes", str(path), "unknown")

    def test_role_phase_are_independent_and_legacy_is_unknown(self):
        import usage
        usage.record_usage(self.info["request_path"], self.sample(role="verifier", phase="rework"))
        report = usage.summarize(self.info["request_path"])
        group = report["groups"][0]
        self.assertEqual((group["role"], group["phase"], group["purpose"]), ("verifier", "rework", "retry"))
        sample = self.sample()
        sample.update(sample_id="a2", attempt_id="try-2")
        usage.record_usage(self.info["request_path"], sample)
        self.assertTrue(any(g["role"] is None and g["phase"] is None for g in
                            usage.summarize(self.info["request_path"])["groups"]))
        for bad in ({"role": 3}, {"phase": ""}, {"rol": "author"}):
            with self.assertRaises(ValueError):
                usage.validate_sample(self.sample(**bad))

    def test_import_mapping_dimensions_validate_before_writes(self):
        import native_usage
        import usage
        path, _ = self.export()
        manifest = self.manifest(path, role="author", phase="implementation")
        native_usage.import_manifest(str(manifest))
        self.assertEqual(usage.summarize(self.info["request_path"])["groups"][0]["role"], "author")
        value = json.loads(manifest.read_text())
        value["mappings"][0]["role"] = False
        manifest.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            native_usage.import_manifest(str(manifest), dry_run=True)
        changed = self.sample(role="verifier", phase="implementation")
        changed["sample_id"] = "a2"
        with self.assertRaisesRegex(ValueError, "attribution"):
            usage.record_usage(self.info["request_path"], changed)

    def project(self, requests, sources):
        path = self.root / "project-usage.json"
        path.write_text(json.dumps(dict(version=1, request_paths=requests, sources=sources)))
        return path

    def test_project_deduplicates_and_exposes_uncovered_sources(self):
        import usage
        second = self.prepare()
        for request in (self.info, second):
            usage.record_usage(request["request_path"], self.sample(role="author", phase="implementation"))
        path = self.project([self.info["request_path"], second["request_path"]], [
            dict(source="opencode:ses-1", sample_ids=["a1", "a2"]),
            dict(source="hermes:ses-2", sample_ids=None),
            dict(source="controller:unbound", sample_ids=None)])
        result = usage.summarize_project(str(path))
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["known_subtotals"]["input_tokens"], 100)
        self.assertFalse(result["source_coverage_complete"])
        self.assertIsNone(result["totals"]["input_tokens"])
        self.assertEqual(result["sources"][0]["missing_sample_ids"], ["a2"])
        self.assertEqual(result["groups"][0]["samples"], 1)

    def test_project_rejects_conflicting_native_attribution(self):
        import usage
        second = self.prepare()
        usage.record_usage(self.info["request_path"], self.sample(role="author"))
        usage.record_usage(second["request_path"], self.sample(role="verifier"))
        path = self.project([self.info["request_path"], second["request_path"]], [])
        with self.assertRaisesRegex(ValueError, "conflict"):
            usage.summarize_project(str(path))

    def test_live_wal_is_read_and_missing_database_is_not_created(self):
        import native_usage
        path, value = self.export()
        dbpath = self.opencode_db(value)
        with closing(sqlite3.connect(dbpath)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            item = copy.deepcopy(value["messages"][1]["info"])
            item["id"] = "a2"
            db.execute("INSERT INTO message VALUES (?,?,?)", ("a2", "ses-1", json.dumps(item)))
            db.commit()
            self.assertEqual(native_usage.inspect_log("opencode", str(dbpath), "ses-1")["turns"][0]["calls"], 2)
        missing = self.root / "missing.db"
        result = self.run_tool("usage.py", "inspect-native", "--host", "hermes", "--log", missing,
                               "--session-id", "ses-1")
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(missing.exists())

    def test_opencode_duplicate_json_and_message_ids_rejected(self):
        import native_usage
        path, value = self.export()
        value["messages"].append(copy.deepcopy(value["messages"][1]))
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            native_usage.inspect_log("opencode", str(path))
        path.write_text('{"info": {}, "info": {}}')
        with self.assertRaises(ValueError):
            native_usage.inspect_log("opencode", str(path))

    def test_opencode_zero_placeholder_is_unknown_even_without_error(self):
        import native_usage
        path, value = self.export()
        item = value["messages"][1]["info"]
        item["error"] = {"name": "Aborted"}
        item["tokens"] = dict(input=0, output=0, reasoning=0, cache=dict(read=0, write=0))
        path.write_text(json.dumps(value))
        report = native_usage.inspect_log("opencode", str(path))
        self.assertEqual(report["turns"][0]["unknown_calls"], 1)
        item.pop("error")
        path.write_text(json.dumps(value))
        report = native_usage.inspect_log("opencode", str(path))
        self.assertEqual(report["turns"][0]["unknown_calls"], 1)

    def test_hermes_missing_bucket_and_invalid_counter_are_not_zero(self):
        import native_usage
        path = self.hermes_db()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE sessions SET cache_write_tokens=NULL")
        result = native_usage.inspect_log("hermes", str(path), "ses-1")
        self.assertIsNone(result["session_usage"]["counters"]["input_tokens"])
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE session_model_usage SET input_tokens=-1 WHERE task='vision'")
        with self.assertRaises(ValueError):
            native_usage.inspect_log("hermes", str(path), "ses-1")

    def test_hermes_endpoint_secrets_not_emitted_or_merged(self):
        import native_usage
        path = self.hermes_db()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("ALTER TABLE session_model_usage ADD COLUMN billing_base_url TEXT")
            db.execute("UPDATE session_model_usage SET billing_base_url='https://SECRET/token'")
            db.execute("INSERT INTO session_model_usage SELECT session_id,model,task,input_tokens,output_tokens,"
                       "cache_read_tokens,cache_write_tokens,reasoning_tokens,'https://SECOND/token' "
                       "FROM session_model_usage WHERE task='vision'")
        result = native_usage.inspect_log("hermes", str(path), "ses-1")
        self.assertEqual(len({r["bucket_id"] for r in result["model_usage"]}), 3)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("SECOND", json.dumps(result))

    def test_project_overlapping_chains_are_not_extra_calls(self):
        import usage
        from test_token_tools import protocol
        usage.record_usage(self.info["request_path"], self.sample())
        protocol.publish(self.info["result_path"], self.response())
        second = self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        sample = self.sample()
        sample["sample_id"] = "a2"
        usage.record_usage(second["request_path"], sample)
        path = self.project([self.info["request_path"], second["request_path"], second["request_path"]],
                            [dict(source="opencode:ses-1", sample_ids=["a1", "a2"])])
        result = self.run_tool("usage.py", "project-summary", "--manifest", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["jobs"], report["rounds"], report["samples"], report["duplicates"]), (1, 2, 2, 0))
        self.assertEqual(report["totals"]["input_tokens"], 200)
        self.assertTrue(report["source_coverage_complete"])

    def test_project_undeclared_empty_and_unexpected_sources(self):
        import usage
        usage.record_usage(self.info["request_path"], self.sample())
        for sources in ([], [dict(source="opencode:ses-1", sample_ids=[])]):
            path = self.project([self.info["request_path"]], sources)
            report = usage.summarize_project(str(path))
            self.assertFalse(report["source_coverage_complete"])
            self.assertIsNone(report["totals"]["input_tokens"])
        empty = self.prepare()
        path = self.project([empty["request_path"]], [dict(source="unmetered", sample_ids=None)])
        report = usage.summarize_project(str(path))
        self.assertEqual(len(report["rounds_without_usage"]), 1)
        self.assertIsNone(report["totals"]["input_tokens"])

    def test_project_conflicting_counters_and_invalid_inventory_rejected(self):
        import usage
        usage.record_usage(self.info["request_path"], self.sample())
        second = self.prepare()
        sample = self.sample()
        sample["output_tokens"] = 21
        usage.record_usage(second["request_path"], sample)
        path = self.project([self.info["request_path"], second["request_path"]], [])
        with self.assertRaisesRegex(ValueError, "conflict"):
            usage.summarize_project(str(path))
        for sources in ([dict(source="s", sample_ids=["a", "a"])],
                        [dict(source="s", sample_ids=[True])],
                        [dict(source="s", sample_ids=None, extra=1)]):
            path = self.project([self.info["request_path"]], sources)
            with self.assertRaises(ValueError):
                usage.summarize_project(str(path))

    def test_two_mappings_with_different_attempt_roles_fail_before_import(self):
        import native_usage
        from pathlib import Path
        path, value = self.export()
        value["messages"].extend(copy.deepcopy(value["messages"]))
        value["messages"][2]["info"]["id"] = "u2"
        value["messages"][3]["info"].update(id="a2", parentID="u2")
        path.write_text(json.dumps(value))
        manifest = self.manifest(path, role="author")
        spec = json.loads(manifest.read_text())
        spec["mappings"].append({**spec["mappings"][0], "native_id": "u2", "role": "verifier"})
        manifest.write_text(json.dumps(spec))
        with self.assertRaisesRegex(ValueError, "attribution"):
            native_usage.import_manifest(str(manifest))
        self.assertFalse((Path(self.info["request_path"]).parent / "usage").exists())
