"""Append-only, compare-and-swap corrections for a single job's native usage ledger."""

import hashlib
import json
from pathlib import Path
import re
from typing import Any

import protocol
import usage


def digest(value: Any) -> str:
    """Fingerprint canonical JSON, independent of file formatting."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def key(sample: dict[str, Any]) -> tuple[str, str]:
    """Native call identity survives all corrections."""
    return sample["source"], sample["sample_id"]


def nonempty(value: Any, label: str) -> None:
    """Reject ambiguous audit metadata."""
    protocol.require(isinstance(value, str) and bool(value.strip()), f"{label} must be a nonempty string")


def fingerprint(value: Any) -> bool:
    """Revision and evidence hashes are lowercase SHA-256 strings."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def validate_manifest(manifest: dict[str, Any], check_evidence: bool = False) -> list[tuple[Path, dict[str, Any]]]:
    """Validate the whole replacement set before any ledger write."""
    protocol.require(set(manifest) == {"version", "request_path", "correction_id", "reason", "evidence", "updates"} and
                     type(manifest["version"]) is int and manifest["version"] == 1,
                     "invalid correction manifest")
    for field in ("correction_id", "reason"):
        nonempty(manifest[field], field)
    nonempty(manifest["request_path"], "ledger request_path")
    protocol.require(Path(manifest["request_path"]).is_absolute(), "ledger request_path must be absolute")
    anchor = usage.request_chain(manifest["request_path"])
    evidence = manifest["evidence"]
    protocol.require(isinstance(evidence, list) and bool(evidence), "correction requires evidence")
    for item in evidence:
        protocol.require(isinstance(item, dict) and set(item) == {"path", "sha256"}, "invalid correction evidence")
        nonempty(item["path"], "evidence path")
        protocol.require(Path(item["path"]).is_absolute() and fingerprint(item["sha256"]),
                         "evidence requires an absolute path and SHA-256")
        if check_evidence:
            actual = hashlib.sha256(protocol.read_bytes(item["path"])).hexdigest()
            protocol.require(actual == item["sha256"], "correction evidence hash mismatch")
    updates = manifest["updates"]
    protocol.require(isinstance(updates, list) and bool(updates), "updates must be a nonempty array")
    roots = {anchor[0][0]}
    seen = set()
    requests = []
    for update in updates:
        protocol.require(isinstance(update, dict) and set(update) == {"request_path", "expected_revision", "sample"},
                         "invalid correction update")
        protocol.require(fingerprint(update["expected_revision"]), "invalid expected_revision")
        nonempty(update["request_path"], "request_path")
        protocol.require(Path(update["request_path"]).is_absolute(), "request_path must be absolute")
        chain = usage.request_chain(update["request_path"])
        roots.add(chain[0][0])
        requests.append(chain[-1])
        sample = update["sample"]
        protocol.require(isinstance(sample, dict), "replacement sample must be an object")
        usage.validate_sample(sample)
        protocol.require(not any(k in sample for k in protocol.IDENTITY_FIELDS), "replacement identity comes from request")
        protocol.require(key(sample) not in seen, "duplicate correction update")
        seen.add(key(sample))
    protocol.require(len(roots) == 1, "corrections cannot transfer samples across jobs")
    return requests


def check_attempts(entries: dict[tuple[str, str], dict[str, Any]]) -> None:
    """All effective calls in a round/attempt must share attribution."""
    attempts: dict[tuple[str, str], tuple[Any, ...]] = {}
    for entry in entries.values():
        sample = entry["sample"]
        attempt = sample["round_id"], sample["attempt_id"]
        value = usage.attribution(sample)
        protocol.require(attempt not in attempts or attempts[attempt] == value,
                         "attempt purpose or attribution conflicts; correct the whole attempt together")
        attempts[attempt] = value


def original_entry(path: Path, sample: dict[str, Any]) -> dict[str, Any]:
    """Initialize a revision from an immutable base record."""
    return dict(path=path, original=sample, sample=sample,
                revision=digest({"original": sample}), corrections=[])


def replacements(state: dict[str, Any], manifest: dict[str, Any],
                 requests: list[tuple[Path, dict[str, Any]]],
                 validate_attempts: bool = True) -> dict[tuple[str, str], dict[str, Any]]:
    """Compare all expected revisions and validate a hypothetical complete ledger."""
    entries = {k: dict(v) for k, v in state["entries"].items()}
    for update, (_, request) in zip(manifest["updates"], requests):
        sample = update["sample"]
        native = key(sample)
        protocol.require(native in entries, "correction sample not found in this job")
        entry = entries[native]
        protocol.require(entry["revision"] == update["expected_revision"], "stale correction revision")
        protocol.require(entry["original"]["job_id"] == request["job_id"], "corrections cannot transfer samples across jobs")
        entry["sample"] = {**sample, **{k: request[k] for k in protocol.IDENTITY_FIELDS}}
    if validate_attempts:
        check_attempts(entries)
    return entries


