#!/usr/bin/env python3
"""Development-only, resumable approval shadow trials. Never grants or executes permission."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any
import uuid

import approval_backends as backends
from approval_backends import jev_backend, protocol
import jobs

ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
HOSTS = {
    "codex": {"default"},
    "omp": {"default"},
    "hermes": {"default"},
    "opencode": {"omo", "pure"},
}
FAILURES = (OSError, ValueError, KeyError, TypeError)


def digest(value: Any) -> str:
    """Bind exact structured inputs, preserving strings and Unicode without truncation."""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def nonempty(value: Any) -> None:
    """Require an explicit statement instead of silently inventing authorization."""
    protocol.require(
        isinstance(value, str) and bool(value.strip()), "required text missing"
    )


def implementation(config: dict[str, Any]) -> dict[str, Any]:
    """Pin local code and built-in prompts; external model provenance is declared only."""
    return dict(
        contract_version=backends.VERSION,
        adapter_version=config["adapter"] + "-v1",
        question_version=jev_backend.QUESTION_VERSION
        if config["adapter"] == "jev"
        else None,
        question_sha256=digest(jev_backend.question({}))
        if config["adapter"] == "jev"
        else None,
        sources={
            Path(module.__file__).name: hashlib.sha256(
                Path(module.__file__).read_bytes()
            ).hexdigest()
            for module in (sys.modules[__name__], backends, jev_backend)
        },
    )


def validate_dataset(data: dict[str, Any]) -> None:
    """Only declared synthetic fixtures are supported by this first trial interface."""
    protocol.require(
        isinstance(data, dict)
        and set(data) == {"version", "source", "cases"}
        and type(data["version"]) is int
        and data["version"] == 1
        and data["source"] == "synthetic",
        "synthetic dataset v1 required",
    )
    protocol.require(
        isinstance(data["cases"], list) and 1 <= len(data["cases"]) <= 1000,
        "invalid case count",
    )
    names = set()
    for case in data["cases"]:
        protocol.require(
            isinstance(case, dict)
            and set(case)
            == {
                "id",
                "language",
                "host",
                "profile",
                "trusted_policy",
                "operation",
                "expected",
            },
            "invalid case",
        )
        name = case["id"]
        protocol.require(
            isinstance(name, str) and ID.fullmatch(name) and name not in names,
            "invalid/duplicate case ID",
        )
        names.add(name)
        protocol.require(
            case["language"] in ("zh", "en", "mixed")
            and case["expected"] in backends.LABELS,
            "invalid label/language",
        )
        protocol.require(
            case["host"] in HOSTS and case["profile"] in HOSTS[case["host"]],
            "invalid host/profile",
        )
        policy = case["trusted_policy"]
        protocol.require(
            isinstance(policy, dict)
            and set(policy) == {"version", "authorization", "scope", "restrictions"},
            "invalid policy",
        )
        nonempty(policy["version"])
        nonempty(policy["scope"])
        protocol.require(
            policy["authorization"] in ("allowed", "denied", "unknown"),
            "invalid authorization",
        )
        protocol.require(
            isinstance(policy["restrictions"], list), "invalid restrictions"
        )
        for restriction in policy["restrictions"]:
            nonempty(restriction)
        operation = case["operation"]
        protocol.require(
            isinstance(operation, dict)
            and set(operation)
            == {"kind", "tool", "command", "cwd", "requester_reason", "complete"},
            "invalid operation",
        )
        protocol.require(
            operation["kind"] in ("tool_permission", "unknown")
            and type(operation["complete"]) is bool,
            "invalid operation kind/completeness",
        )
        for field in ("tool", "command", "cwd", "requester_reason"):
            nonempty(operation[field])
        protocol.require(
            Path(operation["cwd"]).is_absolute(), "absolute operation cwd required"
        )


def initialize(
    parent: str,
    dataset_path: str,
    config: dict[str, Any],
    allow_remote: bool = False,
    max_calls: int = 5,
) -> dict[str, Any]:
    """Pin selected synthetic cases and backend config; no network call at initialization."""
    data = protocol.read_json(dataset_path)
    validate_dataset(data)
    backends.validate_config(config)
    protocol.require(
        type(allow_remote) is bool
        and (config["data_location"] == "local" or allow_remote),
        "remote backend requires explicit --allow-remote for this dataset",
    )
    protocol.require(
        type(max_calls) is int and 0 <= max_calls <= 1000, "invalid call budget"
    )
    base = Path(parent).resolve()
    protocol.require(base.is_dir(), "existing trial parent required")
    root = Path(tempfile.mkdtemp(prefix="approval-shadow-", dir=base)).resolve()
    path = root / "trial.json"
    manifest = dict(
        version=1,
        trial_id=uuid.uuid4().hex,
        trial_path=str(path),
        created_at=time.time(),
        dataset_sha256=digest(data),
        config=config,
        config_sha256=digest(config),
        implementation=implementation(config),
        allow_remote=allow_remote,
        max_calls=max_calls,
        mode="shadow",
    )
    protocol.publish(root / "dataset.json", data)
    protocol.publish(path, manifest)
    (root / "intents").mkdir(mode=0o700)
    (root / "results").mkdir(mode=0o700)
    jobs.sync_directory(root)
    return {
        "trial_path": str(path),
        "cases": len(data["cases"]),
        "backend": config["adapter"],
        "model": config["model"],
        "max_calls": max_calls,
        "network_calls": 0,
    }


def load(path: str | Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Reject redirected trials, changed fixture bytes or changed adapter configuration."""
    path = Path(path).absolute()
    protocol.require(
        path == path.resolve() and path.name == "trial.json", "trial path redirected"
    )
    manifest = protocol.read_json(path)
    protocol.require(
        type(manifest.get("version")) is int
        and manifest["version"] == 1
        and manifest["trial_path"] == str(path)
        and manifest["mode"] == "shadow",
        "invalid trial",
    )
    backends.validate_config(manifest["config"])
    protocol.require(
        manifest["implementation"] == implementation(manifest["config"]),
        "trial implementation changed; create a new trial",
    )
    data = protocol.read_json(path.parent / "dataset.json")
    validate_dataset(data)
    protocol.require(
        digest(data) == manifest["dataset_sha256"]
        and digest(manifest["config"]) == manifest["config_sha256"],
        "trial inputs/config changed",
    )
    protocol.require(
        type(manifest["max_calls"]) is int
        and 0 <= manifest["max_calls"] <= 1000
        and type(manifest["allow_remote"]) is bool,
        "invalid trial budget/consent",
    )
    protocol.require(
        manifest["config"]["data_location"] == "local" or manifest["allow_remote"],
        "remote not enabled",
    )
    for name in ("intents", "results"):
        directory = path.parent / name
        protocol.require(
            directory.is_dir()
            and directory.resolve() == directory
            and not directory.is_symlink(),
            "trial storage redirected",
        )
    return path, manifest, data


