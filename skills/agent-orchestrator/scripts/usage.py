#!/usr/bin/env python3
"""Immutable native-usage samples and honest per-job totals; no token estimates."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterator

import protocol


COUNTERS = ("input_tokens", "cached_input_tokens", "output_tokens")
PURPOSES = ("task", "retry", "report_repair")
DIMENSIONS = ("role", "phase")


def validate_dimensions(value: dict[str, Any]) -> None:
    """Role and work phase are independent of the reason for an attempt."""
    for field in DIMENSIONS:
        item = value.get(field)
        protocol.require(item is None or isinstance(item, str) and bool(item.strip()),
                         f"{field} must be a nonempty string or null")


def attribution(value: dict[str, Any]) -> tuple[Any, ...]:
    """Absent legacy dimensions compare as unknown, never as an inferred role."""
    return tuple(value.get(k) for k in ("purpose", *DIMENSIONS))


def validate_sample(sample: dict[str, Any]) -> dict[str, Any]:
    """Validate per-call deltas; input tokens must include their cached subset."""
    protocol.validate_json_values(sample)
    allowed = {"sample_id", "source", "attempt_id", "measurement", "purpose", "provider", "model",
               *COUNTERS, *DIMENSIONS, *protocol.IDENTITY_FIELDS}
    protocol.require(set(sample) <= allowed, "unknown usage sample fields")
    validate_dimensions(sample)
    for field in ("sample_id", "source", "attempt_id"):
        protocol.require(isinstance(sample.get(field), str) and sample[field].strip(),
                         f"{field} must be a nonempty string")
    protocol.require(sample.get("measurement") == "delta", "only per-call delta counts may be summed")
    protocol.require(sample.get("purpose") in PURPOSES, "invalid attempt purpose")
    for field in ("provider", "model"):
        protocol.require(field in sample and (sample[field] is None or
                         isinstance(sample[field], str)), f"{field} must be a string or null")
    for field in COUNTERS:
        protocol.require(field in sample and (sample[field] is None or
                         (type(sample[field]) is int and sample[field] >= 0)),
                         f"{field} must be a nonnegative integer or null")
    total, cached = sample["input_tokens"], sample["cached_input_tokens"]
    protocol.require(total is None or cached is None or cached <= total,
                     "cached_input_tokens is a subset of input_tokens")
    return sample


@contextmanager
def ledger_lock(directory: Path) -> Iterator[None]:
    """Serialize job-wide uniqueness and attempt checks with publication."""
    directory.mkdir(mode=0o700, exist_ok=True)
    with (directory / ".lock").open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def raw_stored_samples(chain: list[tuple[Path, dict[str, Any]]]) -> list[tuple[Path, dict[str, Any]]]:
    """Read job-wide calls and legacy per-round records without changing them."""
    paths = sorted((chain[0][0].parent / "usage" / "calls").glob("*.json"))
    for request_path, request in chain:
        for path in sorted((request_path.parent / "usage").glob("*.json")):
            entry = protocol.read_json(path)
            protocol.require(all(type(entry.get(key)) is type(request[key]) and
                             entry[key] == request[key] for key in protocol.IDENTITY_FIELDS),
                             "usage record identity mismatch")
            paths.append(path)
    entries = []
    for path in paths:
        entry = validate_sample(protocol.read_json(path))
        protocol.require(entry.get("job_id") == chain[0][1]["job_id"], "usage record identity mismatch")
        entries.append((path, entry))
    return entries


def stored_samples(chain: list[tuple[Path, dict[str, Any]]]) -> list[tuple[Path, dict[str, Any]]]:
    """Expose only the effective version of each native call; keep originals intact."""
    import usage_corrections
    state = usage_corrections.load_ledger(chain)
    return [(entry["path"], entry["sample"]) for entry in state["entries"].values()]


def record_usage(request_path: str, sample: dict[str, Any]) -> dict[str, Any]:
    """Commit one call atomically; deduplicate across all new rounds in a job."""
    chain = request_chain(request_path)
    request = chain[-1][1]
    validate_sample(sample)
    protocol.require(not any(key in sample for key in protocol.IDENTITY_FIELDS),
                     "sample identity is supplied by --request")
    entry = {**sample, **{key: request[key] for key in protocol.IDENTITY_FIELDS}}
    directory = chain[0][0].parent / "usage"
    with ledger_lock(directory):
        for existing_path, existing in stored_samples(chain):
            if (existing["source"], existing["sample_id"]) == (sample["source"], sample["sample_id"]):
                protocol.require(existing == entry, "usage sample conflicts with existing record or round")
                return {"path": str(existing_path), "job_id": request["job_id"],
                        "round_id": request["round_id"], "duplicate": True}
            if (existing["round_id"], existing["attempt_id"]) == (request["round_id"], sample["attempt_id"]):
                protocol.require(existing["purpose"] == sample["purpose"], "attempt purpose changed within a round")
                protocol.require(attribution(existing) == attribution(sample), "attempt attribution changed within a round")
        calls = directory / "calls"
        calls.mkdir(mode=0o700, exist_ok=True)
        key = hashlib.sha256(json.dumps([sample["source"], sample["sample_id"]]).encode()).hexdigest()
        path = calls / (key + ".json")
        # The call itself is the attempt metadata. Failure cannot reserve an
        # attempt, and a crash after publication is an idempotent retry.
        protocol.publish(path, entry)
    return {"path": str(path), "job_id": request["job_id"], "round_id": request["round_id"],
            "duplicate": False}


def request_chain(request_path: str) -> list[tuple[Path, dict[str, Any]]]:
    """Read the actual predecessor chain, rejecting cycles or cross-job links."""
    path = Path(request_path).resolve()
    job_id = protocol.load_request(path)["job_id"]
    chain: list[tuple[Path, dict[str, Any]]] = []
    seen: set[str] = set()
    while True:
        request = protocol.load_request(path)
        protocol.require(request["job_id"] == job_id, "previous request belongs to another job")
        protocol.require(request["round_id"] not in seen, "cycle or repeated round in request chain")
        seen.add(request["round_id"])
        chain.append((path, request))
        previous = request.get("previous_request")
        if previous is None:
            return list(reversed(chain))
        protocol.require(isinstance(previous, str) and Path(previous).is_absolute(),
                         "previous_request must be an absolute path")
        path = Path(previous).resolve()


def summarize(request_path: str) -> dict[str, Any]:
    """Aggregate known counters without turning missing telemetry into zero."""
    chain = request_chain(request_path)
    samples: list[dict[str, Any]] = []
    missing: list[str] = []
    seen: set[tuple[str, str]] = set()
    attempts: dict[tuple[str, str], str] = {}
    attributions: dict[tuple[str, str], tuple[Any, ...]] = {}
    entries = stored_samples(chain)
    for path, request in chain:
        round_entries = [entry for _, entry in entries if entry.get("round_id") == request["round_id"]]
        if not round_entries:
            missing.append(request["round_id"])
        for entry in round_entries:
            protocol.require(all(type(entry.get(key)) is type(request[key]) and
                             entry[key] == request[key] for key in protocol.IDENTITY_FIELDS),
                             "usage record identity mismatch")
            key = (entry["source"], entry["sample_id"])
            protocol.require(key not in seen, "native usage sample appears in multiple rounds/files")
            seen.add(key)
            attempt = (request["round_id"], entry["attempt_id"])
            protocol.require(attempt not in attempts or attempts[attempt] == entry["purpose"],
                             "attempt purpose changed within a round")
            attempts[attempt] = entry["purpose"]
            protocol.require(attempt not in attributions or attributions[attempt] == attribution(entry),
                             "attempt attribution changed within a round")
            attributions[attempt] = attribution(entry)
            samples.append(entry)
    known = {field: sum(sample[field] for sample in samples if sample[field] is not None)
             for field in COUNTERS}
    complete = {field: bool(samples) and not missing and
                all(sample[field] is not None for sample in samples) for field in COUNTERS}
    return {"job_id": chain[-1][1]["job_id"], "through_round": chain[-1][1]["round_id"],
            "rounds": len(chain), "samples": len(samples), "rounds_without_usage": missing,
            "known_subtotals": known,
            "totals": {field: known[field] if complete[field] else None for field in COUNTERS},
            "counter_coverage_complete": complete,
            "attempts": {purpose: sum(value == purpose for value in attempts.values())
                         for purpose in PURPOSES},
            "sources": sorted({sample["source"] for sample in samples}),
            "groups": group_samples(samples),
            "note": "Only effective recorded calls/attempts are counted; corrections preserve originals. Cached input is included in input; "
                    "do not add it again. Unknown counts are null. No pricing or tokenizer estimates."}


def group_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group known recorded calls without attributing missing calls to a role."""
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for sample in samples:
        groups.setdefault(attribution(sample), []).append(sample)
    result = []
    for (purpose, role, phase), entries in groups.items():
        known = {k: sum(e[k] for e in entries if e[k] is not None) for k in COUNTERS}
        result.append(dict(role=role, phase=phase, purpose=purpose, samples=len(entries),
                           known_subtotals=known,
                           totals={k: known[k] if all(e[k] is not None for e in entries) else None for k in COUNTERS}))
    return sorted(result, key=lambda g: json.dumps([g[k] for k in ("purpose", "role", "phase")]))


