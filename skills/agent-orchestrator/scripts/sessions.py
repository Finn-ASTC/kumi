#!/usr/bin/env python3
"""Read-only project session directory derived from one registered kumi run."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

import capabilities
import completion
import jobs
import protocol
import registry
import runs

MAX_ROWS = 1000
TITLE_LIMIT = 160
SESSION_ID_LIMIT = 256
require = protocol.require


def _text(value: Any, label: str, limit: int) -> str | None:
    if value is None:
        return None
    require(isinstance(value, str) and len(value) <= limit and value.isprintable(),
            "invalid " + label)
    return value


def _session_row(run: dict[str, Any], state: dict[str, Any],
                 registration: dict[str, Any] | None = None,
                 capability_records: list[dict[str, Any]] | None = None,
                 *, ambiguous: bool = False, now: float | None = None) -> dict[str, Any]:
    request = protocol.load_request(state["active_request"])
    launch = state.get("launch")
    native = state.get("native")
    require(isinstance(launch, (dict, type(None))) and isinstance(native, dict),
            "invalid job session metadata")

    executable = profile = None
    if launch is not None:
        argv = launch.get("argv")
        require(isinstance(argv, list) and bool(argv)
                and all(isinstance(value, str) for value in argv), "invalid launch metadata")
        executable = _text(argv[0], "launch executable", 256)
        profile = _text(launch.get("profile"), "launch profile", 128)

    session_id = _text(native.get("session_id"), "native session ID", SESSION_ID_LIMIT)
    request_path = Path(state["active_request"])
    resource_path = request_path.parent / "resources.json"
    resources = None
    if os.path.lexists(resource_path):
        record = protocol.validate_resources(protocol.read_json(resource_path), request)
        resources = {
            "mode": record["mode"], "session": record["session"], "agent": record["agent"],
            "workspace": record.get("workspace"), "tab": record.get("tab"),
            "pane": record["pane"], "owns_session": record["owns_session"],
            "owns_workspace": record["owns_workspace"], "owns_tab": record.get("owns_tab", False),
            "owns_pane": record["owns_pane"],
        }

    title_value = next((line.strip() for line in request["task"].splitlines() if line.strip()), "(untitled)")
    title = _text(title_value[:TITLE_LIMIT], "task title", TITLE_LIMIT)
    closed = state.get("closed")
    result = jobs.result_view(state)
    status = "closed" if closed is not None else result["status"]
    registered_host = registration.get("host") if registration else None
    registered_store = registration.get("store") if registration else None
    registered_profile = registration.get("profile") if registration else profile
    capability_map = capabilities.by_host_profile(
        capability_records or [], registered_host, registered_profile)
    identity_conflict = bool(registration and (registration.get("session_id") != session_id
                             or profile is not None and registration.get("profile") != profile))
    native_state = ("ambiguous_registration" if ambiguous else
                    "identity_conflict" if identity_conflict else
                    "unbound" if session_id is None else
                    "bound" if registered_host and registered_store else "host_unknown")
    archive_reasons = []
    if closed is None:
        archive_reasons.append("job_not_closed")
    if state.get("submission") == "uncertain":
        archive_reasons.append("submission_uncertain")
    control = state.get("control")
    if control and (control.get("status") == "uncertain"
                    or control.get("status") == "confirmed"
                    and control.get("kind") in ("cancel", "interrupt")):
        archive_reasons.append("control_requires_reconciliation")
    if session_id is None or not (registered_host and registered_store):
        archive_reasons.append("native_session_unbound")
    if identity_conflict:
        archive_reasons.append("native_identity_conflict")
    if ambiguous:
        archive_reasons.append("ambiguous_registration")
    archive_status = capability_map.get("archive", {}).get("effective_status", "unknown")
    resume_status = capability_map.get("resume", {}).get("effective_status", "unknown")
    if archive_status != "verified":
        archive_reasons.append("native_archive_capability_unverified")
    recovery_blockers: list[str] = []
    if closed is not None:
        recovery_blockers.append("job_closed")
    if native_state != "bound":
        recovery_blockers.append("native_identity_unavailable")
    if ambiguous:
        recovery_blockers.append("ambiguous_registration")
    if resume_status != "verified":
        recovery_blockers.append("native_resume_capability_unverified")
    if state.get("submission") == "uncertain":
        recovery_blockers.append("submission_uncertain")
    if "control_requires_reconciliation" in archive_reasons:
        recovery_blockers.append("control_requires_reconciliation")
    current = jobs.clock(now)
    if not completion.host_summary(state, result, current)["settled"]:
        archive_reasons.append("host_not_proven_settled")
        recovery_blockers.append("host_not_proven_settled")
    lease = state.get("lease")
    if lease is None or lease.get("scope") == "acceptance" or lease["expires_at"] <= current:
        recovery_blockers.append("live_control_lease_missing")
    else:
        # A read-only caller has supplied no fencing token and holds no input lock.
        recovery_blockers.append("input_ownership_not_verified")
        archive_reasons.append("live_control_lease_present")
    shared = ["capability_applicability_not_verified", "message_responsibility_not_integrated",
              "handoff_responsibility_not_integrated"]
    recovery_blockers.extend(shared + ["native_resume_adapter_unavailable"])
    archive_reasons.extend(shared + ["native_archive_adapter_unavailable",
                                    "descendant_scope_not_verified", "evidence_retention_not_verified"])
    if registration and registration["kind"] == "external":
        group = "external"
        archive_reasons.append("external_user_owned_session")
        recovery_blockers.append("external_user_owned_session")
    elif registration and registration.get("role") == "coordinator":
        group = "coordinator"
    else:
        group = "worker"
    return {
        "group": group,
        "role": registration.get("role", "unknown") if registration else "unknown",
        "parent_job_id": None,
        "parent_registration_id": registration.get("parent_registration_id") if registration else None,
        "relationship": registration.get("relationship") if registration else None,
        "relationship_status": "recorded" if registration else "not_recorded",
        "run_id": run["run_id"], "job_id": state["job_id"], "round_id": state["round_id"],
        "round_count": len(state["rounds"]), "task_title": title,
        "task_title_truncated": len(title_value) > TITLE_LIMIT,
        "project_cwd": request["cwd"], "status": status, "submission": state["submission"],
        "closed": ({key: closed.get(key) for key in ("outcome", "recorded_at")}
                   if closed is not None else None),
        "native_identity": {
            "host": registered_host, "store": registered_store,
            "profile": registered_profile, "session_id": session_id,
            "status": native_state, "launcher": executable,
        },
        "resources": resources,
        "request_path": str(request_path),
        "recovery_preview": {
            "available": False, "candidate": (session_id is not None and not identity_conflict
                                               and not ambiguous and closed is None),
            "executes_commands": False,
            "reason": "verified host/store adapter and current input ownership are not available",
            "dry_run": {"eligible": False, "blockers": recovery_blockers},
            "capability_status": resume_status,
        },
        "archive_preview": {"eligible": False, "blockers": archive_reasons,
                            "executes_commands": False,
                            "capability_status": archive_status},
    }


def directory(run_path: str, *, job_id: str | None = None, limit: int = 100,
              offset: int = 0) -> dict[str, Any]:
    """List indexed worker jobs in one run; never scan native histories or write state."""
    require(type(limit) is int and 1 <= limit <= MAX_ROWS and type(offset) is int and offset >= 0,
            "invalid session directory page")
    if job_id is not None:
        require(isinstance(job_id, str) and jobs.ID.fullmatch(job_id), "invalid job_id")
    run = runs.load_run(run_path)
    records = registry.list_records(run_path)
    capability_records = capabilities.list_records(run_path)
    by_job_round: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        if record["job_id"] is not None:
            by_job_round.setdefault((record["job_id"], record["round_id"]), []).append(record)
    by_id = {record["registration_id"]: record for record in records}
    index = Path(run["index_path"])
    job_dirs = sorted(path for path in index.iterdir()
                      if re.fullmatch(r"[0-9a-f]{32}", path.name)
                      and (path.is_dir() or path.is_symlink()))
    if job_id is not None:
        job_dirs = [path for path in job_dirs if path.name == job_id]
    selected = job_dirs[offset:offset + limit]
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in selected:
        if path.is_symlink():
            errors.append({"job_id": path.name, "code": "job_directory_redirected"})
            continue
        try:
            state = jobs.load(index, path.name)
            matches = by_job_round.get((state["job_id"], state["round_id"]), [])
            registration = matches[0] if len(matches) == 1 else None
            row = _session_row(run, state, registration, capability_records,
                               ambiguous=len(matches) > 1)
            if registration and registration["parent_registration_id"] is not None:
                row["parent_job_id"] = by_id[registration["parent_registration_id"]]["job_id"]
            rows.append(row)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            errors.append({"job_id": path.name, "code": "job_session_unreadable"})

    next_offset = offset + len(selected)
    project_roots = sorted({row["project_cwd"] for row in rows})
    digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                      allow_nan=False).encode("utf-8")).hexdigest()
    return {
        "version": 1, "mode": "read_only_session_directory",
        "run": {"run_id": run["run_id"], "run_path": run["run_path"]},
        "projects": [{"cwd": root, "identity": "run-scoped; path is display metadata"}
                     for root in project_roots],
        "sessions": rows,
        "registrations": [
            {**{key: record.get(key) for key in (
                "registration_id", "kind", "role", "host", "store", "profile", "session_id",
                "job_id", "round_id", "parent_registration_id", "relationship", "label")},
             "control_granted": False}
            for record in records
        ],
        "session_registry": {"version": 1, "count": len(records),
                             "digest": registry.digest(records)},
        "capability_registry": {"version": 1, "count": len(capability_records),
                                 "records": [{key: record.get(key) for key in (
                                     "record_id", "host", "profile", "capability", "status",
                                     "effective_status", "evidence_status", "host_version", "observed_at")}
                                             for record in capability_records],
                                 "scope": "caller-reported evidence; runtime applicability unverified"},
        "counts": {"indexed_jobs": len(job_dirs), "returned_sessions": len(rows),
                   "errors": len(errors)},
        "errors": errors, "offset": offset, "next_offset": next_offset,
        "has_more": len(job_dirs) > next_offset, "digest": digest,
        "scope": "registered jobs in this run only; no global HOME or native transcript scan",
        "executes_commands": False, "writes_host": False,
        "host_capabilities": [_capability_summary(host, profile, capability_records)
                              for host, profile in (("codex", "default"), ("omp", "default"),
                                                    ("hermes", "default"), ("opencode", "omo"),
                                                    ("opencode", "pure"))],
        "archive": {"supported": False, "mode": "dry_run_only"},
        "recovery": {"supported": False, "mode": "preview_only"},
        "note": "Main coordinator sessions and parent-child links are shown only when explicitly registered; none are inferred.",
    }


def _capability_summary(host: str, profile: str,
                        records: list[dict[str, Any]]) -> dict[str, Any]:
    values = capabilities.by_host_profile(records, host, profile)
    return {
        "host": host, "profile": profile,
        **{name: values.get(name, {}).get("effective_status", "unknown")
           for name in capabilities.CAPABILITIES},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--job")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    try:
        value = directory(args.run, job_id=args.job, limit=args.limit, offset=args.offset)
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"error": "session directory failed; inspect the run privately"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
