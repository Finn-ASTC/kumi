"""Explicit native usage adapters; never scan session directories or infer rounds."""

import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

import protocol
import usage


def nonempty(value: Any, label: str) -> str:
    """Require a stable, explicit identity."""
    protocol.require(isinstance(value, str) and bool(value.strip()), f"{label} must be a nonempty string")
    return value


def objects(path: str) -> Iterator[dict[str, Any]]:
    """Read strict JSONL; incomplete logs must be retried once writing finishes."""
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            protocol.require(key not in value, f"duplicate JSON key: {key}")
            value[key] = item
        return value

    with Path(path).open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                value = json.loads(line, object_pairs_hook=unique)
                protocol.require(isinstance(value, dict), "JSONL row must be an object")
                protocol.validate_json_values(value)
            except ValueError as exc:
                raise protocol.ProtocolError(f"invalid native JSONL at line {number}: {exc}") from exc
            yield value


def counts(value: Any, fields: tuple[str, ...]) -> dict[str, int]:
    """Reject absent, negative, boolean or otherwise ambiguous native counters."""
    protocol.require(isinstance(value, dict), "native counters must be an object")
    for field in fields:
        protocol.require(type(value.get(field)) is int and value[field] >= 0,
                         f"native {field} must be a nonnegative integer")
    return {field: value[field] for field in fields}


