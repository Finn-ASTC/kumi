"""Explicit, immutable run metering plans and resumable collection checkpoints."""

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import native_usage
import protocol
import runs
import usage
import usage_sources

ERRORS = (OSError, ValueError, KeyError, TypeError, RuntimeError)


def _digest(value: dict[str, Any]) -> str:
    """Identify the complete plan, independently of JSON formatting."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def _validate(plan: dict[str, Any]) -> dict[str, Any]:
    """Check declared ownership before any import; native availability is checked later."""
    protocol.require(
        set(plan) == {"version", "run_path", "bindings", "imports"}
        and type(plan["version"]) is int
        and plan["version"] == 1,
        "invalid metering plan fields/version",
    )
    run = runs.load_run(plan["run_path"])
    protocol.require(plan["run_path"] == run["run_path"], "use the exact run_path")
    inventory = usage_sources.discover(run["run_path"])
    protocol.require(inventory["complete"], "run inventory is incomplete")
    subjects = {s["subject_id"]: s for s in inventory["subjects"]}
    protocol.require(isinstance(plan["bindings"], list), "bindings must be an array")
    owners: dict[tuple[str, str, str], set[str]] = {}
    origins: dict[tuple[str, str], str] = {}
    pairs: set[tuple[str, str, str]] = set()
    for binding in plan["bindings"]:
        usage_sources._binding(binding)
        subject = binding["subject_id"]
        source = (binding["host"], binding["session_id"])
        protocol.require(
            subject in subjects or subject == "native:" + ":".join(source),
            "unknown metering subject",
        )
        pair = (subject, *source)
        protocol.require(pair not in pairs, "duplicate subject/source binding")
        pairs.add(pair)
        protocol.require(
            source not in origins or origins[source] == binding["log_path"],
            "same source bound to different log paths",
        )
        origins[source] = binding["log_path"]
        if subject in subjects and subjects[subject]["kind"] == "round":
            protocol.require(
                not binding.get("ledger_request_paths"),
                "round bindings use their registered request only",
            )
            scope = {subjects[subject]["request_path"]}
        else:
            scope = {
                str(path)
                for request in binding.get("ledger_request_paths", [])
                for path, _ in usage.request_chain(request)
            }
        for native_id in binding["native_ids"] or []:
            key = (*source, native_id)
            protocol.require(key not in owners, "conflicting native turn owners")
            owners[key] = scope

    protocol.require(isinstance(plan["imports"], list), "imports must be an array")
    selected: set[tuple[str, str, str]] = set()
    attempts: dict[tuple[str, str], tuple[Any, ...]] = {}
    for manifest in plan["imports"]:
        protocol.require(
            isinstance(manifest, dict)
            and set(manifest)
            == {"version", "host", "session_id", "log_path", "mappings"}
            and type(manifest["version"]) is int
            and manifest["version"] == 1,
            "invalid metering import manifest",
        )
        source = (manifest["host"], manifest["session_id"])
        protocol.require(
            source in origins and origins[source] == manifest["log_path"],
            "import source must match its evidence-backed binding",
        )
        protocol.require(
            isinstance(manifest["mappings"], list) and bool(manifest["mappings"]),
            "mappings must be a nonempty array",
        )
        for mapping in manifest["mappings"]:
            protocol.require(
                isinstance(mapping, dict)
                and set(mapping)
                == {
                    "native_id",
                    "request_path",
                    "attempt_id",
                    "purpose",
                    "role",
                    "phase",
                },
                "metering mappings require native identity, attempt, purpose, role and phase",
            )
            native_id = native_usage.nonempty(mapping["native_id"], "native_id")
            key = (*source, native_id)
            protocol.require(key not in selected, "duplicate metering native mapping")
            selected.add(key)
            protocol.require(
                key in owners and mapping["request_path"] in owners[key],
                "import mapping is outside the bound actor's ledger scope",
            )
            usage.request_chain(mapping["request_path"])
            attempt = native_usage.nonempty(mapping["attempt_id"], "attempt_id")
            protocol.require(
                mapping["purpose"] in usage.PURPOSES, "invalid attempt purpose"
            )
            usage.validate_dimensions(mapping)
            attempt_key = (mapping["request_path"], attempt)
            attribution = usage.attribution(mapping)
            protocol.require(
                attempt_key not in attempts or attempts[attempt_key] == attribution,
                "attempt attribution changed in metering plan",
            )
            attempts[attempt_key] = attribution
    return run


def _storage(run: dict[str, Any], name: str, create: bool = False) -> Path:
    """Keep plan/report publication within the pinned run, rejecting redirected storage."""
    root = Path(run["run_path"]).parent
    for path in (root / "metering", root / "metering" / name):
        protocol.require(not path.is_symlink(), "metering storage is redirected")
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        protocol.require(path.resolve() == path, "metering storage is redirected")
    return root / "metering" / name


def register(manifest_path: str) -> dict[str, Any]:
    """Retain an immutable plan; do not inspect native logs or collect any counters."""
    plan = protocol.read_json(manifest_path)
    run = _validate(plan)
    digest = _digest(plan)
    path = _storage(run, "plans", create=True) / (digest + ".json")
    runs.publish_once(path, plan)
    return dict(
        run_path=run["run_path"],
        plan_path=str(path),
        plan_digest=digest,
        bindings=len(plan["bindings"]),
        imports=len(plan["imports"]),
        collected=False,
    )


def _audit_manifest(plan: dict[str, Any]) -> dict[str, Any]:
    """Reuse the existing evidence and source audit without a second manifest file."""
    return {k: plan[k] for k in ("version", "run_path", "bindings")}


def _brief(audit: dict[str, Any] | None) -> dict[str, Any]:
    """Return actionable gaps and aggregates without echoing every native call ID."""
    if audit is None:
        return dict(
            inventory_complete=False,
            binding_coverage_complete=False,
            usage=None,
            unbound_subjects=None,
            issue_counts=None,
        )
    counts: dict[str, int] = {}
    for issue in audit["issues"]:
        counts[issue["code"]] = counts.get(issue["code"], 0) + 1
    summary = audit["usage"]
    return dict(
        inventory_complete=audit["inventory_complete"],
        binding_coverage_complete=audit["binding_coverage_complete"],
        unbound_subjects=audit["unbound_subjects"],
        issue_counts=counts,
        source_gaps=[
            dict(
                source=s["source"],
                recorded_samples=s["recorded_samples"],
                expected_samples=s["expected_samples"],
                missing_samples=None
                if s["missing_sample_ids"] is None
                else len(s["missing_sample_ids"]),
                unexpected_samples=None
                if s["unexpected_sample_ids"] is None
                else len(s["unexpected_sample_ids"]),
            )
            for s in summary.get("sources", [])
            if not s["complete"]
        ],
        usage={
            k: v
            for k, v in summary.items()
            if k not in ("sources", "duplicate_owners", "note")
        },
    )


def checkpoint(plan_path: str, label: str, dry_run: bool = False) -> dict[str, Any]:
    """Collect selected calls and retain gaps; retries reuse existing sample identities.

    This is a sequential observation, not an atomic native snapshot. A failed
    import may already have committed calls; the final audit reports actual state.
    """
    native_usage.nonempty(label, "checkpoint label")
    plan = protocol.read_json(plan_path)
    run = runs.load_run(plan["run_path"])
    digest = _digest(plan)
    expected = _storage(run, "plans") / (digest + ".json")
    protocol.require(
        Path(plan_path).absolute() == expected and not Path(plan_path).is_symlink(),
        "checkpoint requires an unchanged registered plan_path",
    )
    started = protocol.utc_now()
    errors: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    audit: dict[str, Any] | None = None
    try:
        _validate(plan)
        audit = usage_sources.audit_data(_audit_manifest(plan))
        protocol.require(
            not any(
                issue["code"]
                in {"wrong_round", "wrong_ledger_scope", "conflicting_turn_owners"}
                for issue in audit["issues"]
            ),
            "existing ledger ownership conflicts with plan; reconcile before collection",
        )
    except ERRORS as exc:
        errors.append(dict(stage="preflight", error=str(exc)))
    if not errors:
        for index, manifest in enumerate(plan["imports"]):
            try:
                report = native_usage.import_data(manifest, dry_run)
                imports.append(dict(index=index, **report))
            except ERRORS as exc:
                errors.append(dict(stage="import", index=index, error=str(exc)))
        if not dry_run:
            try:
                audit = usage_sources.audit_data(_audit_manifest(plan))
            except ERRORS as exc:
                errors.append(dict(stage="audit", error=str(exc)))
                audit = None  # Never present the pre-import audit as the final state.
    brief = _brief(audit)
    # A failed import/preflight invalidates a complete claim, even if old samples
    # happen to match the subsequently observed source.
    if errors and brief["usage"] is not None:
        brief["usage"] = {
            **brief["usage"],
            "totals": dict.fromkeys(usage.COUNTERS),
            "counter_coverage_complete": dict.fromkeys(usage.COUNTERS, False),
        }
        if "non_cached_input" in brief["usage"]:
            brief["usage"]["non_cached_input"] = {
                **brief["usage"]["non_cached_input"],
                "total": None,
            }
        brief["binding_coverage_complete"] = False
    result = dict(
        version=1,
        run_path=run["run_path"],
        plan_path=str(expected),
        plan_digest=digest,
        label=label,
        started_at=started,
        finished_at=protocol.utc_now(),
        dry_run=dry_run,
        collection_complete=not errors,
        errors=errors,
        import_counts_complete=not any(e["stage"] == "import" for e in errors),
        selected_calls=sum(r["selected_calls"] for r in imports),
        imported=sum(r["imported"] for r in imports),
        duplicates=sum(r["duplicates"] for r in imports),
        **brief,
        note="Cumulative checkpoint, not an interval delta or billing total. Collection success is not coverage. "
        "Groups cover recorded calls only. No background collection; native sources may advance between reads. "
        "Failed imports can be partial.",
    )
    if not dry_run:
        path = _storage(run, "checkpoints", create=True) / (uuid.uuid4().hex + ".json")
        protocol.publish(path, {**result, "imports": imports, "audit": audit})
        result["checkpoint_path"] = str(path)
    return result
