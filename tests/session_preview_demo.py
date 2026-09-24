#!/usr/bin/env python3
"""Retained CLI fault experiment; synthetic sessions, no native hosts/models."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/agent-orchestrator/scripts"


def run(parent: Path, scripts: Path = SCRIPTS) -> dict:
    root = Path(tempfile.mkdtemp(prefix="kumi-session-preview-", dir=parent)).resolve()
    project = root / "project"
    project.mkdir()
    (project / "task.txt").write_text("Review a synthetic session directory\nPRIVATE_TASK_SENTINEL\n")
    commands = root / "commands"
    commands.mkdir()
    sequence = 0

    def call(script: str, *args: str) -> tuple[int, dict]:
        nonlocal sequence
        sequence += 1
        reply = subprocess.run([sys.executable, str(scripts / script), *map(str, args)],
                               capture_output=True, text=True, timeout=15)
        # Retained private evidence may include synthetic lease tokens.
        path = commands / f"{sequence:03d}-{script}.json"
        path.write_text(json.dumps({"code": reply.returncode, "stdout": reply.stdout,
                                    "stderr": reply.stderr}, indent=2))
        value = json.loads(reply.stdout if reply.returncode == 0 else reply.stderr)
        return reply.returncode, value

    def ok(script: str, *args: str) -> dict:
        code, value = call(script, *args)
        if code != 0:
            raise RuntimeError(f"CLI failed; see retained evidence in {commands}")
        return value

    run = ok("runs.py", "init", "--root", str(root))
    info = ok("protocol.py", "prepare", "--cwd", str(project), "--parent-depth", "0",
              "--run", run["run_path"], "--task-file", str(project / "task.txt"), "--brief")
    state = ok("jobs.py", "register", "--index", run["index_path"], "--request", info["request_path"])
    state = ok("jobs.py", "claim", "--index", run["index_path"], "--job", state["job_id"],
               "--expect-revision", str(state["revision"]), "--owner", "fixture-controller")
    payload = root / "binding.json"
    payload.write_text(json.dumps({"native": {"session_id": "synthetic-worker", "turn_id": None},
                                   "launch": {"argv": ["fixture-not-a-host"], "profile": "default"}}))
    state = ok("jobs.py", "update", "--index", run["index_path"], "--job", state["job_id"],
               "--expect-revision", str(state["revision"]), "--token", state["lease"]["token"],
               "--input", str(payload))
    regargs = ["--run", run["run_path"], "--host", "omp", "--store", str(root / "synthetic-store"),
               "--profile", "default", "--session-id", "synthetic-worker", "--role", "worker",
               "--job", state["job_id"], "--round", state["round_id"]]
    ok("registry.py", "register", "--id", "a" * 32, *regargs)
    capargs = ["--run", run["run_path"], "--host", "omp", "--profile", "default",
               "--capability", "resume", "--status", "verified", "--host-version", "synthetic-1",
               "--observed-at", "2026-09-24T00:00:00Z"]
    missing_code, _ = call("capabilities.py", "register", *capargs,
                           "--evidence-path", str(root / "missing.json"))
    checks = {"missing_evidence_rejected": missing_code != 0}
    # A separate run keeps a buggy successful registration from poisoning the
    # real-file case; the before/after demo is identical on both revisions.
    cap_run = run if missing_code != 0 else ok("runs.py", "init", "--root", str(root))
    proof = root / "fixture-proof.json"
    proof.write_text(json.dumps({"fixture": True, "claim": "synthetic resume", "private": "PRIVATE_PROOF_SENTINEL"}))
    capargs[1] = cap_run["run_path"]
    registered = ok("capabilities.py", "register", *capargs, "--evidence-path", str(proof))
    checks["evidence_pinned"] = registered.get("evidence_sha256") == hashlib.sha256(proof.read_bytes()).hexdigest()
    view = ok("sessions.py", "--run", run["run_path"])
    checks["runtime_unknown_blocks_resume"] = not view["sessions"][0]["recovery_preview"]["dry_run"]["eligible"]
    proof.write_text("changed proof bytes")
    changed = ok("capabilities.py", "list", "--run", cap_run["run_path"])
    checks["changed_evidence_not_verified"] = changed["records"][0].get("effective_status", changed["records"][0]["status"]) != "verified"
    ok("registry.py", "register", "--id", "b" * 32, *regargs)
    ambiguous = ok("sessions.py", "--run", run["run_path"])
    checks["ambiguous_registration_not_selected"] = ambiguous["sessions"][0]["native_identity"]["status"] == "ambiguous_registration"
    checks["private_body_not_in_directory"] = all(
        sentinel not in json.dumps(view) for sentinel in ("PRIVATE_TASK_SENTINEL", "PRIVATE_PROOF_SENTINEL"))
    summary = {"root": str(root), "run_path": run["run_path"], "fixture": True,
               "model_calls": 0, "native_hosts_started": 0, "passed": all(checks.values()), "checks": checks}
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--scripts", type=Path, default=SCRIPTS)
    args = parser.parse_args()
    result = run(args.root, args.scripts)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
