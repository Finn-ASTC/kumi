"""Frozen Hermes observations and non-additive cumulative interval diagnostics."""

from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

import native_stores
import protocol
import usage

RAW = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")


def capture(log_path: str, session_id: str, output: str) -> dict[str, str]:
    """Read native SQLite without mutation and publish a new, bounded evidence file."""
    path = Path(log_path).resolve()
    before = path.stat()
    snapshot = native_stores.inspect_hermes(str(path), session_id)
    after = path.stat()
    protocol.require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino),
                     "native database replaced during capture")
    value = dict(version=1, kind="hermes_usage_observation", store=dict(path=str(path),
                 device=before.st_dev, inode=before.st_ino), snapshot=snapshot)
    protocol.require(len((json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()) <= protocol.MAX_FILE_BYTES,
                     "snapshot exceeds evidence size limit")
    destination = Path(output).absolute()
    protocol.publish(destination, value)
    return dict(path=str(destination), sha256=hashlib.sha256(protocol.read_bytes(destination)).hexdigest())


def _row(row: Any) -> None:
    """Validate saved numeric evidence, including the normalization relationship."""
    protocol.require(isinstance(row, dict), "invalid saved usage row")
    bucket = row.get("bucket_id")
    protocol.require(isinstance(bucket, str) and len(bucket) == 64 and
                     all(c in "0123456789abcdef" for c in bucket), "invalid bucket identity")
    for key in ("model", "provider", "task"):
        protocol.require(row.get(key) is None or isinstance(row[key], str), "invalid row label")
    native_stores.counter(row["api_call_count"], "api_call_count")
    raw = row["native_counters"]
    protocol.require(isinstance(raw, dict) and set(raw) == set(RAW), "invalid native counters")
    for key, value in raw.items():
        native_stores.counter(value, key)
    normalized = dict(input_tokens=native_stores.add_known(raw["input_tokens"], raw["cache_read_tokens"],
                      raw["cache_write_tokens"]), cached_input_tokens=raw["cache_read_tokens"],
                      output_tokens=raw["output_tokens"])
    protocol.require(isinstance(row["counters"], dict) and set(row["counters"]) == set(usage.COUNTERS),
                     "invalid normalized counters")
    for key, value in row["counters"].items():
        native_stores.counter(value, key)
    protocol.require(row["counters"] == normalized, "saved normalization disagrees with raw counters")


def _load(receipt: Any) -> dict[str, Any]:
    """Hash-check frozen bytes, without consulting a mutable live source."""
    protocol.require(isinstance(receipt, dict) and set(receipt) == {"path", "sha256"}, "invalid snapshot receipt")
    protocol.require(isinstance(receipt["path"], str) and Path(receipt["path"]).is_absolute(),
                     "snapshot path must be absolute")
    raw = protocol.read_bytes(receipt["path"])
    protocol.require(hashlib.sha256(raw).hexdigest() == receipt["sha256"], "snapshot hash mismatch")
    value = protocol.parse_json(raw.decode("utf-8"))
    protocol.require(set(value) == {"version", "kind", "store", "snapshot"} and
                     type(value["version"]) is int and value["version"] == 1 and
                     value["kind"] == "hermes_usage_observation", "invalid Hermes observation")
    store = value["store"]
    protocol.require(isinstance(store, dict) and set(store) == {"path", "device", "inode"} and
                     isinstance(store["path"], str) and Path(store["path"]).is_absolute() and
                     all(type(store[k]) is int and store[k] >= 0 for k in ("device", "inode")), "invalid store identity")
    snap = value["snapshot"]
    protocol.require(isinstance(snap, dict) and snap.get("host") == "hermes" and
                     snap.get("measurement") == "cumulative_snapshot", "invalid cumulative snapshot")
    sid = native_stores.identity(snap.get("session_id"), "session_id")
    protocol.require(snap.get("source") == "hermes:" + sid, "snapshot source mismatch")
    observed = datetime.fromisoformat(snap["observed_at"])
    protocol.require(observed.tzinfo is not None, "snapshot time needs timezone")
    _row(snap["session_usage"])
    models = snap["model_usage"]
    protocol.require(models is None or isinstance(models, list), "invalid model inventory")
    seen = set()
    for row in models or []:
        _row(row)
        protocol.require(row["bucket_id"] not in seen, "duplicate model bucket")
        seen.add(row["bucket_id"])
    parent, children = snap.get("parent_session_id"), snap.get("child_session_ids")
    protocol.require(parent is None or isinstance(parent, str) and bool(parent), "invalid parent identity")
    protocol.require(children is None or isinstance(children, list) and
                     all(isinstance(c, str) and bool(c) for c in children) and len(set(children)) == len(children),
                     "invalid child identities")
    return value


def _difference(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    """Do not infer zero baselines, merge buckets, or subtract across visible resets."""
    row = after if after is not None else before
    issues = []
    if before is None:
        issues.append("missing_baseline")
    elif after is None:
        issues.append("missing_endpoint")
    elif before["bucket_id"] != after["bucket_id"]:
        issues.append("bucket_identity_changed")
    if before is not None and after is not None:
        left = {**before["native_counters"], "api_call_count": before["api_call_count"]}
        right = {**after["native_counters"], "api_call_count": after["api_call_count"]}
        if any(left[k] is not None and right[k] is not None and right[k] < left[k] for k in left):
            issues.append("counter_decreased")
    delta = dict.fromkeys(usage.COUNTERS)
    calls = None
    if not issues:
        delta = {k: None if before["counters"][k] is None or after["counters"][k] is None
                 else after["counters"][k] - before["counters"][k] for k in usage.COUNTERS}
        if before["api_call_count"] is not None and after["api_call_count"] is not None:
            calls = after["api_call_count"] - before["api_call_count"]
        if any(v is None for v in delta.values()) or calls is None:
            issues.append("missing_counters")
    return dict(bucket_id=row["bucket_id"], model=row["model"], provider=row["provider"], task=row["task"],
                observed_delta=delta, observed_api_call_delta=calls, issues=issues)


def compare(manifest_path: str) -> dict[str, Any]:
    """Compare only adjacent snapshots in one ordered chain; never mint ledger samples."""
    manifest = protocol.read_json(manifest_path)
    protocol.require(set(manifest) == {"version", "snapshots"} and type(manifest["version"]) is int and
                     manifest["version"] == 1, "invalid Hermes windows manifest")
    receipts = manifest["snapshots"]
    protocol.require(isinstance(receipts, list) and len(receipts) >= 2, "at least two snapshots are required")
    values = [_load(receipt) for receipt in receipts]
    protocol.require(len({r["sha256"] for r in receipts}) == len(receipts), "duplicate/overlapping snapshot chain")
    windows = []
    for position, (left, right) in enumerate(zip(values, values[1:])):
        a, b = left["snapshot"], right["snapshot"]
        protocol.require(left["store"] == right["store"] and a["session_id"] == b["session_id"],
                         "snapshot store/session identity changed")
        protocol.require(datetime.fromisoformat(a["observed_at"]) < datetime.fromisoformat(b["observed_at"]),
                         "snapshots must be strictly ordered; time is not round attribution")
        issues = []
        for key in ("parent_session_id", "child_session_ids"):
            if a[key] != b[key] and "lineage_changed" not in issues:
                issues.append("lineage_changed")
        if a["child_session_ids"] is None or b["child_session_ids"] is None:
            issues.append("lineage_unknown")
        if a["model_usage"] is None or b["model_usage"] is None:
            issues.append("model_inventory_unknown")
        old = {r["bucket_id"]: r for r in a["model_usage"] or []}
        new = {r["bucket_id"]: r for r in b["model_usage"] or []}
        windows.append(dict(before=receipts[position], after=receipts[position + 1],
            session=_difference(a["session_usage"], b["session_usage"]),
            models=[_difference(old.get(k), new.get(k)) for k in sorted(old.keys() | new.keys())], issues=issues))
    return dict(source=values[0]["snapshot"]["source"], measurement="observed_cumulative_difference",
                additive=False, round_attributable=False, round_totals=dict.fromkeys(usage.COUNTERS), windows=windows,
                limitations=["persistence_unverified", "intervening_reset_not_excluded", "round_ownership_unknown",
                             "overlapping_views", "cross_report_overlap_not_tracked", "hidden_calls_unknown"],
                note="Differences describe observed persisted values, not calls performed within the interval. "
                     "Do not sum session/model views, parent/child sessions or reports. Stable observations do not "
                     "prove flush completion; positive differences do not exclude a hidden reset. No import or costs.")