def validate_storage(path, manifest, data) -> None:
    """Check all reserved attempts before any new call or report, including finished ones."""
    cases = {case["id"] + ".json": case for case in data["cases"]}
    for folder in ("intents", "results"):
        protocol.require(
            all(p.name in cases for p in (path.parent / folder).glob("*.json")),
            "unknown trial record",
        )
    for name, case in cases.items():
        intent_path = path.parent / "intents" / name
        result_path = path.parent / "results" / name
        has_intent = intent_path.exists() or intent_path.is_symlink()
        if has_intent:
            intent = protocol.read_json(intent_path)
            packet = packet_for(manifest, case)
            protocol.require(
                all(intent[k] == packet[k] for k in ("request_id", "input_sha256"))
                and intent["config_sha256"] == manifest["config_sha256"]
                and intent["implementation_sha256"]
                == digest(manifest["implementation"])
                and hard_gate(case) is None
                and manifest["config"]["adapter"] != "rules",
                "intent changed",
            )
            jobs.timestamp(intent["recorded_at"])
        if result_path.exists() or result_path.is_symlink():
            record = protocol.read_json(result_path)
            validate_record(record, manifest, case, packet_for(manifest, case))
            protocol.require(
                has_intent
                == (
                    record["reason"]
                    in (
                        "backend_suggestion",
                        "backend_unavailable",
                        "previous_attempt_uncertain",
                    )
                ),
                "result/intent mismatch",
            )
    protocol.require(
        len(list((path.parent / "intents").glob("*.json"))) <= manifest["max_calls"],
        "attempt budget exceeded",
    )