def call_totals(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize observed counters while exposing unavailable call telemetry."""
    return {"calls": len(samples),
            "unknown_calls": sum(any(s[k] is None for k in usage.COUNTERS) for s in samples),
            "known_subtotals": {k: sum(s[k] for s in samples if s[k] is not None)
                                for k in usage.COUNTERS}}


def read_log(host: str, path: str, session_id: str | None = None,
             selected_native_ids: set[str] | None = None) -> dict[str, Any]:
    """Normalize reported calls and retain native attribution, excluding prompt text."""
    if host == "opencode":
        import native_stores
        return native_stores.read_opencode(path, session_id, selected_native_ids)
    if host == "hermes":
        import hermes_hooks
        protocol.require(hermes_hooks.is_store(path), "Hermes cumulative snapshots cannot be imported as round deltas")
        return hermes_hooks.read_log(path, session_id, selected_native_ids)
    protocol.require(host in ("codex", "omp"), "unsupported native host")
    session = None
    provider = model = current = None
    previous = dict.fromkeys(usage.COUNTERS, 0)
    calls: list[dict[str, Any]] = []
    native_ids: dict[str, None] = {}
    # omp entries form a tree; physical append order is not conversation ancestry.
    parents: dict[str, str | None] = {}
    fingerprints: dict[str, str] = {}
    for event in objects(path):
        kind = event.get("type")
        payload = event.get("payload", {}) if host == "codex" else event
        protocol.require(isinstance(payload, dict), "native payload must be an object")
        if kind == ("session_meta" if host == "codex" else "session"):
            identity = nonempty(payload.get("id"), "native session ID")
            protocol.require(session is None, "multiple native session headers")
            if host == "omp":
                protocol.require(type(payload.get("version")) is int and payload["version"] == 3,
                                 "unsupported omp session version")
            session = identity
            provider = payload.get("model_provider")
            continue
        if host == "codex":
            if kind == "turn_context" or (kind == "event_msg" and payload.get("type") == "task_started"):
                next_turn = nonempty(payload.get("turn_id"), "native turn ID")
                if next_turn != current:
                    model = None
                current = next_turn
                native_ids[current] = None
                if kind == "turn_context":
                    model = payload.get("model")
            if kind != "event_msg" or payload.get("type") != "token_count" or payload.get("info") is None:
                continue
            info = payload["info"]
            protocol.require(isinstance(info, dict), "native token info must be an object")
            total = counts(info.get("total_token_usage"), usage.COUNTERS)
            last = counts(info.get("last_token_usage"), usage.COUNTERS)
            if total == previous:
                continue  # Codex repeats the previous count in metadata events.
            protocol.require(all(total[key] - previous[key] == last[key] for key in usage.COUNTERS),
                             "native delta disagrees with cumulative baseline or counters reset")
            protocol.require(current is not None, "native usage has no turn identity")
            sample_id = hashlib.sha256(json.dumps(info["total_token_usage"], sort_keys=True).encode()).hexdigest()
            counters: dict[str, int | None] = dict(last)
            previous = total
        else:
            entry_id = event.get("id")
            if entry_id is None:
                protocol.require(kind != "message", "omp message has no entry ID")
                continue  # For example, the padded title preceding the session header.
            entry_id = nonempty(entry_id, "omp entry ID")
            fingerprint = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
            if entry_id in fingerprints:
                protocol.require(fingerprints[entry_id] == fingerprint, "conflicting duplicate omp entry ID")
                continue
            fingerprints[entry_id] = fingerprint
            parent = event.get("parentId")
            protocol.require(parent is None or isinstance(parent, str) and parent in parents,
                             "omp parent missing or out of order")
            current = parents[parent] if parent is not None else None
            message = event.get("message", {})
            protocol.require(isinstance(message, dict), "omp message must be an object")
            if kind == "message" and message.get("role") == "user":
                current = entry_id
                native_ids[current] = None
            parents[entry_id] = current
            if kind != "message" or message.get("role") != "assistant":
                continue
            protocol.require(current is not None, "omp assistant has no user-message ancestor")
            native = message.get("usage")
            counters = dict.fromkeys(usage.COUNTERS)
            if native is not None:
                raw = counts(native, ("input", "cacheRead", "cacheWrite", "output", "totalTokens"))
                total_input = raw["input"] + raw["cacheRead"] + raw["cacheWrite"]
                protocol.require(total_input + raw["output"] == raw["totalTokens"],
                                 "omp native totalTokens disagrees with input/cache/output")
                counters = dict(input_tokens=total_input, cached_input_tokens=raw["cacheRead"],
                                output_tokens=raw["output"])
            sample_id = entry_id
            provider, model = message.get("provider"), message.get("model")
        protocol.require(session is not None, "native usage precedes session header")
        sample = dict(sample_id=sample_id, source=f"{host}:{session}", measurement="delta",
                      provider=provider, model=model, **counters)
        usage.validate_sample({**sample, "attempt_id": "validation", "purpose": "task"})
        calls.append({"native_id": current, "sample": sample})
    protocol.require(session is not None, "missing native session header")
    protocol.require(session_id is None or session == session_id, "native session identity mismatch")
    return {"host": host, "session_id": session, "native_ids": list(native_ids), "calls": calls}


def inspect_log(host: str, path: str, session_id: str | None = None) -> dict[str, Any]:
    """List stable IDs and measured coverage without exposing native conversation text."""
    if host == "hermes":
        import hermes_hooks
        import native_stores
        if not hermes_hooks.is_store(path):
            return native_stores.inspect_hermes(path, session_id)
    log = read_log(host, path, session_id)
    turns = []
    for native_id in log["native_ids"]:
        samples = [entry["sample"] for entry in log["calls"] if entry["native_id"] == native_id]
        pending = [entry["sample_id"] for entry in log.get("pending", []) if entry["native_id"] == native_id]
        turns.append({"native_id": native_id, "sample_ids": [s["sample_id"] for s in samples],
                      "pending_sample_ids": pending, **call_totals(samples)})
    return {"host": host, "session_id": log["session_id"], "source": f"{host}:{log['session_id']}",
            "turns": turns, **{k: log[k] for k in ("parent_session_id", "child_session_ids", "measurement",
                "coverage_complete", "coverage_scope", "limitations", "integrity_issues", "turn_receipts",
                "call_receipts", "store_id", "persisted_sequence") if k in log}}


def import_manifest(path: str, dry_run: bool = False) -> dict[str, Any]:
    """Validate the complete source and mapping, then idempotently publish selected calls."""
    manifest = protocol.read_json(path)
    protocol.require(set(manifest) == {"version", "host", "log_path", "session_id", "mappings"},
                     "invalid native manifest fields")
    protocol.require(type(manifest["version"]) is int and manifest["version"] == 1,
                     "unsupported native manifest version")
    log_path = nonempty(manifest["log_path"], "log_path")
    protocol.require(Path(log_path).is_absolute(), "log_path must be absolute")
    nonempty(manifest["session_id"], "session_id")
    mappings = manifest["mappings"]
    protocol.require(isinstance(mappings, list) and bool(mappings), "mappings must be a nonempty array")
    selected_ids = set()
    for mapping in mappings:
        protocol.require(isinstance(mapping, dict), "invalid native mapping fields")
        selected_ids.add(nonempty(mapping.get("native_id"), "native_id"))
    log = read_log(manifest["host"], log_path, manifest["session_id"], selected_ids)
    protocol.require(log["session_id"] == manifest["session_id"], "native session identity mismatch")
    selected: dict[str, dict[str, Any]] = {}
    attempts: dict[tuple[str, str, str], tuple[Any, ...]] = {}
    for mapping in mappings:
        required = {"native_id", "request_path", "attempt_id", "purpose"}
        protocol.require(isinstance(mapping, dict) and required <= set(mapping) <= required | set(usage.DIMENSIONS),
                         "invalid native mapping fields")
        usage.validate_dimensions(mapping)
        native_id = nonempty(mapping["native_id"], "native_id")
        protocol.require(native_id in log["native_ids"], "native mapping ID not found in log")
        protocol.require(native_id not in selected, "duplicate native mapping")
        request_path = nonempty(mapping["request_path"], "request_path")
        protocol.require(Path(request_path).is_absolute(), "request_path must be absolute")
        request = usage.request_chain(request_path)[-1][1]
        attempt = nonempty(mapping["attempt_id"], "attempt_id")
        purpose = mapping["purpose"]
        protocol.require(purpose in usage.PURPOSES, "invalid attempt purpose")
        key = (request["job_id"], request["round_id"], attempt)
        attribution = usage.attribution(mapping)
        protocol.require(key not in attempts or attempts[key] == attribution,
                         "attempt purpose or attribution changed within a round")
        attempts[key] = attribution
        selected[native_id] = mapping
    selected_calls = [entry for entry in log["calls"] if entry["native_id"] in selected]
    protocol.require(all(any(entry["native_id"] == key for entry in selected_calls) for key in selected),
                     "mapped native turn has no calls; usage remains unknown")
    report = {"host": manifest["host"], "session_id": log["session_id"], "dry_run": dry_run,
              "selected_calls": len(selected_calls), "unmapped_calls": len(log["calls"]) - len(selected_calls),
              "pending_calls": len(log.get("pending", [])),
              "pending_native_ids": sorted({p["native_id"] for p in log.get("pending", [])}),
              "imported": 0, "duplicates": 0,
              **{k: log[k] for k in ("coverage_complete", "coverage_scope", "limitations", "persisted_sequence") if k in log},
              "mappings": [{**mapping, **call_totals([entry["sample"] for entry in selected_calls
                                                     if entry["native_id"] == native_id])}
                           for native_id, mapping in selected.items()]}
    if not dry_run:
        for entry in selected_calls:
            mapping = selected[entry["native_id"]]
            sample = {**entry["sample"], "attempt_id": mapping["attempt_id"], "purpose": mapping["purpose"],
                      **{k: mapping[k] for k in usage.DIMENSIONS if k in mapping}}
            result = usage.record_usage(mapping["request_path"], sample)
            report["duplicates" if result["duplicate"] else "imported"] += 1
    return report
