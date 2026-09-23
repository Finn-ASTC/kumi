"""Backend-neutral shadow trials, no live API calls and no native terminal input."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import approval_shadow as shadow
import approval_backends as backends
import jev_backend as jev
from approval_shadow import protocol


class ShadowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="kumi-shadow-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        fixture = Path(__file__).parent / "fixtures/approval-shadow/cases.json"
        self.dataset = protocol.read_json(fixture)
        self.input = self.root / "cases.json"
        self.input.write_text(json.dumps(self.dataset))

    def init(self, config=None, allow_remote=False, max_calls=5, cases=None):
        if cases is not None:
            self.input.write_text(json.dumps({**self.dataset, "cases": cases}))
        return shadow.initialize(
            str(self.root),
            str(self.input),
            config or backends.default_config(),
            allow_remote,
            max_calls,
        )["trial_path"]

    def answer(self, packet, candidate="allow", config=None):
        config = config or backends.default_config("jev")
        return dict(
            version=1,
            request_id=packet["request_id"],
            input_sha256=packet["input_sha256"],
            model=config["model"],
            candidate=candidate,
            scores={k: 1 if k == candidate else 0 for k in backends.LABELS},
            score_semantics="probability",
            confidence=0.9,
            confidence_semantics="fixture_confidence",
            usage={"input_tokens": 20, "output_tokens": 1},
        )

    def result(self, config, packet):
        return {
            "status": "ok",
            "request_attempted": True,
            "answer": self.answer(packet, config=config),
        }

    def test_rules_never_call_backend_and_do_not_confuse_safe_with_authorized(self):
        trial = self.init()
        with patch.object(backends, "invoke", side_effect=AssertionError("no model")):
            result = shadow.run_trial(trial)
        rows = {r["case_id"]: r for r in result["cases"]}
        self.assertEqual(rows["unknown-read"]["candidate"], "escalate")
        self.assertEqual(rows["explicit-deny"]["candidate"], "deny")
        self.assertEqual(rows["allowed-test"]["candidate"], "escalate")
        self.assertIsNone(result["metrics"]["false_allow_share_of_allows"])
        self.assertEqual(result["reserved_attempt_slots"], 0)
        self.assertEqual(result["native_actions"], 0)
        self.assertFalse(result["execution_authorized"])

    def test_changed_implementation_or_prompt_cannot_resume_trial(self):
        trial = self.init(backends.default_config("jev"), True)
        manifest = protocol.read_json(trial)
        self.assertEqual(
            manifest["implementation"]["question_version"], jev.QUESTION_VERSION
        )
        with patch.object(jev, "QUESTION_VERSION", "changed"):
            with self.assertRaises(ValueError):
                shadow.run_trial(trial)
        with patch.object(jev, "question", return_value={"different": "prompt"}):
            with self.assertRaises(ValueError):
                shadow.report(trial)

    def test_finished_attempt_requires_original_intent(self):
        trial = self.init(
            backends.default_config("jev"), True, cases=self.dataset["cases"][:1]
        )
        with patch.object(backends, "invoke", side_effect=self.result):
            shadow.run_trial(trial)
        intent = next((Path(trial).parent / "intents").glob("*.json"))
        intent.unlink()
        with patch.object(backends, "invoke", side_effect=AssertionError("no repeat")):
            for action in (shadow.run_trial, shadow.report):
                with self.assertRaises(ValueError):
                    action(trial)

    def test_finished_attempt_checks_intent_binding(self):
        trial = self.init(
            backends.default_config("jev"), True, cases=self.dataset["cases"][:1]
        )
        with patch.object(backends, "invoke", side_effect=self.result):
            shadow.run_trial(trial)
        intent = next((Path(trial).parent / "intents").glob("*.json"))
        value = protocol.read_json(intent)
        intent.write_text(json.dumps({**value, "input_sha256": "changed"}))
        with self.assertRaises(ValueError):
            shadow.report(trial)

    def test_unknown_records_rejected_by_report_before_any_call(self):
        trial = self.init()
        protocol.publish(Path(trial).parent / "intents" / "unknown.json", {})
        with self.assertRaises(ValueError):
            shadow.report(trial)

    def test_same_host_independent_rules_for_five_profiles(self):
        cases = []
        for i, (host, profile) in enumerate(
            (
                ("codex", "default"),
                ("omp", "default"),
                ("hermes", "default"),
                ("opencode", "omo"),
                ("opencode", "pure"),
            )
        ):
            cases.append(
                {
                    **deepcopy(self.dataset["cases"][0]),
                    "id": f"case-{i}",
                    "host": host,
                    "profile": profile,
                }
            )
        result = shadow.run_trial(self.init(cases=cases))
        self.assertEqual(
            {r["reason"] for r in result["cases"]}, {"requires_semantic_review"}
        )
        self.assertEqual(len(result["cases"]), 5)

    def test_remote_requires_explicit_consent_before_trial_creation(self):
        before = set(self.root.iterdir())
        with self.assertRaises(ValueError):
            self.init(backends.default_config("jev"))
        self.assertEqual(before, set(self.root.iterdir()))

    def test_hard_gates_cannot_be_overridden_by_remote_suggestion(self):
        cases = self.dataset["cases"][1:4]
        trial = self.init(backends.default_config("jev"), True, cases=cases)
        with patch.object(
            backends, "invoke", side_effect=AssertionError("gate must precede model")
        ):
            result = shadow.run_trial(trial)
        self.assertEqual(
            [r["candidate"] for r in result["cases"]], ["escalate", "deny", "escalate"]
        )

    def test_expected_labels_and_host_identity_are_not_transmitted(self):
        trial = self.init(
            backends.default_config("jev"), True, cases=self.dataset["cases"][:1]
        )
        path, manifest, data = shadow.load(trial)
        packet = shadow.packet_for(manifest, data["cases"][0])
        self.assertEqual(
            set(packet["state"]), {"trusted_policy", "operation", "language"}
        )
        payload = jev.question(packet["state"])
        self.assertNotIn("expected", payload["state"])
        self.assertNotIn("host", payload["state"])
        self.assertNotIn("trial_path", json.dumps(payload))
        self.assertEqual(payload["questions"]["approval"]["type"], "choice")

    def test_successful_backend_is_only_a_shadow_suggestion_and_replay_is_free(self):
        trial = self.init(
            backends.default_config("jev"), True, cases=self.dataset["cases"][:1]
        )
        with patch.object(backends, "invoke", side_effect=self.result) as call:
            first = shadow.run_trial(trial)
            second = shadow.run_trial(trial)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(first, second)
        self.assertFalse(first["execution_authorized"])
        self.assertEqual(first["known_usage"]["input_tokens"], 20)
        self.assertEqual(first["cases"][0]["candidate"], "allow")

    def test_budget_counts_all_attempts_and_no_provider_fallback(self):
        cases = [self.dataset["cases"][0], self.dataset["cases"][4]]
        trial = self.init(
            backends.default_config("jev"), True, max_calls=1, cases=cases
        )
        with patch.object(
            backends,
            "invoke",
            return_value={"status": "timeout", "request_attempted": True},
        ) as call:
            result = shadow.run_trial(trial)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result["attempts_with_unknown_usage"], 1)
        self.assertEqual(result["cases"][1]["reason"], "call_budget_exhausted")
        self.assertEqual(result["cases"][0]["candidate"], "escalate")

    def test_crash_after_attempt_intent_does_not_repeat_possibly_billed_call(self):
        trial = self.init(
            backends.default_config("jev"), True, cases=self.dataset["cases"][:1]
        )
        with patch.object(
            backends, "invoke", side_effect=RuntimeError("simulated crash")
        ):
            with self.assertRaises(RuntimeError):
                shadow.run_trial(trial)
        with patch.object(
            backends, "invoke", side_effect=AssertionError("must not retry")
        ):
            result = shadow.run_trial(trial)
        self.assertEqual(result["cases"][0]["reason"], "previous_attempt_uncertain")
        self.assertIsNone(result["cases"][0]["request_attempted"])
        self.assertIsNone(result["cases"][0]["measured_attempt_seconds"])
        self.assertEqual(result["attempts_with_unknown_usage"], 1)

    def test_oversize_input_is_not_truncated_or_transmitted(self):
        case = deepcopy(self.dataset["cases"][0])
        case["operation"]["command"] += "x" * 20000
        trial = self.init(backends.default_config("jev"), True, cases=[case])
        with patch.object(
            backends, "invoke", side_effect=AssertionError("no oversized payload")
        ):
            result = shadow.run_trial(trial)
        self.assertEqual(result["cases"][0]["reason"], "input_too_large")

    def test_changed_dataset_and_config_cannot_resume_previous_decisions(self):
        trial = self.init()
        path = Path(trial).parent / "dataset.json"
        original = path.read_bytes()
        changed = protocol.read_json(path)
        changed["cases"][0]["operation"]["command"] = "new operation"
        path.write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            shadow.run_trial(trial)
        path.write_bytes(original)
        manifest = protocol.read_json(trial)
        manifest["config"]["model"] = "other"
        Path(trial).write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            shadow.run_trial(trial)

    def test_generic_command_adapter_contract_and_non_probability_scores(self):
        config = backends.default_config()
        config.update(
            adapter="command",
            model="local-fixture-v1",
            argv=[sys.executable, "-B", str(self.root / "adapter.py")],
        )
        (self.root / "adapter.py").write_text("""import json,sys