def load_ledger(chain: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    """Replay immutable events; read explicit legacy bases outside a shorter request chain."""
    directory = chain[0][0].parent / "usage"
    state: dict[str, Any] = dict(entries={}, events=[], head=None)
    for path, sample in usage.raw_stored_samples(chain):
        protocol.require(key(sample) not in state["entries"], "native usage sample appears in multiple rounds/files")
        state["entries"][key(sample)] = original_entry(path, sample)
    correction_ids = set()
    for number, path in enumerate(sorted((directory / "corrections").glob("*.json")), 1):
        event = protocol.read_json(path)
        protocol.require(set(event) == {"version", "sequence", "previous", "created_at", "manifest", "original_paths"}
                         and type(event["version"]) is int and event["version"] == 1,
                         "invalid correction event")
        protocol.require(type(event["sequence"]) is int and event["sequence"] == number and
                         event["previous"] == state["head"], "correction chain gap or fingerprint mismatch")
        nonempty(event["created_at"], "correction timestamp")
        manifest = event["manifest"]
        protocol.require(isinstance(manifest, dict), "invalid correction manifest")
        requests = validate_manifest(manifest)
        protocol.require(usage.request_chain(str(requests[0][0]))[0][0] == chain[0][0], "correction belongs to another ledger")
        ident = manifest["correction_id"]
        protocol.require(ident not in correction_ids, "duplicate correction_id in ledger")
        correction_ids.add(ident)
        protocol.require(path.name == event_name(number, ident), "correction filename/identity mismatch")
        origins = event["original_paths"]
        protocol.require(isinstance(origins, list) and len(origins) == len(manifest["updates"]), "invalid correction bases")
        for update, origin in zip(manifest["updates"], origins):
            nonempty(origin, "original_path")
            protocol.require(Path(origin).is_absolute(), "original_path must be absolute")
            native = key(update["sample"])
            if native not in state["entries"]:
                # A legacy base may be in a later/sibling round not selected by this summary.
                base = Path(origin)
                protocol.require(base.parent.name == "usage" and base.suffix == ".json", "missing correction base")
                base_chain = usage.request_chain(str(base.parent.parent / "request.json"))
                protocol.require(base_chain[0][0] == chain[0][0], "legacy correction base belongs to another ledger")
                sample = usage.validate_sample(protocol.read_json(base))
                protocol.require(all(type(sample.get(k)) is type(base_chain[-1][1][k]) and
                                     sample[k] == base_chain[-1][1][k] for k in protocol.IDENTITY_FIELDS),
                                 "legacy correction base identity mismatch")
                protocol.require(key(sample) == native, "legacy correction base native identity mismatch")
                state["entries"][native] = original_entry(base, sample)
            protocol.require(str(state["entries"][native]["path"]) == origin, "correction base path mismatch")
        # Calls recorded after an old correction already have the newer role.
        # Validate attempt consistency at the final view, not at historical intermediates.
        state["entries"] = replacements(state, manifest, requests, validate_attempts=False)
        revision = digest(event)
        for update in manifest["updates"]:
            entry = state["entries"][key(update["sample"])]
            entry["revision"] = revision
            entry["corrections"] = [*entry["corrections"], dict(path=str(path), revision=revision,
                correction_id=ident, reason=manifest["reason"], evidence=manifest["evidence"],
                previous_revision=update["expected_revision"], effective=entry["sample"])]
        state["head"] = revision
        state["events"].append(dict(path=str(path), event=event))
    check_attempts(state["entries"])
    return state


def event_name(sequence: int, correction_id: str) -> str:
    """Keep publication order explicit and user IDs out of filesystem paths."""
    return f"{sequence:020d}-{digest(correction_id)}.json"


def history(request_path: str, source: str, sample_id: str) -> dict[str, Any]:
    """Expose original, effective revision and correction reasons for one native call."""
    state = load_ledger(usage.request_chain(request_path))
    protocol.require((source, sample_id) in state["entries"], "usage sample not found")
    entry = state["entries"][(source, sample_id)]
    return dict(source=source, sample_id=sample_id, original_path=str(entry["path"]),
                original=entry["original"], effective=entry["sample"], revision=entry["revision"],
                corrections=entry["corrections"])


def correct_manifest(path: str, dry_run: bool = False) -> dict[str, Any]:
    """Atomically append all replacements with evidence and optimistic concurrency checks."""
    manifest = protocol.read_json(path)
    requests = validate_manifest(manifest, check_evidence=True)
    chain = usage.request_chain(manifest["request_path"])
    directory = chain[0][0].parent / "usage"

    def publish() -> dict[str, Any]:
        state = load_ledger(chain)
        for existing in state["events"]:
            if existing["event"]["manifest"]["correction_id"] == manifest["correction_id"]:
                protocol.require(existing["event"]["manifest"] == manifest, "correction_id conflicts with existing correction")
                return dict(path=existing["path"], duplicate=True, dry_run=dry_run, updates=len(manifest["updates"]),
                            revision=digest(existing["event"]))
        replacements(state, manifest, requests)
        sequence = len(state["events"]) + 1
        event = dict(version=1, sequence=sequence, previous=state["head"], created_at=protocol.utc_now(),
                     manifest=manifest, original_paths=[str(state["entries"][key(u["sample"])]["path"])
                                                        for u in manifest["updates"]])
        encoded = json.dumps(event, ensure_ascii=False, indent=2, allow_nan=False).encode() + b"\n"
        protocol.require(len(encoded) <= protocol.MAX_FILE_BYTES, "correction event exceeds readable size limit")
        destination = directory / "corrections" / event_name(sequence, manifest["correction_id"])
        if not dry_run:
            destination.parent.mkdir(mode=0o700, exist_ok=True)
            protocol.publish(destination, event)
        return dict(path=str(destination), duplicate=False, dry_run=dry_run, updates=len(manifest["updates"]),
                    revision=None if dry_run else digest(event))

    if dry_run:
        return publish()
    protocol.require(bool(load_ledger(chain)["entries"]), "correction requires an existing usage ledger")
    with usage.ledger_lock(directory):
        return publish()
