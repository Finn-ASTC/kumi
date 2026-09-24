#!/usr/bin/env python3
"""Explicit, evidence-backed native host capability records.

Records are kumi-owned run sidecars.  Registering a capability never probes,
starts, resumes, archives, or otherwise writes to a native host.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
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


def _root(run: dict[str, Any], *, create: bool = False) -> Path:
    path = Path(run["run_path"]).parent / REGISTRY_NAME
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if os.path.lexists(path):
        protocol.require(path.is_dir() and path.resolve() == path,
                         "capability registry missing or redirected")
    return path


def _key(host: str, profile: str, capability: str) -> str:
    raw = "\0".join((host, profile, capability)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validate(record: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    protocol.require(type(record.get("version")) is int and record["version"] in (1, 2)
                     and record.get("run_id") == run["run_id"]
                     and record.get("run_path") == run["run_path"], "invalid capability record")
    protocol.require(isinstance(record.get("record_id"), str) and ID.fullmatch(record["record_id"]),
                     "invalid capability record ID")
    fields = {"version", "record_id", "run_id", "run_path", "host", "profile", "capability", "status",
              "evidence_path", "observed_at", "host_version", "note"}
    if record["version"] == 2:
        fields.add("evidence_sha256")
    protocol.require(set(record) == fields, "invalid capability record fields")
    for key in ("host", "profile", "capability", "status", "evidence_path", "observed_at", "host_version", "note"):
        _text(record.get(key), key, none=key in {"evidence_path", "observed_at", "host_version", "note"})
    protocol.require(record["record_id"] == _key(record["host"], record["profile"], record["capability"]),
                     "capability identity changed")
    protocol.require(record["capability"] in CAPABILITIES and record["status"] in STATUSES,
                     "invalid capability/status")
    if record["status"] == "verified":
        protocol.require(record["evidence_path"] is not None and record["observed_at"] is not None,
                         "verified capability requires evidence and observation time")
    if record["version"] == 2:
        if record["observed_at"] is not None:
            _observed(record["observed_at"])
        if record["evidence_path"] is not None:
            protocol.require(Path(record["evidence_path"]).is_absolute()
                             and isinstance(record["evidence_sha256"], str)
                             and ID.fullmatch(record["evidence_sha256"]), "invalid evidence pin")
        else:
            protocol.require(record["evidence_sha256"] is None, "digest requires evidence path")
        if record["status"] == "verified":
            protocol.require(record["host_version"] is not None, "verified capability needs host version")
    return record


def _observed(value: str) -> None:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    protocol.require(stamp.tzinfo is not None and stamp.utcoffset() is not None
                     and stamp <= datetime.now(timezone.utc), "invalid observation time")


def assessment(record: dict[str, Any]) -> dict[str, Any]:
    """Verify reference integrity, not the truth or current applicability of a claim."""
    evidence_status = "not_provided"
    if record.get("evidence_path") is not None:
        evidence_status = "unpinned"
        if record.get("evidence_sha256") is not None:
            try:
                valid = jobs.fingerprint(Path(record["evidence_path"])) == record["evidence_sha256"]
            except (OSError, ValueError):
                valid = False
            evidence_status = "valid" if valid else "invalid"
    effective = record["status"]
    if effective == "verified" and evidence_status != "valid":
        effective = "unknown"
    return {**record, "effective_status": effective, "evidence_status": evidence_status}


def register(run_path: str, *, host: str, profile: str, capability: str,
             status: str, evidence_path: str | None = None,
             observed_at: str | None = None, host_version: str | None = None,
             note: str | None = None) -> dict[str, Any]:
    """Record a capability claim or verification without invoking its host."""
    run = runs.load_run(run_path)
    _text(host, "host")
    _text(profile, "profile")
    _text(capability, "capability")
    _text(status, "status")
    _text(evidence_path, "evidence_path", none=True)
    _text(observed_at, "observed_at", none=True)
    _text(host_version, "host_version", none=True)
    _text(note, "note", none=True)
    record = {"version": 2, "record_id": _key(host, profile, capability),
              "run_id": run["run_id"], "run_path": run["run_path"], "host": host,
              "profile": profile, "capability": capability, "status": status,
              "evidence_path": evidence_path, "observed_at": observed_at,
              "host_version": host_version, "note": note, "evidence_sha256": None}
    if evidence_path is not None:
        protocol.require(Path(evidence_path).is_absolute(), "evidence must be an absolute path")
        record["evidence_sha256"] = jobs.fingerprint(Path(evidence_path))
    _validate(record, run)
    with jobs.locked(Path(run["index_path"])):
        root = _root(run, create=True)
        path = root / (record["record_id"] + ".json")
        try:
            existing = protocol.read_json(path)
        except FileNotFoundError:
            protocol.publish(path, record)
            jobs.sync_directory(root)
            jobs.sync_directory(root.parent)
            return record
        protocol.require(existing == record, "capability record already has different details")
        jobs.sync_directory(root)
        jobs.sync_directory(root.parent)
        return _validate(existing, run)


def list_records(run_path: str) -> list[dict[str, Any]]:
    """Read capability sidecars; absent records remain unknown."""
    run = runs.load_run(run_path)
    root = _root(run)
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        protocol.require(ID.fullmatch(path.stem), "invalid capability filename")
        record = protocol.read_json(path)
        protocol.require(record.get("record_id") == path.stem, "capability filename mismatch")
        records.append(assessment(_validate(record, run)))
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
