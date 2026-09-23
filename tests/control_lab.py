#!/usr/bin/env python3
"""Opt-in control experiment bookkeeping. Never approves, interrupts or sends notices."""

import argparse
import json
from pathlib import Path
import sys
import tempfile
from typing import Any
import uuid

import e2e_runner
import lab_resources

import controls
import delivery
import jobs
import metering
import protocol
import watch


CAPABILITIES = ("deny", "stop", "notice_idle", "notice_interrupt")
require = protocol.require


def seed_records(scenario: dict[str, Any]) -> dict[str, Any]:
    """Pin full input trees; task packets and verification plans live in the recipe."""
    records = {}
    for target in scenario["targets"]:
        seed = Path(target["seed"])
        require(seed.is_absolute() and seed.is_dir() and not seed.is_symlink(), "invalid seed directory")
        files, _ = delivery.inventory(seed, ["."], [])
        require(all(entry["kind"] != "symlink" for entry in files.values()), "seed symlinks unsupported")
        records[target["name"]] = files
    return records


def save(path: Path, value: dict[str, Any]) -> None:
    """Persist controller state after immutable evidence is published."""
    watch.atomic_save(path, value)
    jobs.sync_directory(path.parent)


def initialize(parent: Path, registry: Path, spec: dict[str, Any], fixture: bool = False) -> dict[str, Any]:
    """Create an owned recoverable lab and prelaunch metering plan, without host I/O."""
    require(parent.is_dir(), "existing experiment parent required")
    require(isinstance(spec, dict) and set(spec) == {"version", "input_version", "scenario", "resources"}
            and type(spec["version"]) is int and spec["version"] == 1, "invalid control recipe")
    jobs.require_text(spec["input_version"], "input version")
    scenario = spec["scenario"]
    require(isinstance(scenario, dict) and isinstance(scenario.get("targets"), list)
            and bool(scenario["targets"]), "scenario targets required")
    require(all(target.get("kind") in ("codex", "omp") for target in scenario["targets"]),
            "first control lab supports Codex and omp only")
    owners = {target["name"] for target in scenario["targets"]}
    resources = lab_resources.validate(spec["resources"])
    require(all(item["owner"] in owners for item in resources), "unknown resource owner")
    registry = lab_resources.directory(registry)
    seeds = seed_records(scenario)
    root = Path(tempfile.mkdtemp(prefix="control-lab-", dir=parent)).resolve()
    path = root / "experiment.json"
    spec = {**spec, "resources": resources}
    recipe = {"version": 1, "spec": spec, "seeds": seeds, "fixture": fixture}
    protocol.publish(root / "recipe.json", recipe)
    resources = lab_resources.validate(resources + [
        {"kind": "path", "owner": "experiment", "path": str(root), "purpose": "workspace"}])
    state = {"version": 1, "experiment_id": uuid.uuid4().hex, "experiment_path": str(path),
             "recipe_sha256": jobs.fingerprint(root / "recipe.json"), "fixture": fixture,
             "registry": str(registry), "resources": resources, "phase": "initializing",
             "runner_path": None, "plans": [], "checkpoints": [], "identities": {}, "observations": [],
             "created_at": protocol.utc_now()}
    save(path, state)
    try:
        lab_resources.reserve(registry, state["experiment_id"], str(path), resources)
        runner = e2e_runner.initialize(root, scenario, fixture, control_experiment=str(path))
        state["runner_path"] = runner["runner_path"]
        save(path, state)
        with e2e_runner.Runner(runner["runner_path"]) as controller:
            for name, target in controller.data["targets"].items():
                actual = delivery.inventory(Path(target["cwd"]), ["."], [])[0]
                require(actual == seeds[name], "copied seed changed during initialization")
        require(seed_records(scenario) == seeds, "seed changed during initialization")
        plan = {"version": 1, "run_path": runner["run_path"], "bindings": [], "imports": []}
        protocol.publish(root / "initial-metering.json", plan)
        registered = metering.register(str(root / "initial-metering.json"))
        state["plans"].append(registered["plan_path"])
        checkpoint = metering.checkpoint(registered["plan_path"], "before-launch")
        state["checkpoints"].append(checkpoint["checkpoint_path"])
        state["phase"] = "ready"
        save(path, state)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        state.update(phase="initialization_failed", error=str(exc))
        save(path, state)
        raise protocol.ProtocolError(f"{exc}; partial experiment retained: {path}") from exc
    return {"experiment_path": str(path), "runner_path": state["runner_path"],
            "fixture": fixture, "launches_agents": False, "metering": checkpoint}


