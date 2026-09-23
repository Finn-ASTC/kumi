"""Version drift and CLI failure must not become native compatibility claims."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import host_compat_probe as probe


class HostCompatibilityTests(unittest.TestCase):
    def test_versions_ignore_dependency_numbers_and_update_banner(self):
        samples = {"codex": "codex-cli 0.155.1\n", "omp": "omp/18.2.11 linux-x64\n",
                   "hermes": "Hermes Agent v0.21.3 (2026.9.14)\nPython 3.11.16\n3651 commits behind\n",
                   "opencode": "1.18.29\n", "herdr": "herdr 0.9.1\n", "tmux": "tmux 3.7c\n"}
        for host, version in {**probe.BASELINES, "codex": "0.155.1", "omp": "18.2.11"}.items():
            with self.subTest(host=host):
                self.assertEqual(probe.parse_version(host, samples[host]), version)
                self.assertIsNone(probe.parse_version(host, samples[host] * 2))
                self.assertIsNone(probe.parse_version(host, "Python 3.14.7\n"))

    def test_option_declarations_not_prose_substrings(self):
        text = ("  -s, --session  exact ID\n      --profile=<value>  help\n"
                "  --pure-extra  includes --pure in prose\n"
                "Example: tool --pure --dir /tmp\n")
        self.assertEqual(probe.help_options(text), {"--session", "--profile", "--pure-extra"})

    def test_version_change_with_matching_help_is_not_native_verification(self):
        def fake(argv, timeout):
            if argv[-1] == "--version":
                return {"status": "ok", "text": "codex-cli 0.155.1\n"}
            options = next(opts for args, opts in probe.SURFACES["codex"] if tuple(argv[1:]) == args)
            return {"status": "ok", "text": "\n".join("  " + opt + "  help" for opt in options)}
        with patch.object(probe.shutil, "which", return_value="/fixture/codex"), \
                patch.object(probe, "capture", side_effect=fake):
            report = probe.inspect_host("codex")
        self.assertTrue(report["version_changed"])
        self.assertEqual(report["status"], "advertised")
        self.assertFalse(report["native_behavior_verified"])
        self.assertNotIn('"text"', json.dumps(report))

    def test_missing_binary_does_not_run_commands(self):
        with patch.object(probe.shutil, "which", return_value=None), patch.object(probe, "capture") as call:
            report = probe.inspect_host("omp")
        call.assert_not_called()
        self.assertEqual(report["status"], "missing")
        self.assertIsNone(report["version_changed"])

    def test_unknown_or_failed_version_never_runs_help(self):
        for version in [{"status": "ok", "text": "not a version"},
                        {"status": "command_failed", "text": "codex-cli 0.155.1"}]:
            with self.subTest(version=version), \
                    patch.object(probe.shutil, "which", return_value="/fixture/codex"), \
                    patch.object(probe, "capture", return_value=version) as call:
                report = probe.inspect_host("codex")
                self.assertEqual(report["status"], "unknown_version")
                self.assertIsNone(report["version"])
                self.assertEqual(call.call_count, 1)

    def test_missing_help_or_failed_help_requires_review(self):
        for result in [{"status": "ok", "text": " --purely  help"},
                       {"status": "timeout", "text": "  --pure  help"}]:
            with patch.object(probe.shutil, "which", return_value="/fixture/opencode"), \
                    patch.object(probe, "capture", side_effect=[{"status": "ok", "text": "1.18.29"}, result]):
                report = probe.inspect_host("opencode")
            self.assertEqual(report["status"], "needs_review")
            self.assertFalse(report["native_behavior_verified"])

    def test_stderr_help_and_stdout_version_with_literal_argv(self):
        literal = "$(do-not-run); space `command`"
        result = probe.capture([sys.executable, "-c", "import sys; print(sys.argv[1]); print('  --pure  help', file=sys.stderr)", literal], 5)
        self.assertEqual(result["status"], "ok")
        self.assertIn(literal, result["text"])
        self.assertIn("--pure", probe.help_options(result["text"]))
        self.assertTrue(result["stderr_present"])

    def test_failed_process_timeout_missing_executable_and_large_output(self):
        failure = probe.capture([sys.executable, "-c", "raise SystemExit(7)"], 5)
        self.assertEqual((failure["status"], failure["exit_code"]), ("command_failed", 7))
        with patch.object(probe.subprocess, "run", side_effect=subprocess.TimeoutExpired(["fixture"], 1)):
            self.assertEqual(probe.capture(["fixture"], 1)["status"], "timeout")
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(probe.capture([str(Path(root) / "missing")], 1)["status"], "execution_failed")
        with patch.object(probe, "OUTPUT_LIMIT", 10):
            large = probe.capture([sys.executable, "-c", "import sys; print('x'*20, file=sys.stderr)"], 5)
        self.assertEqual(large["status"], "output_limit")
        self.assertTrue(large["truncated"])

    def test_omo_wrapper_is_not_installed_package_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "package.json"
            path.write_text(json.dumps({"dependencies": {"oh-my-openagent": "4.19.4"}}))
            self.assertEqual(probe.inspect_omo(path)["status"], "unknown")
            path.write_text(json.dumps({"name": "oh-my-openagent", "version": "4.19.4"}))
            report = probe.inspect_omo(path)
            self.assertEqual(report["status"], "metadata_only")
            self.assertFalse(report["loaded_plugin_verified"])
            self.assertFalse(report["native_behavior_verified"])
            path.write_text("[")
            self.assertEqual(probe.inspect_omo(path)["status"], "unknown")

    def test_cli_missing_selected_host_returns_nonzero_json(self):
        with tempfile.TemporaryDirectory() as root:
            result = subprocess.run([sys.executable, str(Path(probe.__file__)), "--host", "omp",
                                     "--executable", "omp=" + str(Path(root) / "missing")],
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(len(report["hosts"]), 1)
        self.assertEqual(report["hosts"][0]["status"], "missing")
        self.assertEqual(report["model_tasks_started"], 0)


if __name__ == "__main__":
    unittest.main()