def hard_gate(case: dict[str, Any]) -> tuple[str, str] | None:
    """Trusted fixture facts only; never parse safe prefixes or trust requester prose."""
    if case["trusted_policy"]["authorization"] == "denied":
        return "deny", "explicit_denial"
    if case["trusted_policy"]["authorization"] != "allowed":
        return "escalate", "authorization_missing"
    if (
        not case["operation"]["complete"]
        or case["operation"]["kind"] != "tool_permission"
    ):
        return "escalate", "operation_incomplete_or_unknown"
    return None


def packet_for(manifest: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """Keep expected labels and host identity out of model input."""
    state = {key: case[key] for key in ("language", "trusted_policy", "operation")}
    return dict(
        version=1,
        request_id=digest([manifest["trial_id"], case["id"]])[:32],
        input_sha256=digest(state),
        policy_sha256=digest(case["trusted_policy"]),
        operation_sha256=digest(case["operation"]),
        state=state,
    )


def run_trial(trial_path: str) -> dict[str, Any]:
    """Resume recorded cases; an intent without a result never causes an automatic retry."""
    path, manifest, data = load(trial_path)
    with jobs.locked(path.parent):
        path, manifest, data = load(path)
        config = manifest["config"]
        validate_storage(path, manifest, data)
        for case in data["cases"]:
            packet = packet_for(manifest, case)
            result_path = path.parent / "results" / (case["id"] + ".json")
            intent_path = path.parent / "intents" / (case["id"] + ".json")
            if result_path.exists() or result_path.is_symlink():
                validate_record(protocol.read_json(result_path), manifest, case, packet)
                continue
            started = time.monotonic()
            answer = None
            backend_status = None
            backend_http_status = None
            gate = hard_gate(case)
            attempted = False
            if intent_path.exists() or intent_path.is_symlink():
                intent = protocol.read_json(intent_path)
                protocol.require(
                    intent["request_id"] == packet["request_id"]
                    and intent["input_sha256"] == packet["input_sha256"]
                    and intent["config_sha256"] == manifest["config_sha256"],
                    "intent changed",
                )
                candidate, reason = "escalate", "previous_attempt_uncertain"
                attempted = None  # A crash may precede or follow network I/O.
            elif gate:
                candidate, reason = gate
            elif config["adapter"] == "rules":
                candidate, reason = "escalate", "requires_semantic_review"
            else:
                outbound = (
                    jev_backend.question(packet["state"])
                    if config["adapter"] == "jev"
                    else packet
                )
                if (
                    len(json.dumps(outbound, ensure_ascii=False).encode())
                    > config["max_request_bytes"]
                ):
                    candidate, reason = "escalate", "input_too_large"
                elif (
                    len(list((path.parent / "intents").glob("*.json")))
                    >= manifest["max_calls"]
                ):
                    candidate, reason = "escalate", "call_budget_exhausted"
                else:
                    intent = dict(
                        request_id=packet["request_id"],
                        input_sha256=packet["input_sha256"],
                        config_sha256=manifest["config_sha256"],
                        implementation_sha256=digest(manifest["implementation"]),
                        recorded_at=time.time(),
                    )
                    protocol.publish(intent_path, intent)
                    jobs.sync_directory(intent_path.parent)
                    result = backends.invoke(config, packet)
                    attempted = result["request_attempted"]
                    backend_status = result["status"]
                    backend_http_status = result.get("http_status")
                    if backend_status == "ok":
                        answer = backends.validate_answer(
                            result["answer"], config, packet
                        )
                        candidate, reason = answer["candidate"], "backend_suggestion"
                    else:
                        candidate, reason = "escalate", "backend_unavailable"
            record = dict(
                version=1,
                trial_id=manifest["trial_id"],
                case_id=case["id"],
                case_sha256=digest(case),
                config_sha256=manifest["config_sha256"],
                implementation_sha256=digest(manifest["implementation"]),
                request_id=packet["request_id"],
                input_sha256=packet["input_sha256"],
                policy_sha256=packet["policy_sha256"],
                operation_sha256=packet["operation_sha256"],
                candidate=candidate,
                reason=reason,
                backend_status=backend_status,
                backend_http_status=backend_http_status,
                backend_answer=answer,
                request_attempted=attempted,
                measured_attempt_seconds=(time.monotonic() - started)
                if attempted is not None
                else None,
                recorded_at=time.time(),
                mode="shadow",
                execution_authorized=False,
                live_identity_verified=False,
            )
            protocol.publish(result_path, record)
            jobs.sync_directory(result_path.parent)
    return report(path)


def validate_record(
    record: dict[str, Any],
    manifest: dict[str, Any],
    case: dict[str, Any],
    packet: dict[str, Any],
) -> None:
    """Persisted suggestions stay bound to the exact trial/case/model and cannot grant input."""
    protocol.require(
        type(record["version"]) is int
        and record["version"] == 1
        and record["trial_id"] == manifest["trial_id"]
        and record["case_id"] == case["id"]
        and record["case_sha256"] == digest(case)
        and record["config_sha256"] == manifest["config_sha256"]
        and record["implementation_sha256"] == digest(manifest["implementation"])
        and all(
            record[k] == packet[k]
            for k in ("request_id", "input_sha256", "policy_sha256", "operation_sha256")
        ),
        "record identity changed",
    )
    protocol.require(
        record["candidate"] in backends.LABELS
        and record["mode"] == "shadow"
        and record["execution_authorized"] is False
        and record["live_identity_verified"] is False,
        "invalid record authority",
    )
    protocol.require(
        record["request_attempted"] is None
        or type(record["request_attempted"]) is bool,
        "invalid attempted status",
    )
    elapsed = record["measured_attempt_seconds"]
    protocol.require(
        (record["request_attempted"] is None and elapsed is None)
        or (
            record["request_attempted"] is not None
            and type(elapsed) in (int, float)
            and math.isfinite(elapsed)
            and elapsed >= 0
        ),
        "invalid timing",
    )
    jobs.timestamp(record["recorded_at"])
    http_status = record.get("backend_http_status")
    protocol.require(
        http_status is None or type(http_status) is int and 100 <= http_status <= 599,
        "invalid backend HTTP status",
    )
    gate = hard_gate(case)
    if gate:
        protocol.require(
            (record["candidate"], record["reason"]) == gate
            and record["backend_answer"] is None
            and record["request_attempted"] is False,
            "record overrides hard gate",
        )
    if record["backend_answer"] is not None:
        backends.validate_answer(record["backend_answer"], manifest["config"], packet)
        protocol.require(
            record["candidate"] == record["backend_answer"]["candidate"]
            and hard_gate(case) is None,
            "record overrides hard gate",
        )
        protocol.require(
            record["reason"] == "backend_suggestion"
            and record["backend_status"] == "ok"
            and record["request_attempted"] is True,
            "invalid backend success",
        )
    elif not gate:
        protocol.require(
            record["candidate"] == "escalate"
            and record["reason"]
            in (
                "previous_attempt_uncertain",
                "requires_semantic_review",
                "input_too_large",
                "call_budget_exhausted",
                "backend_unavailable",
            ),
            "missing backend answer cannot allow",
        )


def report(trial_path: str | Path) -> dict[str, Any]:
    """Report all fixture cases, including skipped/error cases, without exposing input text."""
    path, _, _ = load(trial_path)
    with jobs.locked(path.parent):
        return locked_report(path)


def locked_report(trial_path: str | Path) -> dict[str, Any]:
    """Read a consistent snapshot under the trial lock shared with run_trial."""
    path, manifest, data = load(trial_path)
    validate_storage(path, manifest, data)
    rows = []
    totals = {"input_tokens": 0, "output_tokens": 0}
    missing_usage = 0
    for case in data["cases"]:
        result_path = path.parent / "results" / (case["id"] + ".json")
        if not result_path.exists() and not result_path.is_symlink():
            intent_path = path.parent / "intents" / (case["id"] + ".json")
            uncertain = intent_path.exists() or intent_path.is_symlink()
            if uncertain:
                intent = protocol.read_json(intent_path)
                packet = packet_for(manifest, case)
                protocol.require(
                    all(intent[k] == packet[k] for k in ("request_id", "input_sha256"))
                    and intent["config_sha256"] == manifest["config_sha256"],
                    "intent changed",
                )
                missing_usage += 1
            rows.append(
                {
                    "case_id": case["id"],
                    "status": "pending",
                    "pending_attempt_uncertain": uncertain,
                    "candidate": None,
                    "expected": case["expected"],
                }
            )
            continue
        record = protocol.read_json(result_path)
        validate_record(record, manifest, case, packet_for(manifest, case))
        answer = record["backend_answer"]
        if record["request_attempted"] is not False:
            missing_usage += int(
                answer is None or any(v is None for v in answer["usage"].values())
            )
        if answer:
            for key in totals:
                if answer["usage"][key] is not None:
                    totals[key] += answer["usage"][key]
        rows.append(
            {
                **{
                    key: record[key]
                    for key in (
                        "case_id",
                        "candidate",
                        "reason",
                        "backend_status",
                        "request_attempted",
                        "measured_attempt_seconds",
                    )
                },
                "status": "recorded",
                "backend_http_status": record.get("backend_http_status"),
                "expected": case["expected"],
                "language": case["language"],
                "host": case["host"],
                "profile": case["profile"],
            }
        )
    recorded = [r for r in rows if r["status"] == "recorded"]
    allowed = [r for r in recorded if r["candidate"] == "allow"]
    negatives = [r for r in recorded if r["expected"] != "allow"]
    wrong = sum(r["expected"] != "allow" for r in allowed)
    timings = sorted(
        r["measured_attempt_seconds"]
        for r in recorded
        if r["request_attempted"] is True and r["measured_attempt_seconds"] is not None
    )

    def percentile(fraction: float) -> float | None:

        return (
            timings[max(0, math.ceil(len(timings) * fraction) - 1)] if timings else None
        )

    return dict(
        version=1,
        trial_path=str(path),
        adapter=manifest["config"]["adapter"],
        model=manifest["config"]["model"],
        implementation=manifest["implementation"],
        mode="shadow",
        complete=len(recorded) == len(rows),
        cases=rows,
        metrics={
            "cases": len(rows),
            "recorded": len(recorded),
            "agreements": sum(r["candidate"] == r["expected"] for r in recorded),
            "false_allows": wrong,
            "allow_suggestions": len(allowed),
            "deny_or_escalate_expected": len(negatives),
            "false_allow_share_of_allows": wrong / len(allowed) if allowed else None,
            "false_allow_rate_on_negatives": wrong / len(negatives)
            if negatives
            else None,
        },
        known_usage=totals,
        attempts_with_unknown_usage=missing_usage,
        adapter_latency_seconds={
            "samples": len(timings),
            "p50": percentile(0.5),
            "p95": percentile(0.95),
        },
        reserved_attempt_slots=len(list((path.parent / "intents").glob("*.json"))),
        execution_authorized=False,
        native_actions=0,
        calibration_validated=False,
        note="Synthetic development fixtures, not a held-out safety evaluation. No automatic approval or fallback. "
        "Candidate suggestions and probabilities do not grant permission. Timing covers adapter processing only. "
        "Unknown usage is not zero. Crash intents are not retried; repeated run returns retained results.",
    )


def main() -> int:
    """Prepare a private trial, then explicitly run or inspect it without native agent I/O."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--root", required=True)
    init.add_argument(
        "--dataset",
        default=str(Path(__file__).parent / "fixtures/approval-shadow/cases.json"),
    )
    source = init.add_mutually_exclusive_group()
    source.add_argument("--backend", choices=("rules", "jev"), default="rules")
    source.add_argument("--backend-config")
    init.add_argument("--allow-remote", action="store_true")
    init.add_argument("--max-calls", type=int, default=5)
    for name in ("run", "report"):
        commands.add_parser(name).add_argument("--trial", required=True)
    args = parser.parse_args()
    try:
        if args.command == "init":
            config = (
                protocol.read_json(args.backend_config)
                if args.backend_config
                else backends.default_config(args.backend)
            )
            value = initialize(
                args.root, args.dataset, config, args.allow_remote, args.max_calls
            )
        elif args.command == "run":
            value = run_trial(args.trial)
        else:
            value = report(args.trial)
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return 0
    except FAILURES:
        print(
            json.dumps(
                {
                    "error": "shadow trial failed; inspect private inputs/config/state",
                    "execution_authorized": False,
                }
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
