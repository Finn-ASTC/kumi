"""Native log attribution and counter integrity, without model/API calls."""

import copy
import json
from pathlib import Path

from test_token_tools import ToolCase


class NativeUsageTests(ToolCase):
    def log(self, rows):
        path = self.root / "native.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def codex(self):
        counts = dict(input_tokens=100, cached_input_tokens=60, output_tokens=20)
        return [
            {"type": "session_meta", "payload": {"id": "session-1", "model_provider": "provider"}},
            {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "model"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": counts, "last_token_usage": counts}}},
        ]

    def omp(self):
        return [
            {"type": "session", "version": 3, "id": "session-1"},
            {"type": "message", "id": "user-1", "parentId": None, "message": {"role": "user"}},
            {"type": "message", "id": "call-1", "parentId": "user-1", "message": {
                "role": "assistant", "provider": "provider", "model": "model", "usage": {
                    "input": 30, "cacheRead": 60, "cacheWrite": 10, "output": 20, "totalTokens": 120}}},
        ]

    def manifest(self, host="codex", rows=None, native_id="turn-1", **changes):
        value = dict(version=1, host=host, log_path=str(self.log(rows or self.codex())),
                     session_id="session-1", mappings=[dict(native_id=native_id,
                     request_path=self.info["request_path"], attempt_id="initial", purpose="task")])
        value.update(changes)
        path = self.root / "manifest.json"
        path.write_text(json.dumps(value))
        return path

    def test_codex_preview_import_and_reimport(self):
        import native_usage
        import usage
        rows = self.codex()
        rows.append(copy.deepcopy(rows[-1]))
        manifest = self.manifest(rows=rows)
        preview = native_usage.import_manifest(str(manifest), dry_run=True)
        self.assertEqual(preview["selected_calls"], 1)
        self.assertFalse((Path(self.info["request_path"]).parent / "usage").exists())
        first = native_usage.import_manifest(str(manifest))
        self.assertEqual((first["imported"], first["duplicates"]), (1, 0))
        second = native_usage.import_manifest(str(manifest))
        self.assertEqual((second["imported"], second["duplicates"]), (0, 1))
        self.assertEqual(usage.summarize(self.info["request_path"])["totals"],
                         dict(input_tokens=100, cached_input_tokens=60, output_tokens=20))

    def test_codex_turn_selection_uses_baseline_from_unselected_history(self):
        import native_usage
        import usage
        rows = self.codex()
        rows.extend([
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-2"}},
            {"type": "turn_context", "payload": {"turn_id": "turn-2", "model": "next-model"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": dict(input_tokens=150, cached_input_tokens=80, output_tokens=30),
                "last_token_usage": dict(input_tokens=50, cached_input_tokens=20, output_tokens=10)}}},
        ])
        manifest = self.manifest(rows=rows, native_id="turn-2")
        result = native_usage.import_manifest(str(manifest))
        self.assertEqual((result["selected_calls"], result["unmapped_calls"]), (1, 1))
        self.assertEqual(usage.summarize(self.info["request_path"])["totals"]["input_tokens"], 50)

    def test_codex_reset_or_missing_baseline_rejected_before_any_write(self):
        import native_usage
        for total in (50, 200):
            rows = self.codex()
            bad = copy.deepcopy(rows[-1])
            bad["payload"]["info"]["total_token_usage"]["input_tokens"] = total
            rows.append(bad)
            manifest = self.manifest(rows=rows)
            with self.assertRaisesRegex(ValueError, "baseline|delta|reset"):
                native_usage.import_manifest(str(manifest))
            self.assertFalse((Path(self.info["request_path"]).parent / "usage").exists())

    def test_omp_cache_normalization_and_parent_branch_attribution(self):
        import native_usage
        import usage
        rows = self.omp()
        rows.extend([
            {"type": "message", "id": "user-2", "parentId": "call-1", "message": {"role": "user"}},
            {"type": "custom", "id": "branch", "parentId": "call-1"},
            {**copy.deepcopy(rows[-1]), "id": "call-2", "parentId": "branch"},
        ])
        manifest = self.manifest(host="omp", rows=rows, native_id="user-1")
        result = native_usage.import_manifest(str(manifest))
        self.assertEqual(result["selected_calls"], 2)
        self.assertEqual(usage.summarize(self.info["request_path"])["totals"],
                         dict(input_tokens=200, cached_input_tokens=120, output_tokens=40))

    def test_omp_missing_usage_stays_unknown(self):
        import native_usage
        import usage
        rows = self.omp()
        del rows[-1]["message"]["usage"]
        manifest = self.manifest(host="omp", rows=rows, native_id="user-1")
        native_usage.import_manifest(str(manifest))
        summary = usage.summarize(self.info["request_path"])
        self.assertEqual(summary["samples"], 1)
        self.assertTrue(all(value is None for value in summary["totals"].values()))

    def test_omp_inconsistent_counters_and_missing_parent_rejected(self):
        import native_usage
        for change in ("total", "boolean", "parent"):
            rows = self.omp()
            if change == "parent":
                rows[-1]["parentId"] = "unknown"
            else:
                rows[-1]["message"]["usage"]["totalTokens" if change == "total" else "input"] = 999 if change == "total" else True
            manifest = self.manifest(host="omp", rows=rows, native_id="user-1")
            with self.assertRaises(ValueError):
                native_usage.import_manifest(str(manifest))

    def test_session_and_mapping_mistakes_are_not_guessed(self):
        import native_usage
        for changes in ({"session_id": "wrong"}, {"version": True}, {"native_id": "unknown"},
                        {"mappings": []}, {"extra": "typo"}):
            manifest = self.manifest(**changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                native_usage.import_manifest(str(manifest))

    def test_duplicate_mapping_and_sample_conflicts_rejected(self):
        import native_usage
        manifest = self.manifest()
        value = json.loads(manifest.read_text())
        value["mappings"].append(value["mappings"][0])
        manifest.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            native_usage.import_manifest(str(manifest))
        rows = self.omp()
        rows.append({**copy.deepcopy(rows[-1]), "parentId": None})
        manifest = self.manifest(host="omp", rows=rows, native_id="user-1")
        with self.assertRaises(ValueError):
            native_usage.import_manifest(str(manifest))

    def test_multiple_native_turns_map_to_exact_protocol_rounds(self):
        import native_usage
        import usage
        from test_token_tools import protocol
        protocol.publish(self.info["result_path"], self.response())
        next_round = self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        rows = self.omp()
        rows.extend([
            {"type": "message", "id": "user-2", "parentId": "call-1", "message": {"role": "user"}},
            {**copy.deepcopy(rows[-1]), "id": "call-2", "parentId": "user-2"},
        ])
        manifest = self.manifest(host="omp", rows=rows, native_id="user-1")
        value = json.loads(manifest.read_text())
        value["mappings"].append(dict(native_id="user-2", request_path=next_round["request_path"],
                                      attempt_id="repair", purpose="retry"))
        manifest.write_text(json.dumps(value))
        native_usage.import_manifest(str(manifest))
        self.assertEqual(usage.summarize(self.info["request_path"])["samples"], 1)
        summary = usage.summarize(next_round["request_path"])
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["attempts"], dict(task=1, retry=1, report_repair=0))
        # Renaming a log does not change native session or call identities.
        renamed = self.root / "renamed.jsonl"
        (self.root / "native.jsonl").rename(renamed)
        value["log_path"] = str(renamed)
        manifest.write_text(json.dumps(value))
        self.assertEqual(native_usage.import_manifest(str(manifest))["duplicates"], 2)
        value["mappings"][1]["request_path"] = self.info["request_path"]
        manifest.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "conflicts"):
            native_usage.import_manifest(str(manifest))

    def test_empty_selected_turn_and_usage_without_identity_rejected(self):
        import native_usage
        for rows in (self.codex()[:2], [self.codex()[0], self.codex()[2]], self.codex()[1:]):
            manifest = self.manifest(rows=rows)
            with self.assertRaises(ValueError):
                native_usage.import_manifest(str(manifest))

    def test_invalid_jsonl_tail_and_duplicate_keys_rejected_without_writes(self):
        import native_usage
        for tail in ('{"type":', '{"type":"ignored","type":"message"}\n', '[]\n'):
            manifest = self.manifest()
            with (self.root / "native.jsonl").open("a") as stream:
                stream.write(tail)
            with self.assertRaises(ValueError):
                native_usage.import_manifest(str(manifest))
            self.assertFalse((Path(self.info["request_path"]).parent / "usage").exists())

    def test_inspect_and_import_cli_do_not_expose_prompt_contents(self):
        manifest = self.manifest()
        result = self.run_tool("usage.py", "inspect-native", "--host", "codex", "--log", self.root / "native.jsonl")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["session_id"], "session-1")
        self.assertEqual(report["turns"][0]["native_id"], "turn-1")
        result = self.run_tool("usage.py", "import-native", "--manifest", manifest, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["selected_calls"], 1)
