#!/usr/bin/env python3
"""Retain a real Rust debug/release rejection and rework, without native agents.

Requires Linux /proc, cargo, rustc, rustfmt, clippy and Python 3.10+. No network,
terminal or model is used. All builds and corrections stay in the allocated lab.
"""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills/agent-orchestrator/scripts"))
import delivery
import jobs
import protocol


def require(condition: bool, message: str) -> None:
    """Fail the demonstration even if Python assertions are disabled."""
    if not condition:
        raise RuntimeError(message)


def digest(path: Path) -> str:
    """Fingerprint immutable evidence for comparison after rework."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def active_group_members(group: int) -> list[int]:
    """Observe live Linux processes in this fixture's recorded process group."""
    active = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            continue
        if int(fields[2]) == group and fields[0] not in ("Z", "X"):
            active.append(int(entry.name))
    return active


def coverage(plan: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    """Account for stopped/unrun commands without changing the executor schema."""
    rows = []
    for position, command in enumerate(plan["commands"], 1):
        row = {"command_position": position, "argv": command["argv"], "owner": "fixture-verifier"}
        if position > len(result["commands"]):
            row.update(status="not_run", reason="Earlier command stopped this attempt",
                       remaining_action="Reverify the required matrix after rework")
        else:
            actual = result["commands"][position - 1]
            status = ("timeout" if actual["outcome"] == "timeout" else
                      "passed" if actual["outcome"] == "exited" and actual["exit_code"] == 0 else "failed")
            sealed_result = Path(result["attempt_path"]).with_name("result.json")
            row.update(status=status, evidence=str(sealed_result), evidence_sha256=digest(sealed_result),
                       remaining_action=None if status == "passed" else "Preserve failure and rework")
        rows.append(row)
    return rows


def run_demo(parent: Path) -> dict[str, Any]:
    """Execute the published example on bad and corrected fixture snapshots."""
    require(sys.platform.startswith("linux"), "This demo uses Linux /proc for the process witness")
    for name in ("cargo", "rustc", "python3"):
        require(shutil.which(name) is not None, f"Required local tool missing: {name}")
    lab = Path(tempfile.mkdtemp(prefix="delivery-config-demo-", dir=parent))
    project = lab / "project"
    try:
        shutil.copytree(REPO / "tests/fixtures/delivery-config", project)
        task = lab / "task.txt"
        task.write_text("Fixture only: deliver item-count; preserve failure, rework and exact artifact evidence.\n")

        def prepare(previous: str | None = None) -> dict[str, Any]:
            return protocol.prepare(argparse.Namespace(
                cwd=None if previous else str(project), parent_depth=None if previous else 0,
                max_depth=None, previous=previous, task_file=str(task), root=str(lab)))

        def publish(info: dict[str, Any]) -> None:
            request = protocol.load_request(info["request_path"])
            result = {key: request[key] for key in protocol.IDENTITY_FIELDS}
            result.update({key: [] for key in protocol.FILE_FIELDS})
            result.update(status="success", output="Synthetic author handoff; product acceptance is separate.",
                          error=None, blocked_reason=None, completed_at=protocol.utc_now(),
                          files_modified=["src/main.rs"])
            protocol.publish(info["result_path"], result)

        info = prepare()
        index = lab / "jobs"
        state = jobs.register(index, info["request_path"])
        state = jobs.claim(index, info["job_id"], state["revision"], "fixture-controller", 300)
        token = state["lease"]["token"]

        def change(action: str, payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal state
            state = jobs.change(index, info["job_id"], state["revision"], token, action, payload)
            return state

        publish(info)
        original_result = Path(info["result_path"])
        original_result_hash = digest(original_result)
        bad = delivery.capture(info["request_path"], ["."], [], "Single fixture writer yielded", lab)
        plan_path = REPO / "skills/agent-orchestrator/assets/verification/rust-cli-plan.json"
        shipped = json.loads(plan_path.read_text())
        delivery.validate_plan(shipped)
        # Same matrix as the shipped example, with a shorter diagnostic timeout.
        matrix = copy.deepcopy(shipped)
        release_test = next(c for c in matrix["commands"] if c["argv"][:2] == ["cargo", "test"]
                            and "--release" in c["argv"])
        release_test["timeout_seconds"] = 3
        for label, plan in (("shipped", shipped), ("matrix", matrix)):
            protocol.publish(lab / f"{label}-plan.json", plan)
        caller_plan = lab / "acceptance-plan.json"
        protocol.publish(caller_plan, {
            "delivery": {"target": "Native Linux item-count release executable",
                         "contract": "No arguments: 0\\n; alpha beta gamma: 3\\n; exit 0 and empty stderr",
                         "artifact": "build/cargo/release/item-count"},
            "configurations": ["development tests", "release tests/build/artifact smoke"],
            "settings": shipped["environment"],
            "required_gates": [{"execution_plan": "fixed-matrix-plan.json",
                                "execution_plan_source_sha256": digest(plan_path),
                                "command_positions": list(range(1, len(shipped["commands"]) + 1)),
                                "owner": "fixture-verifier"},
                               {"execution_plan": "fixed-quality-plan.json", "command_positions": [1, 2],
                                "owner": "fixture-verifier"}],
            "controller_gate": "Reconcile all required command results and snapshot/artifact evidence",
            "not_applicable": {"features": "Manifest defines none",
                               "other_platforms_and_msrv": "Demo promises only installed native toolchain on Linux"},
            "exclusions": [], "scope_owner": "fixture-controller",
            "diagnostics": "Debug-only pass is intentionally incomplete; bad-matrix and bad-artifact preserve rejections",
        })

        def verify(label: str, snapshot: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
            protocol.publish(lab / f"{label}-plan.json", plan)
            result = delivery.verify(snapshot["snapshot_path"], plan, lab)
            protocol.publish(lab / f"{label}-result.json", result)
            protocol.publish(lab / f"{label}-coverage.json", coverage(plan, result))
            return result

        debug_plan = copy.deepcopy(shipped)
        debug_plan["commands"] = [c for c in debug_plan["commands"]
                                  if c["argv"][:2] == ["cargo", "test"] and "--release" not in c["argv"]]
        debug_plan["artifacts"] = []
        debug = verify("debug-only", bad, debug_plan)
        require(debug["passed"], "Negative control must first pass debug tests")
        rejected = verify("bad-matrix", bad, matrix)
        require(not rejected["passed"] and rejected["commands"][-1]["outcome"] == "timeout",
                "Release tests must time out on the broken snapshot")
        pid_file = Path(rejected["attempt_path"]).parent / "build/test.pid"
        test_pid = int(pid_file.read_text())
        proc = Path("/proc") / str(test_pid) / "stat"
        process_state = proc.read_text().rsplit(")", 1)[1].split()[0] if proc.exists() else None
        require(process_state in (None, "Z"), "Owned release test still running after timeout")
        recorded_stat = (pid_file.parent / "test.stat").read_text().rsplit(")", 1)[1].split()
        process_group = int(recorded_stat[2])
        group_members = active_group_members(process_group)
        require(not group_members, "Owned process group still active after timeout")

        # The stop-on-failure matrix never reaches smoke. Exercise that oracle
        # separately against the same bad source, in a fresh build/evidence tree.
        smoke_plan = copy.deepcopy(shipped)
        smoke_plan["commands"] = [c for c in smoke_plan["commands"] if c["argv"][:2] != ["cargo", "test"]]
        smoke = verify("bad-artifact", bad, smoke_plan)
        last = smoke["commands"][-1]
        require(not smoke["passed"] and last["outcome"] == "exited" and last["exit_code"] != 0
                and "TimeoutExpired" in Path(last["stderr"]["path"]).read_text(),
                "Independent artifact oracle must reject the hanging binary")

        failed_path = Path(rejected["attempt_path"]).parent / "result.json"
        failed_hash = digest(failed_path)
        acceptance = {"kind": "delivery", "verdict": "accepted",
                      "attempt_path": rejected["attempt_path"], "evidence_path": str(failed_path),
                      "note": "Fixture regression: accepting this failed delivery must be rejected"}
        try:
            change("accept", acceptance)
        except ValueError as exc:
            acceptance_error = str(exc)
            require("passed" in acceptance_error, acceptance_error)
        else:
            raise RuntimeError("Failed delivery was accepted")
        change("accept", {**acceptance, "verdict": "rejected", "note": "Release gate timed out"})
        before_rework = jobs.recover(index, info["job_id"])["completion"]
        require(not before_rework["accepted_by_verifier"], "Rejected matrix counted as accepted")
        try:
            change("close", {"outcome": "completed", "evidence_path": str(failed_path),
                             "note": "Fixture regression: rejected product cannot complete"})
        except ValueError as exc:
            close_error = str(exc)
            require("verifier rejected" in close_error, close_error)
        else:
            raise RuntimeError("Rejected product was marked completed")

        next_info = prepare(info["request_path"])
        change("activate", {"request_path": next_info["request_path"]})
        main = project / "src/main.rs"
        broken = "        debug_assert!(items.pop().is_some());"
        fixed = "        let popped = items.pop();\n        debug_assert!(popped.is_some());"
        require(main.read_text().count(broken) == 1, "Unexpected negative fixture source")
        main.write_text(main.read_text().replace(broken, fixed))
        publish(next_info)
        corrected = delivery.capture(next_info["request_path"], ["."], [],
                                     "Fixture correction yielded in fresh round", lab,
                                     baseline=bad["snapshot_path"])
        accepted = verify("fixed-matrix", corrected, shipped)
        require(accepted["passed"], "Corrected fixture failed the unmodified published plan")
        lint_plan = {**copy.deepcopy(shipped), "commands": [
            {"argv": ["cargo", "fmt", "--check"], "timeout_seconds": 30},
            {"argv": ["cargo", "clippy", "--locked", "--offline", "--all-targets", "--all-features", "--", "-D", "warnings"],
             "timeout_seconds": 60}], "artifacts": []}
        quality = verify("fixed-quality", corrected, lint_plan)
        require(quality["passed"], "Corrected fixture fmt/clippy failed")
        proof = lab / "accepted-evidence.json"
        protocol.publish(proof, {"fixture_only": True, "caller_scope": "Native Linux; no features; debug/release and binary smoke",
                                "scope_exclusions": "No other platform/MSRV promised by this demo",
                                "matrix_attempt": accepted["attempt_path"], "quality_attempt": quality["attempt_path"],
                                "caller_plan_path": str(caller_plan), "caller_plan_sha256": digest(caller_plan),
                                "execution_plan_sha256": digest(lab / "fixed-matrix-plan.json")})
        change("accept", {**acceptance, "attempt_path": accepted["attempt_path"],
                          "evidence_path": str(proof), "note": "Declared native fixture coverage passed"})
        after_rework = jobs.recover(index, info["job_id"])["completion"]
        require(after_rework["accepted_by_verifier"], "Fixed delivery not accepted")
        require(not after_rework["host_settled"] and not after_rework["ready_to_complete"],
                "No native host observation was made in this demo")
        require(digest(failed_path) == failed_hash and digest(original_result) == original_result_hash,
                "Rework changed old result/failure bytes")
        for result in (debug, rejected, smoke, accepted, quality):
            checked = delivery.inspect(result["attempt_path"])
            require(checked["evidence_valid"] and checked["passed"] == result["passed"],
                    "Attempt evidence changed")
        require(delivery.check(bad["snapshot_path"])["valid"], "Original source snapshot changed")
        change("release", {})
        report = {"root": str(lab), "native_models_launched": 0, "synthetic_author_results": True,
                  "debug_only_passed": debug["passed"], "rejected_attempt": rejected["attempt_path"],
                  "release_timeout": {"exit_code": rejected["commands"][-1]["exit_code"],
                                      "test_pid": test_pid, "process_state_after": process_state,
                                      "process_group": process_group, "active_group_members": group_members},
                  "matrix_smoke_not_run": True, "separate_bad_artifact_attempt": smoke["attempt_path"],
                  "acceptance_error": acceptance_error, "close_error": close_error,
                  "accepted_attempt": accepted["attempt_path"], "quality_attempt": quality["attempt_path"],
                  "artifacts": accepted["artifacts"], "old_evidence_unchanged": True,
                  "followup_same_job": next_info["job_id"] == info["job_id"],
                  "fresh_round": next_info["round_id"] != info["round_id"],
                  "completion": after_rework,
                  "meaning": "Declared configuration coverage exercised; no automatic plan completeness or native lifecycle proof"}
        protocol.publish(lab / "report.json", report)
        return report
    except BaseException as exc:
        protocol.publish(lab / "failure.json", {"error": str(exc), "type": type(exc).__name__})
        raise


def main() -> None:
    """Run one retained demonstration under an existing evidence parent."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="existing parent for retained evidence")
    args = parser.parse_args()
    print(json.dumps(run_demo(args.root.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
