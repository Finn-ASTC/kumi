#!/usr/bin/env python3
"""Explicit, evidence-backed native host capability records.

Records are kumi-owned run sidecars.  Registering a capability never probes,
starts, resumes, archives, or otherwise writes to a native host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

import jobs
import protocol
import runs


REGISTRY_NAME = "capability-registry"
CAPABILITIES = ("session_store_isolation", "native_history_visibility", "resume",
                "archive", "archive_descendants", "evidence_read")
STATUSES = {"unknown", "declared", "verified", "unsupported"}
ID = re.compile(r"[0-9a-f]{64}")
MAX_TEXT = 512


def _text(value: Any, label: str, *, none: bool = False) -> str | None:
    if value is None and none:
        return None
    protocol.require(isinstance(value, str) and 0 < len(value) <= MAX_TEXT
                     and value.isprintable() and "\x00" not in value, "invalid " + label)
    return value


def _root(run: dict[str, Any]) -> Path:
    path = Path(run["run_path"]).parent / REGISTRY_NAME
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _key(host: str, profile: str, capability: str) -> str:
    raw = "\0".join((host, profile, capability)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validate(record: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    protocol.require(record.get("version") == 1 and record.get("run_id") == run["run_id"]
                     and record.get("run_path") == run["run_path"], "invalid capability record")
    protocol.require(ID.fullmatch(record.get("record_id", "")), "invalid capability record ID")
    for key in ("host", "profile", "capability", "status", "evidence_path", "observed_at", "host_version", "note"):
        _text(record.get(key), key, none=key in {"evidence_path", "observed_at", "host_version", "note"})
    protocol.require(record["record_id"] == _key(record["host"], record["profile"], record["capability"]),
                     "capability identity changed")
    protocol.require(record["capability"] in CAPABILITIES and record["status"] in STATUSES,
                     "invalid capability/status")
    if record["status"] == "verified":
        protocol.require(record["evidence_path"] is not None and record["observed_at"] is not None,
                         "verified capability requires evidence and observation time")
    return record


def register(run_path: str, *, host: str, profile: str, capability: str,
             status: str, evidence_path: str | None = None,
             observed_at: str | None = None, host_version: str | None = None,
             note: str | None = None) -> dict[str, Any]:
    """Record a capability claim or verification without invoking its host."""
    run = runs.load_run(run_path)
    _text(host, "host"); _text(profile, "profile"); _text(capability, "capability")
    _text(status, "status"); _text(evidence_path, "evidence_path", none=True)
    _text(observed_at, "observed_at", none=True); _text(host_version, "host_version", none=True)
    _text(note, "note", none=True)
    record = {"version": 1, "record_id": _key(host, profile, capability),
              "run_id": run["run_id"], "run_path": run["run_path"], "host": host,
              "profile": profile, "capability": capability, "status": status,
              "evidence_path": evidence_path, "observed_at": observed_at,
              "host_version": host_version, "note": note}
    _validate(record, run)
    root = _root(run)
    path = root / (record["record_id"] + ".json")
    with jobs.locked(Path(run["index_path"])):
        try:
            existing = protocol.read_json(path)
        except FileNotFoundError:
            protocol.publish(path, record)
            jobs.sync_directory(root)
            return record
        protocol.require(existing == record, "capability record already has different details")
        return _validate(existing, run)


def list_records(run_path: str) -> list[dict[str, Any]]:
    """Read capability sidecars; absent records remain unknown."""
    run = runs.load_run(run_path)
    root = Path(run["run_path"]).parent / REGISTRY_NAME
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("[0-9a-f]" * 64 + ".json")):
        record = protocol.read_json(path)
        records.append(_validate(record, run))
    return records


def by_host_profile(records: list[dict[str, Any]], host: str | None,
                    profile: str | None) -> dict[str, dict[str, Any]]:
    if host is None or profile is None:
        return {}
    return {record["capability"]: record for record in records
            if record["host"] == host and record["profile"] == profile}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "list"))
    parser.add_argument("--run", required=True)
    parser.add_argument("--host")
    parser.add_argument("--profile")
    parser.add_argument("--capability")
    parser.add_argument("--status", choices=sorted(STATUSES))
    parser.add_argument("--evidence-path")
    parser.add_argument("--observed-at")
    parser.add_argument("--host-version")
    parser.add_argument("--note")
    args = parser.parse_args()
    try:
        if args.command == "list":
            output: Any = {"version": 1, "records": list_records(args.run)}
        else:
            protocol.require(all(value is not None for value in
                                 (args.host, args.profile, args.capability, args.status)),
                             "register requires host, profile, capability, and status")
            output = register(args.run, host=args.host, profile=args.profile,
                              capability=args.capability, status=args.status,
                              evidence_path=args.evidence_path, observed_at=args.observed_at,
                              host_version=args.host_version, note=args.note)
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"error": "capability registry failed; inspect the run privately"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
