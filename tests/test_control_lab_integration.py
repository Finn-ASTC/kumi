"""All host profiles use real private tmux and deterministic peers, never native models."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import control_lab
import e2e_runner

import controls
import jobs
import protocol


@unittest.skipUnless(os.environ.get("ORCH_RUN_TMUX_TESTS") == "1", "opt-in private tmux socket")
@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class ControlLabIntegrationTests(unittest.TestCase):
    def test_all_host_profiles_start_pin_and_exit_owned_fixture_terminals(self):
        with tempfile.TemporaryDirectory(prefix="cl-") as directory:
            root = Path(directory)
            seed, registry = root / "seed", root / "registry"
            seed.mkdir()
            registry.mkdir()
            (seed / "input.txt").write_text("fixed\n")
            targets = []
            for host in sorted(e2e_runner.KINDS):
                for mode in (("omo", "pure") if host == "opencode" else (None,)):
                    targets.append({"name": host + ("_" + mode if mode else ""), "kind": host,
                                    **({"opencode_mode": mode} if mode else {}),
                                    "label": "统一启动验证", "seed": str(seed),
                                    "task_packet": {"objective": "Fixture identity check", "scope": "Owned terminal",
                                                    "acceptance": ["No native models started"]},
                                    "scope": {"include": ["."], "exclude": []},
                                    "verification_plan": {"commands": [{"argv": [sys.executable, "-c", "pass"],
                                                                        "timeout_seconds": 2}],
                                                          "artifacts": [], "dependencies": {}, "environment": {}}})
            info = control_lab.initialize(root, registry, {
                "version": 1, "input_version": "all-host-startup-fixture-v1",
                "scenario": {"version": 1, "targets": targets}, "resources": []}, fixture=True)
            runner_path = Path(info["runner_path"])
            cli = ["tmux", "-S", str(runner_path.parent / "tmux.sock"), "-f", "/dev/null"]
            proof = root / "fixture-proof.json"
            protocol.publish(proof, {"fixture": True, "native_models_launched": 0})
            preflight = {"host_version": "fixture-only", "evidence_path": str(proof),
                         "note": "Deterministic Python peer only", "checks": {
                             "model_and_approvals_preserved": True, "runtime_dependencies_verified": True}}

            def tmux(*args, check=True):
                return subprocess.run(cli + list(args), capture_output=True, text=True, check=check, timeout=5)

            def wait(predicate):
                deadline = time.monotonic() + 5
                while not predicate():
                    self.assertLess(time.monotonic(), deadline, "fixture did not become ready/exit")
                    time.sleep(0.02)

            try:
                tmux("new-session", "-d", "-s", "sentinel", sys.executable, "-c", "import time; time.sleep(60)")
                for target in targets:
                    name = target["name"]
                    with self.subTest(host=target["kind"], profile=target.get("opencode_mode")):
                        with e2e_runner.Runner(runner_path) as controller:
                            started = controller.start(name, preflight, transport="tmux")
                            actual_target = controller.target(name)
                            self.assertEqual(actual_target["launch_argv"][0], sys.executable)
                            self.assertIn("e2e_peer.py", actual_target["launch_argv"][1])
                            controller.native(name, {"native": {"session_id": "synthetic-" + name, "turn_id": None},
                                                     "evidence_path": str(proof), "note": "Synthetic peer identity"})
                            identity = controls.identity(jobs.load(Path(controller.data["index_path"]), actual_target["job_id"]))
                        wait(lambda: (runner_path.parent / "native" / (name + ".ready")).exists())
                        control_lab.pin(info["experiment_path"], name, {
                            "identity": identity, "evidence_path": str(proof), "note": "Exact synthetic peer/terminal"})
                        with e2e_runner.Runner(runner_path) as controller:
                            control_lab.preflight(controller, name, submitting=True)
                        title = tmux("list-windows", "-t", "=" + actual_target["agent"], "-F", "#{window_name}").stdout.strip()
                        self.assertEqual(title, started["page_label"])
                        tmux("send-keys", "-t", started["resources"]["pane"], "-l", "/exit")
                        tmux("send-keys", "-t", started["resources"]["pane"], "Enter")
                        wait(lambda: tmux("has-session", "-t", "=" + actual_target["agent"], check=False).returncode != 0)
                report = control_lab.report(info["experiment_path"])
                self.assertEqual(len(report["host_matrix"]), 5)
                self.assertTrue(all(row["identity_pinned"] for row in report["targets"]))
                self.assertTrue(all(row["capabilities"]["stop"] == ["not_tested"] for row in report["targets"]))
                self.assertEqual(tmux("has-session", "-t", "=sentinel", check=False).returncode, 0)
            finally:
                tmux("kill-server", check=False)  # This test's private socket only.


if __name__ == "__main__":
    unittest.main()
