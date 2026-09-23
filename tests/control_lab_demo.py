#!/usr/bin/env python3
"""Retain a model-free all-host metering/recovery experiment. No terminal input."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

import control_lab
import control_usage_fixtures
import e2e_runner
import protocol
import jobs


def run(parent: Path) -> dict[str, Any]:
    """Exercise the public experiment CLI with synthetic, explicitly labelled records."""
    root = Path(tempfile.mkdtemp(prefix="control-demo-", dir=parent)).resolve()
    seed, registry, evidence = root / "seed", root / "registry", root / "commands"
    for directory in (seed, registry, evidence):
        directory.mkdir(mode=0o700)
    (seed / "value.txt").write_text("fixed input\n")
    targets = []
    for host in sorted(e2e_runner.KINDS):
        for mode in (("omo", "pure") if host == "opencode" else (None,)):
            name = host + ("_" + mode if mode else "")
            targets.append({"name": name, "kind": host, "label": "实验基础验证", "seed": str(seed),
                        **({"opencode_mode": mode} if mode else {}),
                        "task_packet": {"objective": "Control foundation fixture", "scope": "Owned directory",
                                        "acceptance": ["Pinned input and explicit coverage"]},
                        "scope": {"include": ["."], "exclude": []},
                        "verification_plan": {"commands": [{"argv": [sys.executable, "-c",
                            "from pathlib import Path; assert Path('value.txt').read_text() == 'fixed input\\n'"],
                            "timeout_seconds": 3}], "artifacts": [], "dependencies": {}, "environment": {}}})
    spec = {"version": 1, "input_version": "control-foundation-fixture-v2",
            "scenario": {"version": 1, "targets": targets},
            "resources": [{"kind": "port", "owner": targets[0]["name"], "address": "127.0.0.1", "port": 48123,
                           "protocol": "tcp"}]}
    recipe = root / "recipe.json"
    protocol.publish(recipe, spec)
    sequence = 0

    def call(action: str, args: list[str], expected: int = 0) -> dict[str, Any]:
        nonlocal sequence
        sequence += 1
        argv = [sys.executable, str(Path(control_lab.__file__)), action, *args]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        protocol.publish(evidence / f"{sequence:02d}-{action}.json",
                         {"argv": argv, "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode != expected:
            raise RuntimeError(f"unexpected {action} exit; evidence retained: {evidence}")
        return json.loads(result.stdout if expected == 0 else result.stderr)

    info = call("init", ["--root", str(root), "--registry", str(registry), "--input", str(recipe), "--fixture"])
    path = info["experiment_path"]
    require = protocol.require
    require(not info["metering"]["binding_coverage_complete"], "initial missing coverage must remain unknown")
    conflict = call("init", ["--root", str(root), "--registry", str(registry), "--input", str(recipe), "--fixture"], 2)
    require("already reserved" in conflict["error"], "competing lab must be blocked")
    runner = protocol.read_json(info["runner_path"])
    proof = root / "synthetic-source-evidence.json"
    protocol.publish(proof, {"fixture": True, "note": "Synthetic format examples; no native agent executed."})
    plan: dict[str, Any] = {"version": 1, "run_path": runner["run_path"], "bindings": [], "imports": []}
    for name, target in runner["targets"].items():
        host = target["kind"]
        sid, turn = f"fixture-{name}", "fixture-turn"
        log = control_usage_fixtures.write_store(root / ("synthetic-" + name), host, sid, turn)
        plan["bindings"].append({"subject_id": "round:" + target["round_id"], "host": host, "session_id": sid,
                                 "log_path": str(log), "native_ids": [turn],
                                 "evidence": {"path": str(proof), "sha256": jobs.fingerprint(proof)}})
        plan["imports"].append({"version": 1, "host": host, "session_id": sid, "log_path": str(log),
                                "mappings": [{"native_id": turn, "request_path": target["request_path"],
                                              "attempt_id": "fixture", "purpose": "task", "role": "worker",
                                              "phase": "control-foundation-fixture"}]})
    plan_path = root / "fixture-metering.json"
    protocol.publish(plan_path, plan)
    first = call("collect", ["--experiment", path, "--label", "synthetic-import", "--input", str(plan_path)])
    second = call("collect", ["--experiment", path, "--label", "restarted-controller"])
    require(first["imported"] == len(targets) and second["imported"] == 0 and second["duplicates"] == len(targets),
            "resumed collection must not duplicate samples")
    require(first["usage"]["known_subtotals"] == dict(input_tokens=500, cached_input_tokens=300, output_tokens=100),
            "all synthetic formats must normalize the same sample")
    require(not first["binding_coverage_complete"], "unbound controller must remain visible")
    recovered = call("report", ["--experiment", path])
    require(all(row["selection"] == "selected" for row in recovered["host_matrix"]),
            "all hosts and profiles must be represented")
    require(all(row["capabilities"][kind] == ["not_tested"] for row in recovered["targets"]
                for kind in control_lab.CAPABILITIES), "fixtures must not certify native capabilities")
    release = root / "release.json"
    protocol.publish(release, {"evidence_path": str(proof), "note": "No hosts, servers or database writers were started",
                               "checks": {"writers_stopped": True, "ports_free": True, "databases_closed": True}})
    call("release", ["--experiment", path, "--input", str(release)])
    summary = {"root": str(root), "experiment_path": path, "fixture": True, "model_calls": 0,
               "passed": True, "checks": ["fixed-input", "prelaunch-coverage", "resource-conflict",
                                           "all-host-formats", "resumed-import", "unknown-controller", "explicit-release"],
               "host_matrix": recovered["host_matrix"],
               "synthetic_imported_samples": first["imported"], "resumed_duplicates": second["duplicates"],
               "native_capabilities": "not_tested", "metering_report": second["checkpoint_path"]}
    protocol.publish(root / "summary.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.root), ensure_ascii=False, indent=2))