def load(path: str | Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Fail closed on moved state, changed recipes or redirected evidence."""
    path = Path(path).absolute()
    require(path == path.resolve(), "experiment path redirected")
    state = protocol.read_json(path)
    require(state["version"] == 1 and state["experiment_path"] == str(path), "experiment identity changed")
    recipe_path = path.parent / "recipe.json"
    require(jobs.fingerprint(recipe_path) == state["recipe_sha256"], "experiment recipe changed")
    recipe = protocol.read_json(recipe_path)
    expected_resources = recipe["spec"]["resources"] + [
        {"kind": "path", "owner": "experiment", "path": str(path.parent), "purpose": "workspace"}]
    require(recipe["fixture"] == state["fixture"] and expected_resources == state["resources"],
            "experiment mode or resource recipe changed")
    return path, state, recipe


def same_instance(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Native turns/submission attempts can advance inside one pinned round/instance."""
    return all(left[k] == right[k] for k in
               ("index_path", "job_id", "round_id", "request", "resources", "launch")) and (
                   left["native"]["session_id"] == right["native"]["session_id"])


def preflight(controller: e2e_runner.Runner, name: str, *, submitting: bool = False) -> None:
    """Gate managed runner launch/submission; native binding is mandatory before submission."""
    pointer = controller.data.get("control_experiment")
    if pointer is None:
        return
    path, state, recipe = load(pointer)
    require(state["phase"] == "ready" and state["runner_path"] == str(controller.path)
            and state["fixture"] == controller.data["fixture"], "experiment not ready or runner changed")
    lab_resources.check(state["registry"], state["experiment_id"], str(path), state["resources"])
    require(seed_records(recipe["spec"]["scenario"]) == recipe["seeds"], "fixed experiment inputs changed")
    target = controller.target(name)
    expected = next(t for t in recipe["spec"]["scenario"]["targets"] if t["name"] == name)
    require(all(target[key] == value for key, value in expected.items()), "runner target differs from recipe")
    require(bool(state["plans"]) and bool(state["checkpoints"]), "prelaunch metering missing")
    first = protocol.read_json(state["checkpoints"][0])
    require(first["run_path"] == controller.data["run_path"] and first["label"] == "before-launch",
            "prelaunch metering belongs to another run")
    if not submitting and target["phase"] == "prepared":
        require(delivery.inventory(Path(target["cwd"]), ["."], [])[0] == recipe["seeds"][name],
                "workspace differs from fixed seed before launch")
    if submitting:
        current = controls.identity(jobs.load(Path(controller.data["index_path"]), target["job_id"]))
        pinned = state["identities"].get(name)
        require(pinned is not None and same_instance(pinned["identity"], current), "exact native identity missing or changed")
        require(jobs.fingerprint(Path(pinned["evidence"]["path"])) == pinned["evidence"]["sha256"],
                "native identity evidence changed")


def pin(path: str | Path, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Snapshot an already-bound native identity; never discover by cwd or newest log."""
    path, state, _ = load(path)
    with e2e_runner.Runner(state["runner_path"]) as controller:
        path, state, _ = load(path)
        preflight(controller, name)
        target = controller.target(name)
        require(target["phase"] == "started", "target has not started")
        actual = controls.identity(jobs.load(Path(controller.data["index_path"]), target["job_id"]))
        require(actual["resources"] is not None and actual["launch"] is not None
                and actual["native"]["session_id"], "exact transport/launch/native session required")
        require(set(payload) == {"identity", "evidence_path", "note"} and payload["identity"] == actual,
                "observed identity does not match current job")
        for other_name, other in state["identities"].items():
            if other_name != name and controller.target(other_name)["kind"] == target["kind"]:
                require(other["identity"]["native"]["session_id"] != actual["native"]["session_id"],
                        "native session already pinned to another target")
        record = {"identity": actual, "evidence": jobs.evidence(payload), "recorded_at": protocol.utc_now()}
        require(name not in state["identities"], "identity already pinned; do not silently replace an instance")
        state["identities"][name] = record
        save(path, state)
        return record


def collect(path: str | Path, label: str, plan_path: str | None = None) -> dict[str, Any]:
    """Append immutable metering plans/checkpoints, preserving old unknown coverage."""
    path, state, _ = load(path)
    with e2e_runner.Runner(state["runner_path"]) as controller:
        path, state, _ = load(path)
        require(state["phase"] == "ready", "experiment not ready")
        if plan_path:
            candidate = protocol.read_json(plan_path)
            require(candidate["run_path"] == controller.data["run_path"], "metering plan belongs to another run")
            registered = metering.register(plan_path)["plan_path"]
            if registered not in state["plans"]:
                state["plans"].append(registered)
            save(path, state)
        require(protocol.read_json(state["plans"][-1])["run_path"] == controller.data["run_path"],
                "metering plan belongs to another run")
        report = metering.checkpoint(state["plans"][-1], label)
        state["checkpoints"].append(report["checkpoint_path"])
        save(path, state)
        return report


def observe(path: str | Path, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Retain per-scenario observations without turning them into action permission."""
    path, state, _ = load(path)
    required = {"capability", "scenario", "ui_mode", "status", "identity", "evidence_path", "note", "checks"}
    require(set(payload) == required and payload["capability"] in CAPABILITIES
            and payload["status"] in ("observed", "failed", "unsupported", "uncertain"), "invalid observation")
    jobs.require_text(payload["scenario"], "scenario name")
    jobs.require_text(payload["ui_mode"], "observed UI mode")
    require(isinstance(payload["checks"], dict), "checks must be an object")
    with e2e_runner.Runner(state["runner_path"]) as controller:
        path, state, _ = load(path)
        require(state["phase"] == "ready", "experiment not ready")
        target = controller.target(name)
        actual = controls.identity(jobs.load(Path(controller.data["index_path"]), target["job_id"]))
        require(name in state["identities"] and payload["identity"] == actual, "observation identity changed")
        binding = state["identities"][name]
        require(jobs.fingerprint(Path(binding["evidence"]["path"])) == binding["evidence"]["sha256"],
                "native identity evidence changed")
        jobs.require_text(target.get("host_version"), "observed host version")
        pinned = binding["identity"]
        # Submission creates an attempt, while the native session/resources stay pinned.
        require(same_instance(actual, pinned), "target instance changed since pinning")
        capability = payload["capability"]
        if payload["status"] == "observed":
            checks = payload["checks"]
            if capability == "stop":
                require(set(checks) == controls.STOP_CHECKS and all(v == "clear" for v in checks.values()),
                        "stop requires full scoped-stop checks")
            elif capability == "deny":
                require(set(checks) == {"operation_denied", "scope_unchanged"}
                        and all(v is True for v in checks.values()), "deny needs operation and scope checks")
            else:
                keys = {"exact_recipient", "input_owner", "no_dialog", "draft_preserved", "notice_submitted",
                        "recipient_choice_preserved", "background"}
                require(set(checks) == keys and all(checks[k] is True for k in keys - {"background"})
                        and checks["background"] in ("running", "clear"), "notice needs separate interactivity evidence")
        record = {"version": 1, "target": name, "host": target["kind"],
                  "host_version": target.get("host_version"), "fixture": state["fixture"],
                  "recorded_at": protocol.utc_now(), "observation": payload, "evidence": jobs.evidence(payload),
                  "authorizes_input": False}
        evidence_path = controller.evidence("control-observation", record)
        state["observations"].append(evidence_path)
        save(path, state)
        return {"observation_path": evidence_path, "authorizes_input": False}


def report(path: str | Path) -> dict[str, Any]:
    """Show gaps explicitly; observation history never certifies a whole host."""
    path, state, _ = load(path)
    rows = []
    runner = protocol.read_json(state["runner_path"]) if state["runner_path"] else None
    for name, target in (runner["targets"].items() if runner else []):
        observations = []
        for filename in state["observations"]:
            record = protocol.read_json(filename)
            if record["target"] == name:
                proof = record["evidence"]
                require(jobs.fingerprint(Path(proof["path"])) == proof["sha256"], "observation evidence changed")
                observations.append(record)
        rows.append({"target": name, "host": target["kind"], "host_version": target.get("host_version"),
                     "identity_pinned": name in state["identities"],
                     "capabilities": {kind: [r["observation"]["status"] for r in observations
                                              if r["observation"]["capability"] == kind] or ["not_tested"]
                                      for kind in CAPABILITIES}})
    return {"experiment_path": str(path), "phase": state["phase"], "fixture": state["fixture"],
            "error": state.get("error"),
            "runner_path": state["runner_path"], "targets": rows,
            "metering": protocol.read_json(state["checkpoints"][-1]) if state["checkpoints"] else None,
            "resources": lab_resources.read(lab_resources.directory(state["registry"]))["claims"].get(state["experiment_id"]),
            "note": "Development evidence only. Unknown coverage is not zero cost; fixtures are not native certification."}


def release(path: str | Path, payload: dict[str, Any]) -> None:
    """Require explicit resource checks and existing job closure before relinquishing claims."""
    path, state, _ = load(path)
    require(set(payload) == {"evidence_path", "note", "checks"}
            and isinstance(payload["checks"], dict)
            and set(payload["checks"]) == {"writers_stopped", "ports_free", "databases_closed"}
            and all(value is True for value in payload["checks"].values()),
            "explicit resource-release checks required")
    proof = jobs.evidence(payload)
    # Holds can outlive failed initialization; no timer or controller crash releases them.
    if state["runner_path"]:
        with e2e_runner.Runner(state["runner_path"]) as controller:
            path, state, _ = load(path)
            for target in controller.data["targets"].values():
                job = jobs.load(Path(controller.data["index_path"]), target["job_id"])
                if job["launch"] or job["attempts"] or jobs.resources_for(job["active_request"]):
                    require(job["closed"] is not None, "allocated job must close through existing stop/completion gates")
                    require(job["closed"]["outcome"] in ("completed", "cancelled"), "failed bookkeeping does not prove stop")
            state["phase"] = "releasing"
            save(path, state)
            lab_resources.release(state["registry"], state["experiment_id"], proof)
            state["phase"] = "released"
            save(path, state)
    else:
        lab_resources.release(state["registry"], state["experiment_id"], proof)
        state["phase"] = "released"
        save(path, state)


def main() -> int:
    """Keep initialization, native observations and metering explicitly separate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "report", "pin", "observe", "collect", "release"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--experiment")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--target")
    parser.add_argument("--label")
    parser.add_argument("--fixture", action="store_true")
    args = parser.parse_args()
    try:
        require(args.action == "init" or not args.fixture, "fixture is an init-only choice")
        if args.action == "init":
            require(args.root is not None and args.registry is not None and args.input is not None,
                    "init needs root, registry and input")
            result = initialize(args.root, args.registry, protocol.read_json(args.input), args.fixture)
        else:
            require(args.experiment is not None, "experiment required")
            if args.action == "report":
                result = report(args.experiment)
            elif args.action == "collect":
                result = collect(args.experiment, args.label, str(args.input) if args.input else None)
            elif args.action == "release":
                release(args.experiment, protocol.read_json(args.input))
                result = {"released": True}
            else:
                require(args.input is not None and args.target is not None, "target and input required")
                result = globals()[args.action](args.experiment, args.target, protocol.read_json(args.input))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