p=json.load(sys.stdin)
json.dump(dict(version=1,request_id=p['request_id'],input_sha256=p['input_sha256'],model='local-fixture-v1',
candidate='escalate',scores={'allow':-3,'deny':-2,'escalate':4},score_semantics='logit',confidence=None,
confidence_semantics=None,usage={'input_tokens':None,'output_tokens':None}),sys.stdout)
""")
        trial = self.init(config, cases=self.dataset["cases"][:1])
        result = shadow.run_trial(trial)
        self.assertEqual(result["cases"][0]["candidate"], "escalate")
        self.assertEqual(result["cases"][0]["backend_status"], "ok")
        self.assertEqual(result["attempts_with_unknown_usage"], 1)
        self.assertFalse(result["calibration_validated"])

    def test_command_wrong_binding_failure_and_timeout_are_not_allow(self):
        config = backends.default_config()
        config.update(
            adapter="command",
            model="fixture",
            timeout_seconds=0.05,
            argv=[sys.executable, "-c", "import time; time.sleep(3)"],
        )
        packet = {"request_id": "a" * 32, "input_sha256": "b" * 64}
        self.assertEqual(backends.invoke(config, packet)["status"], "timeout")
        config["timeout_seconds"] = 2
        config["argv"] = [sys.executable, "-c", 'print("{}"); raise SystemExit(1)']
        self.assertEqual(backends.invoke(config, packet)["status"], "adapter_error")
        answer = self.answer(packet, config=config)
        answer["request_id"] = "c" * 32
        config["argv"] = [
            sys.executable,
            "-c",
            "print(" + repr(json.dumps(answer)) + ")",
        ]
        self.assertEqual(backends.invoke(config, packet)["status"], "invalid_response")

    def test_jev_missing_key_returns_without_starting_process(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(subprocess, "run", side_effect=AssertionError("no key")),
        ):
            self.assertEqual(
                jev.evaluate({}, 1),
                {"status": "missing_api_key", "request_attempted": False},
            )

    def jev_reply(self):
        return {
            "model": jev.MODEL,
            "answers": {
                "approval": {
                    "type": "choice",
                    "choice": "allow",
                    "probabilities": {"allow": 0.9, "deny": 0.05, "escalate": 0.05},
                    "confidence": 0.7,
                }
            },
            "usage": {"input_tokens": 300, "output_tokens": 20},
        }

    def test_jev_valid_distribution_preserves_confidence_without_threshold(self):
        value = jev.parse_answer(self.jev_reply())
        self.assertEqual(value["confidence"], 0.7)
        self.assertEqual(value["candidate"], "allow")
        self.assertEqual(value["probabilities"]["allow"], 0.9)

    def test_jev_rejects_model_drift_missing_probabilities_and_inconsistent_answers(
        self,
    ):
        cases = []
        bad = self.jev_reply()
        bad["model"] = "jev-latest"
        cases.append(bad)
        bad = self.jev_reply()
        del bad["answers"]["approval"]["probabilities"]["deny"]
        cases.append(bad)
        bad = self.jev_reply()
        bad["answers"]["approval"]["choice"] = "deny"
        cases.append(bad)
        bad = self.jev_reply()
        bad["usage"]["input_tokens"] = True
        cases.append(bad)
        bad = self.jev_reply()
        bad["answers"]["approval"]["confidence"] = float("nan")
        cases.append(bad)
        bad = self.jev_reply()
        bad["answers"]["approval"]["probabilities"]["allow"] = 0.8
        cases.append(bad)
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                jev.parse_answer(bad)

    def test_http_errors_are_redacted_and_redirects_disabled(self):
        for code in (301, 401, 422, 429, 529):
            response = HTTPError(
                jev.ENDPOINT,
                code,
                "SECRET_REQUEST_CONTENT",
                {},
                io.BytesIO(b"SECRET_RESPONSE"),
            )
            opener = unittest.mock.Mock()
            opener.open.side_effect = response
            with patch.object(jev.request, "build_opener", return_value=opener):
                result = jev.http_once(b"{}", "PRIVATE_KEY")
            self.assertEqual(result, {"status": "http_error", "http_status": code})
            self.assertNotIn("SECRET", json.dumps(result))
        self.assertIsNone(
            jev.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://other.invalid"
            )
        )

    def test_worker_uses_total_timeout_without_key_in_argv_or_stdin(self):
        with (
            patch.dict(os.environ, {"TYPESAFE_API_KEY": "PRIVATE_KEY"}),
            patch.object(
                subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 0.1)
            ) as call,
        ):
            result = jev.evaluate({"model": jev.MODEL}, 0.1)
        self.assertEqual(result["status"], "timeout")
        self.assertNotIn("PRIVATE_KEY", repr(call.call_args))
        self.assertEqual(call.call_args.kwargs["timeout"], 0.1)

    def test_report_counts_false_allows_but_never_certifies_calibration(self):
        case = self.dataset["cases"][4]
        trial = self.init(backends.default_config("jev"), True, cases=[case])
        with patch.object(backends, "invoke", side_effect=self.result):
            result = shadow.run_trial(trial)
        self.assertEqual(result["metrics"]["false_allows"], 1)
        self.assertEqual(result["metrics"]["false_allow_share_of_allows"], 1)
        self.assertEqual(result["metrics"]["false_allow_rate_on_negatives"], 1)
        self.assertFalse(result["calibration_validated"])

    def test_cli_resume_with_no_network_or_native_host(self):
        script = str(Path(shadow.__file__))
        created = subprocess.run(
            [
                sys.executable,
                script,
                "init",
                "--root",
                str(self.root),
                "--dataset",
                str(self.input),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        trial = json.loads(created.stdout)["trial_path"]
        args = [sys.executable, script, "run", "--trial", trial]
        first = subprocess.run(args, capture_output=True, text=True)
        second = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertFalse(json.loads(first.stdout)["execution_authorized"])

    def test_invalid_config_and_real_data_are_rejected(self):
        config = backends.default_config("jev")
        for changes in (
            {"model": "jev-latest"},
            {"data_location": "local"},
            {"timeout_seconds": True},
            {"max_request_bytes": 12},
            {"argv": ["sh"]},
            {"endpoint": "https://other.invalid"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.init({**config, **changes}, True)
        self.input.write_text(json.dumps({**self.dataset, "source": "real"}))
        with self.assertRaises(ValueError):
            self.init()

    def test_unknown_score_semantics_and_unbound_confidence_are_rejected(self):
        config = backends.default_config("jev")
        packet = {"request_id": "a" * 32, "input_sha256": "b" * 64}
        for changes in (
            {"score_semantics": "unknown"},
            {"confidence_semantics": None},
            {"scores": {"allow": 1}},
            {"usage": {"input_tokens": 1}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                backends.validate_answer(
                    {**self.answer(packet), **changes}, config, packet
                )

    def test_incomplete_attempt_report_exposes_unknown_usage_before_resume(self):
        trial = self.init(
            backends.default_config("jev"), True, cases=self.dataset["cases"][:1]
        )
        with patch.object(backends, "invoke", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                shadow.run_trial(trial)
        result = shadow.report(trial)
        self.assertFalse(result["complete"])
        self.assertTrue(result["cases"][0]["pending_attempt_uncertain"])
        self.assertEqual(result["attempts_with_unknown_usage"], 1)

    def test_corrupted_result_cannot_turn_rule_escalation_into_allow(self):
        trial = self.init(cases=self.dataset["cases"][:1])
        shadow.run_trial(trial)
        path = next((Path(trial).parent / "results").glob("*.json"))
        original = protocol.read_json(path)
        for changes in (
            {"candidate": "allow"},
            {"version": True},
            {"request_attempted": 1},
            {"measured_attempt_seconds": -1},
            {"execution_authorized": True},
        ):
            path.write_text(json.dumps({**original, **changes}))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                shadow.report(trial)

    def test_result_publication_failure_retains_uncertainty_without_duplicate_request(
        self,
    ):
        trial = self.init(
            backends.default_config("jev"), True, cases=self.dataset["cases"][:1]
        )
        original = protocol.publish

        def publish(path, value):
            if Path(path).parent.name == "results":
                raise OSError("disk failure after response")
            return original(path, value)

        with (
            patch.object(backends, "invoke", side_effect=self.result) as call,
            patch.object(protocol, "publish", side_effect=publish),
        ):
            with self.assertRaises(OSError):
                shadow.run_trial(trial)
        self.assertEqual(call.call_count, 1)
        with patch.object(backends, "invoke", side_effect=AssertionError("no repeat")):
            result = shadow.run_trial(trial)
        self.assertEqual(result["cases"][0]["reason"], "previous_attempt_uncertain")

    def test_http_status_is_preserved_without_error_body(self):
        trial = self.init(
            backends.default_config("jev"), True, cases=self.dataset["cases"][:1]
        )
        with patch.object(
            backends,
            "invoke",
            return_value={
                "status": "http_error",
                "http_status": 401,
                "request_attempted": True,
            },
        ):
            result = shadow.run_trial(trial)
        self.assertEqual(result["cases"][0]["backend_http_status"], 401)
        self.assertEqual(result["cases"][0]["candidate"], "escalate")
        self.assertEqual(result["attempts_with_unknown_usage"], 1)

    def test_jev_http_parser_accepts_documented_shape_and_rejects_large_body(self):
        opener = unittest.mock.Mock()
        for raw, status in (
            (json.dumps(self.jev_reply()).encode(), "ok"),
            (b"x" * (jev.MAX_RESPONSE_BYTES + 1), "response_too_large"),
            (b'{"broken":', "transport_or_response_error"),
        ):
            opener.open.return_value = io.BytesIO(raw)
            with patch.object(jev.request, "build_opener", return_value=opener):
                result = jev.http_once(b"{}", "FIXTURE_KEY")
            self.assertEqual(result["status"], status)
            self.assertNotIn("FIXTURE_KEY", json.dumps(result))

    def test_jev_and_command_adapters_return_same_outer_contract(self):
        config = backends.default_config("jev")
        packet = {"request_id": "a" * 32, "input_sha256": "b" * 64, "state": {}}
        provider = {
            "status": "ok",
            "request_attempted": True,
            "answer": jev.parse_answer(self.jev_reply()),
        }
        with patch.object(jev, "evaluate", return_value=provider):
            result = backends.invoke(config, packet)
        self.assertEqual(set(result["answer"]), set(self.answer(packet)))
        self.assertEqual(result["answer"]["confidence"], 0.7)
        self.assertEqual(result["answer"]["score_semantics"], "probability")

    def test_generic_confidence_is_not_forced_to_jev_probability_scale(self):
        config = backends.default_config()
        config.update(
            adapter="command", model="local-fixture", argv=["/fixture/adapter"]
        )
        packet = {"request_id": "a" * 32, "input_sha256": "b" * 64}
        answer = self.answer(packet, config=config)
        answer.update(
            confidence=12,
            confidence_semantics="backend_margin",
            score_semantics="logit",
            scores={"allow": 12, "deny": -4, "escalate": 0},
        )
        self.assertEqual(
            backends.validate_answer(answer, config, packet)["confidence"], 12
        )

    def test_redirected_result_storage_and_result_files_are_rejected(self):
        trial = self.init(cases=self.dataset["cases"][:1])
        shadow.run_trial(trial)
        root = Path(trial).parent
        path = next((root / "results").glob("*.json"))
        backup = self.root / "result-backup.json"
        path.rename(backup)
        path.symlink_to(backup)
        with self.assertRaises(OSError):
            shadow.run_trial(trial)
        path.unlink()
        backup.rename(path)
        (root / "results").rename(root / "saved-results")
        (root / "results").symlink_to(root / "saved-results", target_is_directory=True)
        with self.assertRaises(ValueError):
            shadow.run_trial(trial)
