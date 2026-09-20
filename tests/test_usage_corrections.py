"""Append-only corrections must preserve audit history and replace whole attempts safely."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from test_token_tools import ToolCase, protocol


class UsageCorrectionTests(ToolCase):
    def sample(self, **changes):
        value = dict(sample_id="call-1", source="native:session", measurement="delta", attempt_id="a",
                     purpose="task", role="author", phase="implementation", provider="p", model="m",
                     input_tokens=100, cached_input_tokens=60, output_tokens=20)
        value.update(changes)
        return value

    def record(self, sample=None, request=None):
        import usage
        return usage.record_usage(request or self.info["request_path"], sample or self.sample())

    def history(self, sample_id="call-1", request=None):
        import usage_corrections
        return usage_corrections.history(request or self.info["request_path"], "native:session", sample_id)

    def manifest(self, samples, correction_id="fix-1", targets=None):
        evidence = self.root / "native-evidence.json"
        if not evidence.exists():
            evidence.write_text('{"evidence":"retained native observation"}')
        targets = targets or [self.info["request_path"]] * len(samples)
        value = dict(version=1, request_path=self.info["request_path"], correction_id=correction_id, reason="Native evidence reconciled",
                     evidence=[dict(path=str(evidence), sha256=hashlib.sha256(evidence.read_bytes()).hexdigest())],
                     updates=[dict(request_path=target, expected_revision=self.history(s["sample_id"])["revision"],
                                   sample=s) for s, target in zip(samples, targets)])
        path = self.root / (correction_id + ".json")
        path.write_text(json.dumps(value))
        return path

    def test_unknown_to_known_preserves_original_and_reimport_uses_effective_record(self):
        import usage
        import usage_corrections
        old = self.sample(input_tokens=None, cached_input_tokens=None, output_tokens=None)
        original = Path(self.record(old)["path"])
        before = original.read_bytes()
        spec = self.manifest([self.sample()])
        usage_corrections.correct_manifest(str(spec))
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(usage.summarize(self.info["request_path"])["totals"]["input_tokens"], 100)
        self.assertTrue(self.record()["duplicate"])
        with self.assertRaises(ValueError):
            self.record(old)
        history = self.history()
        self.assertEqual(history["original"]["input_tokens"], None)
        self.assertEqual(history["effective"]["input_tokens"], 100)
        self.assertEqual(history["corrections"][0]["reason"], "Native evidence reconciled")

    def test_dry_run_and_idempotent_replay(self):
        import usage_corrections
        self.record()
        spec = self.manifest([self.sample(output_tokens=25)])
        before = set(self.root.rglob("*"))
        report = usage_corrections.correct_manifest(str(spec), dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(before, set(self.root.rglob("*")))
        first = usage_corrections.correct_manifest(str(spec))
        second = usage_corrections.correct_manifest(str(spec))
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.history()["corrections"]), 1)

    def test_whole_attempt_attribution_changes_atomically(self):
        import usage
        import usage_corrections
        self.record()
        self.record(self.sample(sample_id="call-2"))
        one = self.sample(role="verifier", phase="verification", purpose="retry")
        spec = self.manifest([one])
        with self.assertRaisesRegex(ValueError, "attempt"):
            usage_corrections.correct_manifest(str(spec))
        self.assertEqual(len(self.history()["corrections"]), 0)
        spec = self.manifest([one, {**one, "sample_id": "call-2"}])
        usage_corrections.correct_manifest(str(spec))
        report = usage.summarize(self.info["request_path"])
        self.assertEqual(report["attempts"]["retry"], 1)
        self.assertEqual(report["groups"][0]["role"], "verifier")
        self.assertEqual(report["samples"], 2)

    def test_stale_revision_and_reused_id_are_rejected(self):
        import usage_corrections
        self.record()
        first = self.manifest([self.sample(output_tokens=21)], "first")
        stale = self.manifest([self.sample(output_tokens=22)], "stale")
        usage_corrections.correct_manifest(str(first))
        with self.assertRaisesRegex(ValueError, "revision"):
            usage_corrections.correct_manifest(str(stale))
        value = json.loads(first.read_text())
        value["reason"] = "Different correction"
        first.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "correction_id"):
            usage_corrections.correct_manifest(str(first))

    def test_concurrent_same_revision_only_one_commit(self):
        import usage_corrections
        self.record()
        specs = [self.manifest([self.sample(output_tokens=21 + n)], f"fix-{n}") for n in range(2)]
        def correct(path):
            try:
                usage_corrections.correct_manifest(str(path))
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(correct, specs))
        self.assertEqual(sum(result), 1)
        self.assertEqual(len(self.history()["corrections"]), 1)

    def test_failed_publication_cannot_apply_half_an_attempt(self):
        import usage_corrections
        self.record()
        self.record(self.sample(sample_id="call-2"))
        spec = self.manifest([self.sample(role="verifier"), self.sample(sample_id="call-2", role="verifier")])
        with patch.object(protocol, "publish", side_effect=OSError("injected")):
            # The module imports the same protocol module as usage, not test support's private instance.
            import usage
            with patch.object(usage.protocol, "publish", side_effect=OSError("injected")), self.assertRaises(OSError):
                usage_corrections.correct_manifest(str(spec))
        self.assertEqual(self.history()["effective"]["role"], "author")
        usage_corrections.correct_manifest(str(spec))
        self.assertEqual(self.history()["effective"]["role"], "verifier")

    def test_bad_evidence_or_invalid_second_update_never_partially_commits(self):
        import usage_corrections
        self.record()
        self.record(self.sample(sample_id="call-2"))
        for change in ("hash", "sample", "duplicate"):
            spec = self.manifest([self.sample(output_tokens=22), self.sample(sample_id="call-2", output_tokens=22)])
            value = json.loads(spec.read_text())
            if change == "hash":
                value["evidence"][0]["sha256"] = "0" * 64
            elif change == "sample":
                value["updates"][1]["sample"]["output_tokens"] = True
            else:
                value["updates"].append(value["updates"][0])
            spec.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                usage_corrections.correct_manifest(str(spec))
            self.assertEqual(self.history()["effective"]["output_tokens"], 20)

    def test_round_reassignment_within_job_updates_both_summaries(self):
        import usage
        import usage_corrections
        self.record()
        protocol.publish(self.info["result_path"], self.response())
        next_round = self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        spec = self.manifest([self.sample()], targets=[next_round["request_path"]])
        usage_corrections.correct_manifest(str(spec))
        self.assertEqual(usage.summarize(self.info["request_path"])["samples"], 0)
        report = usage.summarize(next_round["request_path"])
        self.assertEqual(report["samples"], 1)
        self.assertEqual(report["rounds_without_usage"], [self.info["round_id"]])
        self.assertEqual(self.history()["effective"]["round_id"], next_round["round_id"])

    def test_cross_job_reassignment_and_unknown_sample_are_rejected(self):
        import usage_corrections
        self.record()
        other = self.prepare()
        spec = self.manifest([self.sample()], targets=[other["request_path"]])
        with self.assertRaises(ValueError):
            usage_corrections.correct_manifest(str(spec))
        spec = self.manifest([self.sample()])
        value = json.loads(spec.read_text())
        value["updates"][0]["sample"]["sample_id"] = "invented"
        spec.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            usage_corrections.correct_manifest(str(spec))

    def test_project_summary_uses_effective_samples_once(self):
        import usage
        import usage_corrections
        self.record()
        spec = self.manifest([self.sample(input_tokens=120)])
        usage_corrections.correct_manifest(str(spec))
        manifest = self.root / "project.json"
        manifest.write_text(json.dumps(dict(version=1, request_paths=[self.info["request_path"]] * 2,
                          sources=[dict(source="native:session", sample_ids=["call-1"])])))
        report = usage.summarize_project(str(manifest))
        self.assertEqual(report["totals"]["input_tokens"], 120)
        self.assertEqual(report["samples"], 1)

    def test_legacy_later_round_correction_does_not_poison_earlier_summary(self):
        import usage
        import usage_corrections
        protocol.publish(self.info["result_path"], self.response())
        later = self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        request = protocol.load_request(later["request_path"])
        directory = Path(later["request_path"]).parent / "usage"
        directory.mkdir()
        original = directory / "legacy.json"
        original.write_text(json.dumps({**self.sample(), **{k: request[k] for k in protocol.IDENTITY_FIELDS}}))
        before = original.read_bytes()
        spec = dict(version=1, request_path=later["request_path"], correction_id="legacy", reason="late telemetry", evidence=[
            dict(path=str(original), sha256=hashlib.sha256(before).hexdigest())], updates=[
                dict(request_path=later["request_path"], expected_revision=self.history(request=later["request_path"])["revision"],
                     sample=self.sample(output_tokens=25))])
        manifest = self.root / "legacy-fix.json"
        manifest.write_text(json.dumps(spec))
        usage_corrections.correct_manifest(str(manifest))
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(usage.summarize(self.info["request_path"])["samples"], 0)
        self.assertEqual(usage.summarize(later["request_path"])["known_subtotals"]["output_tokens"], 25)

    def test_chain_gap_or_tampered_baseline_is_detected(self):
        import usage
        import usage_corrections
        record = self.record()
        first = usage_corrections.correct_manifest(str(self.manifest([self.sample(output_tokens=21)])))
        usage_corrections.correct_manifest(str(self.manifest([self.sample(output_tokens=22)], "fix-2")))
        event = Path(first["path"])
        before = event.read_bytes()
        event.unlink()
        with self.assertRaises(ValueError):
            usage.summarize(self.info["request_path"])
        event.write_bytes(before)
        original = Path(record["path"])
        value = json.loads(original.read_text())
        value["input_tokens"] = 200
        original.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            usage.summarize(self.info["request_path"])

    def test_cli_history_and_correction(self):
        self.record()
        result = self.run_tool("usage.py", "history", "--request", self.info["request_path"],
                               "--source", "native:session", "--sample-id", "call-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["effective"]["output_tokens"], 20)
        spec = self.manifest([self.sample(output_tokens=25)])
        result = self.run_tool("usage.py", "correct", "--manifest", spec)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["duplicate"])

    def test_new_calls_after_several_corrections_do_not_break_replay(self):
        import usage
        import usage_corrections
        self.record()
        usage_corrections.correct_manifest(str(self.manifest([self.sample(role="verifier")], "first")))
        usage_corrections.correct_manifest(str(self.manifest([self.sample(role="controller")], "second")))
        self.record(self.sample(sample_id="call-2", role="controller"))
        report = usage.summarize(self.info["request_path"])
        self.assertEqual(report["groups"][0]["role"], "controller")
        self.assertEqual(report["samples"], 2)

    def test_reverting_values_does_not_revive_an_old_revision(self):
        import usage_corrections
        self.record()
        stale = self.manifest([self.sample(output_tokens=30)], "stale")
        usage_corrections.correct_manifest(str(self.manifest([self.sample(output_tokens=21)], "first")))
        usage_corrections.correct_manifest(str(self.manifest([self.sample()], "revert")))
        with self.assertRaisesRegex(ValueError, "revision"):
            usage_corrections.correct_manifest(str(stale))

    def test_publication_followed_by_lost_receipt_is_idempotent(self):
        import usage
        import usage_corrections
        self.record()
        spec = self.manifest([self.sample(output_tokens=21)])
        real_publish = usage.protocol.publish
        def lost_receipt(path, value):
            real_publish(path, value)
            raise OSError("receipt lost after commit")
        with patch.object(usage.protocol, "publish", side_effect=lost_receipt), self.assertRaises(OSError):
            usage_corrections.correct_manifest(str(spec))
        self.assertTrue(usage_corrections.correct_manifest(str(spec))["duplicate"])
        self.assertEqual(len(self.history()["corrections"]), 1)

    def test_independent_concurrent_corrections_both_survive(self):
        import usage_corrections
        self.record()
        self.record(self.sample(sample_id="call-2"))
        specs = [self.manifest([self.sample(sample_id=f"call-{i}", output_tokens=25)], f"fix-{i}") for i in (1, 2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda p: usage_corrections.correct_manifest(str(p)), specs))
        self.assertTrue(all(not r["duplicate"] for r in results))
        self.assertEqual(self.history()["effective"]["output_tokens"], 25)
        self.assertEqual(self.history("call-2")["effective"]["output_tokens"], 25)

    def test_invalid_manifest_shapes_and_changed_identity_rejected(self):
        import usage_corrections
        self.record()
        for mutation in (lambda v: v.update(version=True), lambda v: v.update(extra="typo"),
                         lambda v: v.update(evidence=[]), lambda v: v.update(updates=[]),
                         lambda v: v.update(reason=""),
                         lambda v: v["updates"][0].update(expected_revision="not-a-digest"),
                         lambda v: v["updates"][0]["sample"].update(source="different-source")):
            path = self.manifest([self.sample(output_tokens=25)])
            value = json.loads(path.read_text())
            mutation(value)
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                usage_corrections.correct_manifest(str(path))
        self.assertEqual(len(self.history()["corrections"]), 0)

    def test_oversized_event_is_rejected_before_publication(self):
        import usage
        import usage_corrections
        self.record()
        path = self.manifest([self.sample(output_tokens=25)])
        value = json.loads(path.read_text())
        value["reason"] = "x" * 3000
        path.write_text(json.dumps(value))
        limit = len(path.read_bytes()) + 1
        with patch.object(usage.protocol, "MAX_FILE_BYTES", limit), self.assertRaisesRegex(ValueError, "size limit"):
            usage_corrections.correct_manifest(str(path))
        self.assertEqual(len(self.history()["corrections"]), 0)
