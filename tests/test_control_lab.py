"""Development experiment contracts: concurrency, recovery and evidence separation."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import control_lab as lab
import control_lab_demo
import e2e_runner
import lab_resources as resources

import controls
import jobs
import protocol


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="resource-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def path(self, suffix="tree", owner="author", purpose="workspace"):
        return {"kind": "path", "owner": owner, "path": str(self.root / suffix), "purpose": purpose}

    def port(self, address="127.0.0.1", port=8123, protocol="tcp"):
        return {"kind": "port", "owner": "author", "address": address, "port": port, "protocol": protocol}

    def test_nested_paths_and_aliases_conflict_across_owners(self):
        (self.root / "alias").symlink_to(self.root / "tree", target_is_directory=True)
        for candidate in (self.path("tree/build", "verifier", "build"), self.path("alias", "verifier")):
            with self.assertRaisesRegex(ValueError, "conflicting"):
                resources.validate([self.path(), candidate])
        self.assertEqual(len(resources.validate([self.path(), self.path("tree/build", purpose="build")])), 2)

    def test_ports_include_wildcards_mapped_ipv4_and_protocol(self):
        for address in ("127.0.0.1", "0.0.0.0", "::", "::ffff:127.0.0.1"):
            self.assertTrue(resources.overlaps(resources.normalize(self.port()), resources.normalize(self.port(address))))
        self.assertFalse(resources.overlaps(self.port(), self.port("127.0.0.2")))
        self.assertFalse(resources.overlaps(self.port(), self.port(protocol="udp")))
        self.assertFalse(resources.overlaps(self.port(), self.port(port=8124)))

    def test_invalid_declarations_rejected(self):
        for value in (self.port(port=True), self.port(port=0), self.port(address="localhost"),
                      {**self.path(), "path": "relative"}, {**self.path(), "extra": 1}):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                resources.validate([value])

    def test_atomic_reservation_retry_and_explicit_release(self):
        first = resources.reserve(self.root, "a" * 32, "first.json", [self.path()])
        self.assertEqual(resources.reserve(self.root, "a" * 32, "first.json", [self.path()]), first)
        with self.assertRaisesRegex(ValueError, "another experiment"):
            resources.reserve(self.root, "b" * 32, "second.json", [self.path("tree/build")])
        resources.release(self.root, "a" * 32, {"note": "checked"})
        resources.release(self.root, "a" * 32, {"note": "checked"})
        with self.assertRaisesRegex(ValueError, "changed"):
            resources.release(self.root, "a" * 32, {"note": "different"})
        resources.reserve(self.root, "b" * 32, "second.json", [self.path("tree/build")])
        with self.assertRaisesRegex(ValueError, "released"):
            resources.reserve(self.root, "a" * 32, "first.json", [self.path()])

    def test_parallel_processes_cannot_both_reserve(self):
        script = ("import sys; sys.path.insert(0, sys.argv[1]); import lab_resources as r; "
                  "r.reserve(sys.argv[2], sys.argv[3], 'manifest', "
                  "[{'kind':'port','owner':'x','address':'127.0.0.1','port':8123,'protocol':'tcp'}])")
        processes = [subprocess.Popen([sys.executable, "-c", script, str(Path(lab.__file__).parent),
                                     str(self.root), c * 32], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for c in "ab"]
        for process in processes:
            process.communicate(timeout=10)
        self.assertEqual(sum(p.returncode == 0 for p in processes), 1)
        self.assertEqual(len(resources.read(self.root)["claims"]), 1)

    def test_corruption_and_redirection_do_not_erase_claims(self):
        (self.root / "reservations.json").write_text("broken")
        with self.assertRaises(ValueError):
            resources.reserve(self.root, "a" * 32, "first", [])
        (self.root / "reservations.json").unlink()
        (self.root / "reservations.json").symlink_to(self.root / "absent")
        with self.assertRaises(OSError):
            resources.reserve(self.root, "a" * 32, "first", [])


class ControlLabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="control-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry"
        self.registry.mkdir()
        self.seed = self.root / "seed"
        self.seed.mkdir()
        (self.seed / "value.txt").write_text("before")
        target = {"name": "author", "label": "控制验证", "kind": "codex", "seed": str(self.seed),
                  "task_packet": {"objective": "Bounded control test", "scope": "Owned workspace",
                                  "acceptance": ["Explicit evidence"]},
                  "scope": {"include": ["."], "exclude": []},
                  "verification_plan": {"commands": [{"argv": [sys.executable, "-c", "assert True"],
                                                       "timeout_seconds": 2}],
                                        "artifacts": [], "dependencies": {}, "environment": {}}}
        self.spec = {"version": 1, "input_version": "fixed-input-v1",
                     "scenario": {"version": 1, "targets": [target]}, "resources": []}
        info = lab.initialize(self.root, self.registry, self.spec, fixture=True)
        self.path = Path(info["experiment_path"])
        self.runner = info["runner_path"]
        self.proof = self.root / "proof.json"
        self.proof.write_text('{"fixture": true}')

    def state(self):
        return protocol.read_json(self.path)

    def started(self):
        with e2e_runner.Runner(self.runner) as controller:
            target = controller.target("author")
            controller.mutate("author", "update", {"launch": {"argv": ["fixture"], "profile": "fixture"},
                                                    "native": {"session_id": "exact-session", "turn_id": None}})
            controller.record("author", {"mode": "tmux", "session": target["agent"], "agent": target["agent"],
                                         "pane": "%1", "owns_agent": True, "owns_pane": True,
                                         "owns_workspace": False, "owns_session": True,
                                         "tmux_socket": str(controller.root / "tmux.sock")})
            target.update(phase="started", host_version="fixture-only")
            controller.save()
        return self.identity()

    def identity(self):
        with e2e_runner.Runner(self.runner) as controller:
            return controls.identity(jobs.load(Path(controller.data["index_path"]), controller.target("author")["job_id"]))

    def pin(self):
        identity = self.started()
        return lab.pin(self.path, "author", {"identity": identity, "evidence_path": str(self.proof), "note": "fixture binding"})

    def observation(self, capability="notice_idle"):
        return {"capability": capability, "scenario": "fixture-idle", "ui_mode": "fixture", "status": "observed",
                "identity": self.identity(), "evidence_path": str(self.proof), "note": "fixture only",
                "checks": {"exact_recipient": True, "input_owner": True, "no_dialog": True,
                           "draft_preserved": True, "notice_submitted": True,
                           "recipient_choice_preserved": True, "background": "running"}}

    def test_init_has_metering_before_launch_unknown_coverage_and_owned_path(self):
        report = lab.report(self.path)
        self.assertTrue(report["fixture"])
        self.assertEqual(report["phase"], "ready")
        self.assertEqual(report["metering"]["label"], "before-launch")
        self.assertFalse(report["metering"]["binding_coverage_complete"])
        self.assertTrue(report["metering"]["unbound_subjects"])
        self.assertEqual(report["targets"][0]["capabilities"]["deny"], ["not_tested"])
        self.assertEqual(report["resources"]["resources"][-1]["path"], str(self.path.parent))
        with e2e_runner.Runner(self.runner) as controller:
            lab.preflight(controller, "author")

    def test_fixed_seed_change_blocks_start_before_transport(self):
        (self.seed / "value.txt").write_text("different")
        with e2e_runner.Runner(self.runner) as controller, patch.object(controller, "execute") as execute:
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                controller.start("author", {}, transport="tmux")
            execute.assert_not_called()

    def test_copied_workspace_change_blocks_start(self):
        with e2e_runner.Runner(self.runner) as controller:
            (Path(controller.target("author")["cwd"]) / "value.txt").write_text("different")
            with self.assertRaisesRegex(ValueError, "workspace differs"):
                lab.preflight(controller, "author")

    def test_recipe_change_and_missing_reservation_fail_closed(self):
        recipe_path = self.path.parent / "recipe.json"
        original = recipe_path.read_bytes()
        recipe_path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "recipe changed"):
            lab.report(self.path)
        recipe_path.write_bytes(original)
        (self.registry / "reservations.json").write_text('{"version":1,"claims":{}}')
        with e2e_runner.Runner(self.runner) as controller:
            with self.assertRaisesRegex(ValueError, "reservation missing"):
                lab.preflight(controller, "author")

    def test_submit_requires_exact_native_pin_before_io(self):
        self.started()
        with e2e_runner.Runner(self.runner) as controller, patch.object(controller, "execute") as execute:
            with self.assertRaisesRegex(ValueError, "identity missing"):
                controller.submit("author", {})
            execute.assert_not_called()

    def test_pin_rejects_wrong_identity_and_silent_replacement(self):
        identity = self.started()
        payload = {"identity": {**identity, "round_id": "wrong"}, "evidence_path": str(self.proof), "note": "checked"}
        with self.assertRaisesRegex(ValueError, "does not match"):
            lab.pin(self.path, "author", payload)
        payload["identity"] = identity
        lab.pin(self.path, "author", payload)
        with self.assertRaisesRegex(ValueError, "already pinned"):
            lab.pin(self.path, "author", payload)
        with e2e_runner.Runner(self.runner) as controller:
            lab.preflight(controller, "author", submitting=True)
            controller.mutate("author", "update", {"native": {"session_id": "replacement", "turn_id": None}})
            with self.assertRaisesRegex(ValueError, "identity missing or changed"):
                lab.preflight(controller, "author", submitting=True)

    def test_modified_pin_evidence_blocks_submission(self):
        self.pin()
        self.proof.write_text('{"fixture": false}')
        with e2e_runner.Runner(self.runner) as controller:
            with self.assertRaisesRegex(ValueError, "evidence changed"):
                lab.preflight(controller, "author", submitting=True)

    def test_notice_with_background_running_is_not_stop_or_permission(self):
        self.pin()
        result = lab.observe(self.path, "author", self.observation())
        self.assertFalse(result["authorizes_input"])
        record = protocol.read_json(result["observation_path"])
        self.assertTrue(record["fixture"])
        with self.assertRaisesRegex(ValueError, "scoped-stop"):
            lab.observe(self.path, "author", self.observation("stop"))
        with e2e_runner.Runner(self.runner) as controller:
            job = jobs.load(Path(controller.data["index_path"]), controller.target("author")["job_id"])
            self.assertIsNone(job.get("control"))
            with self.assertRaisesRegex(ValueError, "confirmed stop"):
                controls.cancellation(job, 0)

    def test_observation_failed_unknown_and_bad_evidence_preserved(self):
        self.pin()
        for status in ("failed", "unsupported", "uncertain"):
            lab.observe(self.path, "author", {**self.observation(), "status": status, "checks": {}})
        report = lab.report(self.path)
        self.assertEqual(report["targets"][0]["capabilities"]["notice_idle"], ["failed", "unsupported", "uncertain"])
        self.proof.write_text("changed")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            lab.report(self.path)

    def test_collect_restarts_without_replacing_old_unknown_checkpoint(self):
        before = self.state()["checkpoints"][0]
        report = lab.collect(self.path, "after-fixture")
        self.assertTrue(report["collection_complete"])
        self.assertFalse(report["binding_coverage_complete"])
        self.assertEqual(self.state()["checkpoints"][0], before)
        self.assertEqual(len(self.state()["checkpoints"]), 2)
        self.assertEqual(lab.report(self.path)["metering"]["label"], "after-fixture")

    def test_other_run_metering_plan_rejected(self):
        other = lab.initialize(self.root, self.registry, self.spec, fixture=True)
        plan = lab.load(other["experiment_path"])[1]["plans"][0]
        with self.assertRaisesRegex(ValueError, "another run"):
            lab.collect(self.path, "wrong-run", plan)

    def test_initialized_resources_remain_after_failure(self):
        with patch.object(lab.metering, "register", side_effect=ValueError("injected interruption")):
            with self.assertRaisesRegex(ValueError, "partial experiment retained"):
                lab.initialize(self.root, self.registry, self.spec, fixture=True)
        failed = [p for p in self.root.glob("control-lab-*/experiment.json") if p != self.path][0]
        report = lab.report(failed)
        self.assertEqual(report["phase"], "initialization_failed")
        self.assertEqual(report["resources"]["status"], "held")
        with e2e_runner.Runner(report["runner_path"]) as controller:
            with self.assertRaisesRegex(ValueError, "not ready"):
                lab.preflight(controller, "author")

    def test_runner_carries_guard_even_if_initialization_dies_before_returning(self):
        with patch.object(e2e_runner.delivery, "capture", side_effect=ValueError("injected baseline failure")):
            with self.assertRaisesRegex(ValueError, "partial experiment retained"):
                lab.initialize(self.root, self.registry, self.spec, fixture=True)
        failed = next(p for p in self.root.glob("control-lab-*/experiment.json") if p != self.path)
        orphan = next(failed.parent.glob("orch-e2e-*/runner.json"))
        self.assertEqual(protocol.read_json(orphan)["control_experiment"], str(failed))
        with e2e_runner.Runner(orphan) as controller:
            with self.assertRaisesRegex(ValueError, "not ready"):
                controller.start("author", {}, transport="tmux")

    def test_native_turn_advances_but_session_replacement_cannot_claim_observation(self):
        self.pin()
        with e2e_runner.Runner(self.runner) as controller:
            controller.mutate("author", "update", {"native": {"session_id": "exact-session", "turn_id": "turn-1"}})
        lab.observe(self.path, "author", self.observation())
        with e2e_runner.Runner(self.runner) as controller:
            controller.mutate("author", "update", {"native": {"session_id": "replacement", "turn_id": "turn-1"}})
        with self.assertRaisesRegex(ValueError, "instance changed"):
            lab.observe(self.path, "author", self.observation())

    def test_release_recovers_if_registry_write_lands_before_manifest_save(self):
        payload = {"evidence_path": str(self.proof), "note": "No launch", "checks": {
            "writers_stopped": True, "ports_free": True, "databases_closed": True}}
        save = lab.save

        def crash(path, state):
            if state["phase"] == "released":
                raise OSError("injected after registry commit")
            save(path, state)

        with patch.object(lab, "save", side_effect=crash):
            with self.assertRaisesRegex(OSError, "injected"):
                lab.release(self.path, payload)
        self.assertEqual(self.state()["phase"], "releasing")
        lab.release(self.path, payload)
        self.assertEqual(lab.report(self.path)["phase"], "released")

    def test_two_format_cli_demo_imports_and_resumes_without_native_claims(self):
        result = control_lab_demo.run(self.root)
        self.assertTrue(result["passed"])
        self.assertEqual(result["synthetic_imported_samples"], 2)
        self.assertEqual(result["resumed_duplicates"], 2)
        self.assertEqual(result["native_capabilities"], "not_tested")

    def test_release_needs_checks_and_stop_not_just_a_notice(self):
        self.pin()
        lab.observe(self.path, "author", self.observation())
        payload = {"evidence_path": str(self.proof), "note": "fixture", "checks": {
            "writers_stopped": True, "ports_free": True, "databases_closed": True}}
        with self.assertRaisesRegex(ValueError, "must close"):
            lab.release(self.path, payload)
        self.assertEqual(lab.report(self.path)["resources"]["status"], "held")

    def test_never_started_release_is_idempotent_and_blocks_launch(self):
        payload = {"evidence_path": str(self.proof), "note": "No launch", "checks": {
            "writers_stopped": True, "ports_free": True, "databases_closed": True}}
        lab.release(self.path, payload)
        lab.release(self.path, payload)
        self.assertEqual(lab.report(self.path)["phase"], "released")
        with e2e_runner.Runner(self.runner) as controller:
            with self.assertRaisesRegex(ValueError, "not ready"):
                lab.preflight(controller, "author")

    def test_recovery_cli_preserves_fixture_and_unknowns(self):
        result = subprocess.run([sys.executable, lab.__file__, "report", "--experiment", str(self.path)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["fixture"])
        self.assertFalse(report["metering"]["binding_coverage_complete"])

    def test_unknown_hosts_and_conflicts_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec["scenario"]["targets"][0]["kind"] = "hermes"
        with self.assertRaisesRegex(ValueError, "Codex and omp"):
            lab.initialize(self.root, self.registry, spec)


if __name__ == "__main__":
    unittest.main()
