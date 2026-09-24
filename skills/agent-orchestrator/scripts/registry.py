#!/usr/bin/env python3
"""Explicit, run-scoped session identity and relationship records.

The registry is a kumi-owned sidecar.  It never edits a host's session store and
does not infer ownership from a cwd, title, or a native session identifier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

import jobs
import protocol
import runs


ID = re.compile(r"[0-9a-f]{32}")
REGISTRY_NAME = "session-registry"
ROLES = {"coordinator", "worker", "verifier", "user"}
KINDS = {"managed", "external"}
RELATIONSHIPS = {"root", "kumi_delegation", "native_child", "continuation", "handoff"}
MAX_TEXT = 256


def _root(run: dict[str, Any], *, create: bool = False) -> Path:
    path = Path(run["run_path"]).parent / REGISTRY_NAME
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if os.path.lexists(path):
        protocol.require(path.is_dir() and path.resolve() == path,
                         "session registry missing or redirected")
    return path


def _text(value: Any, label: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    protocol.require(isinstance(value, str) and 0 < len(value) <= MAX_TEXT
                    and value.isprintable() and "\x00" not in value, "invalid " + label)
    return value


def _record_path(root: Path, registration_id: str) -> Path:
    protocol.require(isinstance(registration_id, str) and ID.fullmatch(registration_id),
                     "invalid registration_id")
    return root / (registration_id + ".json")


def _validate(record: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    protocol.require(type(record.get("version")) is int and record["version"] == 1
                     and record.get("run_id") == run["run_id"]
                     and record.get("run_path") == run["run_path"], "invalid session registration")
    protocol.require(set(record) == {"version", "registration_id", "run_id", "run_path", "kind", "role",
                                    "host", "store", "profile", "session_id", "job_id", "round_id",
                                    "parent_registration_id", "relationship", "label", "registered_at"},
                     "invalid registration fields")
    _record_path(Path("."), record.get("registration_id"))
    protocol.require(record.get("kind") in KINDS and record.get("role") in ROLES,
                     "invalid session registration kind")
    for key in ("host", "store", "profile", "session_id"):
        _text(record.get(key), key, allow_none=True)
    _text(record.get("label"), "label", allow_none=True)
    if record.get("session_id") is not None:
        protocol.require(all(record.get(key) is not None for key in ("host", "store", "profile")),
                         "bound session requires host, store, and profile")
    for key in ("job_id", "round_id", "parent_registration_id"):
        value = record.get(key)
        protocol.require(value is None or isinstance(value, str) and ID.fullmatch(value),
                         "invalid registration identifier")
    protocol.require(record.get("relationship") in RELATIONSHIPS, "invalid session relationship")
    parent = record.get("parent_registration_id")
    if record["relationship"] == "root":
        protocol.require(parent is None, "root registration cannot have a parent")
    else:
        protocol.require(parent is not None, "child registration requires a parent")
    if record["kind"] == "external":
        protocol.require(record["role"] in ("user", "coordinator") and record["job_id"] is None
                         and record["round_id"] is None,
                         "external session must be an unowned user session")
    else:
        protocol.require(record["role"] != "user" and record["job_id"] is not None
                         and record["round_id"] is not None,
                         "managed session requires a worker job")
    return record


def _read(root: Path, registration_id: str, run: dict[str, Any]) -> dict[str, Any]:
    path = _record_path(root, registration_id)
    record = protocol.read_json(path)
    protocol.require(record.get("registration_id") == registration_id, "registration filename mismatch")
    return _validate(record, run)


def _load_all(root: Path, run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.json")):
        record = _read(root, path.stem, run)
        protocol.require(record["registration_id"] not in records, "duplicate session registration")
        records[path.stem] = record
    for record in records.values():
        _no_cycle(records, record)
    return records


def _check_job(run: dict[str, Any], job_id: str | None, round_id: str | None) -> None:
    if job_id is None:
        protocol.require(round_id is None, "round requires job")
        return
    protocol.require(round_id is not None, "managed registration requires round")
    index = Path(run["index_path"])
    path = index / job_id
    protocol.require(path.resolve() == path, "job directory redirected")
    state = jobs.load(index, job_id)
    protocol.require(state["closed"] is None, "closed job cannot be registered")
    protocol.require(any(item["round_id"] == round_id for item in state["rounds"]),
                     "registration round is not in job history")


def _no_cycle(records: dict[str, dict[str, Any]], record: dict[str, Any]) -> None:
    seen = {record["registration_id"]}
    parent = record.get("parent_registration_id")
    while parent is not None:
        protocol.require(parent in records, "parent registration is not recorded")
        protocol.require(parent not in seen, "session relationship cycle")
        seen.add(parent)
        parent = records[parent].get("parent_registration_id")


def register(run_path: str, *, registration_id: str, host: str | None, store: str | None,
             profile: str | None, session_id: str | None, role: str, kind: str = "managed",
             job_id: str | None = None, round_id: str | None = None,
             parent_registration_id: str | None = None, relationship: str = "root",
             label: str | None = None, now: str | None = None) -> dict[str, Any]:
    """Publish one explicit identity; identical retries return the existing record."""
    run = runs.load_run(run_path)
    record = {"version": 1, "registration_id": registration_id, "run_id": run["run_id"],
              "run_path": run["run_path"], "kind": kind, "role": role, "host": host,
              "store": store, "profile": profile, "session_id": session_id, "job_id": job_id,
              "round_id": round_id, "parent_registration_id": parent_registration_id,
              "relationship": relationship, "label": label, "registered_at": now or protocol.utc_now()}
    _validate(record, run)
    with jobs.locked(Path(run["index_path"])):
        root = _root(run)
        records = _load_all(root, run)
        _no_cycle(records, record)
        path = _record_path(root, registration_id)
        try:
            existing = protocol.read_json(path)
        except FileNotFoundError:
            _check_job(run, job_id, round_id)
            root = _root(run, create=True)
            protocol.publish(path, record)
            jobs.sync_directory(root)
            jobs.sync_directory(root.parent)
            return record
        same = dict(existing)
        same.pop("registered_at", None)
        candidate = dict(record)
        candidate.pop("registered_at", None)
        protocol.require(same == candidate, "registration identity already has different details")
        jobs.sync_directory(root)
        jobs.sync_directory(root.parent)
        return _validate(existing, run)


def list_records(run_path: str) -> list[dict[str, Any]]:
    """Read explicit records for a run; absent registry is a valid legacy state."""
    run = runs.load_run(run_path)
    root = _root(run)
    if not root.is_dir():
        return []
    return list(_load_all(root, run).values())


def digest(records: list[dict[str, Any]]) -> str:
    """Stable digest for a directory snapshot without exposing private task text."""
    return hashlib.sha256(json.dumps(records, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "list"))
    parser.add_argument("--run", required=True)
    parser.add_argument("--id", dest="registration_id")
    parser.add_argument("--host")
    parser.add_argument("--store")
    parser.add_argument("--profile")
    parser.add_argument("--session-id")
    parser.add_argument("--role", choices=sorted(ROLES))
    parser.add_argument("--kind", choices=sorted(KINDS), default="managed")
    parser.add_argument("--job")
    parser.add_argument("--round")
    parser.add_argument("--parent")
    parser.add_argument("--relationship", choices=sorted(RELATIONSHIPS), default="root")
    parser.add_argument("--label")
    args = parser.parse_args()
    try:
        if args.command == "list":
            output: Any = {"version": 1, "registrations": list_records(args.run)}
        else:
            protocol.require(args.registration_id is not None and args.role is not None,
                             "register requires --id and --role")
            output = register(args.run, registration_id=args.registration_id, host=args.host,
                              store=args.store, profile=args.profile, session_id=args.session_id,
                              role=args.role, kind=args.kind, job_id=args.job, round_id=args.round,
                              parent_registration_id=args.parent, relationship=args.relationship,
                              label=args.label)
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"error": "session registry failed; inspect the run privately"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