def summarize_project(manifest_path: str) -> dict[str, Any]:
    """Deduplicate explicit job chains and compare them with a declared source inventory."""
    return summarize_manifest(protocol.read_json(manifest_path))


def summarize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Use the same read-only aggregation for a file or a generated source inventory."""
    protocol.require(set(manifest) == {"version", "request_paths", "sources"} and
                     type(manifest["version"]) is int and manifest["version"] == 1,
                     "invalid project usage manifest")
    paths = manifest["request_paths"]
    protocol.require(isinstance(paths, list) and bool(paths), "request_paths must be a nonempty array")
    sources = manifest["sources"]
    protocol.require(isinstance(sources, list), "sources must be an array")
    expected: dict[str, set[str] | None] = {}
    for source in sources:
        protocol.require(isinstance(source, dict) and set(source) == {"source", "sample_ids"},
                         "invalid project source inventory")
        name, ids = source["source"], source["sample_ids"]
        protocol.require(isinstance(name, str) and bool(name.strip()) and name not in expected,
                         "invalid or duplicate source")
        protocol.require(ids is None or isinstance(ids, list) and
                         all(isinstance(k, str) and bool(k.strip()) for k in ids) and len(set(ids)) == len(ids),
                         "sample_ids must be unique strings or null")
        expected[name] = None if ids is None else set(ids)
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    owners: dict[tuple[str, str], list[dict[str, str]]] = {}
    files: set[Path] = set()
    rounds: set[tuple[str, str]] = set()
    missing: set[tuple[str, str]] = set()
    duplicates = 0
    for path in paths:
        protocol.require(isinstance(path, str) and Path(path).is_absolute(), "request paths must be absolute")
        report = summarize(path)  # Validate identities, purposes and legacy duplicates first.
        chain = request_chain(path)
        selected = {r["round_id"] for _, r in chain}
        rounds.update((r["job_id"], r["round_id"]) for _, r in chain)
        missing.update((report["job_id"], r) for r in report["rounds_without_usage"])
        for file, entry in stored_samples(chain):
            if entry["round_id"] not in selected or file.resolve() in files:
                continue
            files.add(file.resolve())
            key = (entry["source"], entry["sample_id"])
            comparable = {k: v for k, v in entry.items() if k not in protocol.IDENTITY_FIELDS}
            comparable.update({k: entry.get(k) for k in DIMENSIONS})
            if key in unique:
                previous = {k: v for k, v in unique[key].items() if k not in protocol.IDENTITY_FIELDS}
                previous.update({k: unique[key].get(k) for k in DIMENSIONS})
                protocol.require(previous == comparable, "conflicting native usage or attribution across jobs")
                duplicates += 1
            else:
                unique[key] = entry
            owners.setdefault(key, []).append({k: entry[k] for k in ("job_id", "round_id", "attempt_id")})
    samples = list(unique.values())
    observed: dict[str, set[str]] = {}
    for source, sample_id in unique:
        observed.setdefault(source, set()).add(sample_id)
    coverage = []
    for source in [*expected, *sorted(set(observed) - set(expected))]:
        ids, actual = expected.get(source), observed.get(source, set())
        coverage.append(dict(source=source, declared=source in expected, recorded_samples=len(actual),
                             expected_samples=None if ids is None else len(ids),
                             missing_sample_ids=None if ids is None else sorted(ids - actual),
                             unexpected_sample_ids=None if ids is None else sorted(actual - ids),
                             complete=ids is not None and ids == actual))
    source_complete = bool(coverage) and all(s["complete"] for s in coverage)
    known = {k: sum(s[k] for s in samples if s[k] is not None) for k in COUNTERS}
    complete = {k: bool(samples) and not missing and source_complete and
                all(s[k] is not None for s in samples) for k in COUNTERS}
    return dict(jobs=len({job for job, _ in rounds}), rounds=len(rounds), samples=len(samples), duplicates=duplicates,
                rounds_without_usage=[dict(job_id=j, round_id=r) for j, r in sorted(missing)],
                known_subtotals=known, totals={k: known[k] if complete[k] else None for k in COUNTERS},
                counter_coverage_complete=complete, source_coverage_complete=source_complete, sources=coverage,
                groups=group_samples(samples), duplicate_owners=[dict(source=k[0], sample_id=k[1], owners=v)
                                                                for k, v in owners.items() if len(v) > 1],
                note="Only declared request chains and native IDs are checked. Inventory completeness is caller-supplied; "
                     "aliases, hidden host calls, controller sessions and descendants are not discovered. "
                     "Groups cover recorded samples only. Cumulative snapshots are excluded; no pricing inferred.")


def main() -> int:
    """Run the usage ledger CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="ingest one normalized native delta sample")
    record.add_argument("--request", required=True)
    record.add_argument("--input", required=True)
    summary = commands.add_parser("summary", help="sum the job chain through this request")
    summary.add_argument("--request", required=True)
    project = commands.add_parser("project-summary", help="deduplicate explicit jobs and check source inventory")
    project.add_argument("--manifest", required=True)
    discover = commands.add_parser("discover-sources", help="list run actors and native candidates without binding")
    discover.add_argument("--run", required=True)
    audit = commands.add_parser("audit-sources", help="audit explicit actor/native bindings and usage gaps")
    audit.add_argument("--manifest", required=True)
    history = commands.add_parser("history", help="show original/effective sample and correction revision")
    history.add_argument("--request", required=True)
    history.add_argument("--source", required=True)
    history.add_argument("--sample-id", required=True)
    correct = commands.add_parser("correct", help="append an atomic group of evidence-backed corrections")
    correct.add_argument("--manifest", required=True)
    correct.add_argument("--dry-run", action="store_true")
    inspect = commands.add_parser("inspect-native", help="list native turn IDs and counters, without chat text")
    inspect.add_argument("--host", choices=("codex", "omp", "opencode", "hermes"), required=True)
    inspect.add_argument("--log", required=True)
    inspect.add_argument("--session-id", help="required for SQLite; optional identity check for single-session logs")
    ingest = commands.add_parser("import-native", help="import explicitly mapped native calls")
    ingest.add_argument("--manifest", required=True)
    ingest.add_argument("--dry-run", action="store_true", help="validate source/mappings without writing samples")
    snapshot = commands.add_parser("hermes-snapshot", help="publish a new immutable Hermes observation")
    snapshot.add_argument("--log", required=True)
    snapshot.add_argument("--session-id", required=True)
    snapshot.add_argument("--output", required=True)
    windows = commands.add_parser("hermes-windows", help="compare frozen adjacent observations; not ledger deltas")
    windows.add_argument("--manifest", required=True)
    auxiliary = commands.add_parser("hermes-aux", help="inspect unassigned auxiliary receipts; not round deltas")
    auxiliary.add_argument("--log", required=True)
    auxiliary.add_argument("--session-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "hermes-aux":
            import hermes_aux
            result = hermes_aux.inspect(args.log, args.session_id)
        elif args.command in ("hermes-snapshot", "hermes-windows"):
            import hermes_windows
            result = (hermes_windows.capture(args.log, args.session_id, args.output)
                      if args.command == "hermes-snapshot" else hermes_windows.compare(args.manifest))
        elif args.command in ("inspect-native", "import-native"):
            import native_usage
            result = (native_usage.inspect_log(args.host, args.log, args.session_id) if args.command == "inspect-native"
                      else native_usage.import_manifest(args.manifest, args.dry_run))
        elif args.command == "project-summary":
            result = summarize_project(args.manifest)
        elif args.command in ("discover-sources", "audit-sources"):
            import usage_sources
            result = (usage_sources.discover(args.run) if args.command == "discover-sources"
                      else usage_sources.audit(args.manifest))
        elif args.command in ("history", "correct"):
            import usage_corrections
            result = (usage_corrections.history(args.request, args.source, args.sample_id) if args.command == "history"
                      else usage_corrections.correct_manifest(args.manifest, args.dry_run))
        else:
            result = (record_usage(args.request, protocol.read_json(args.input))
                      if args.command == "record" else summarize(args.request))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc), "kind": "missing"}), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc), "kind": "invalid"}), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
