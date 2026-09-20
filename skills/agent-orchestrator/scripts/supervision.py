#!/usr/bin/env python3
"""Read a run's supervision checkpoint; never approves, sends input or starts agents."""

import argparse
import math
from pathlib import Path
import sys
import time
import json
from typing import Any

import jobs
import protocol
import runs


def observer_expiry(path: str, watch_id: str, now: float) -> float:
    """Validate the observer's own lifetime record, separately from its lock."""
    data = protocol.read_json(Path(path).with_name("observer.json"))
    protocol.require(type(data.get("version")) is int and data["version"] == 1 and data.get("watch_id") == watch_id,
                     "observer identity changed")
    started, expires = data.get("started_at"), data.get("expires_at")
    for stamp in (started, expires):
        jobs.timestamp(stamp)
    protocol.require(started <= now and expires > started and data.get("stopped_at") is None,
                     "observer is stopped or its clock is inconsistent")
    return float(expires)


def coverage_for(state: dict[str, Any], watches: list[dict[str, Any]], now: float,
                 max_age: float, renew_before: float) -> dict[str, Any]:
    """Check the declared owner and exact round against independently read health."""
    monitor = state["monitor"]
    row: dict[str, Any] = {"job_id": state["job_id"], "round_id": state["round_id"],
                          "watch_path": monitor["watch_path"], "owner": monitor["owner"],
                          "issues": [], "remaining_seconds": None, "renew_by": None,
                          "last_success_age_seconds": None}
    issues = row["issues"]
    expiry = monitor["expires_at"]
    if monitor["owner"] is None or monitor["watch_path"] is None or expiry is None:
        issues.append("monitor_missing")
    if expiry is not None and expiry - now <= renew_before:
        issues.append("monitor_expired" if expiry <= now else "monitor_expiring")
    watcher = next((w for w in watches if w["watch_path"] == monitor["watch_path"]), None)
    if watcher is None:
        issues.append("watch_missing")
        return row
    config = protocol.read_json(watcher["watch_path"])
    pinned = next((t for t in config["targets"] if
                   (t["job_id"], t["round_id"]) == (state["job_id"], state["round_id"])), None)
    resource_ref = jobs.resources_for(state["active_request"])
    if pinned is None or resource_ref is None or pinned["resources"] != resource_ref["record"]:
        issues.append("resources_not_covered")
    if not watcher["observer_active"]:
        issues.append("observer_stopped")
    try:
        actual_expiry = observer_expiry(watcher["watch_path"], watcher["watch_id"], now)
        remaining = min(actual_expiry, expiry if expiry is not None else actual_expiry) - now
        row["remaining_seconds"] = max(0, remaining)
        row["renew_by"] = now + remaining - renew_before
        if actual_expiry - now <= renew_before:
            issues.append("observer_expired" if actual_expiry <= now else "observer_expiring")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        issues.append("observer_lifetime_unknown")
        row["observer_error"] = str(exc)
    target = next((t for t in watcher.get("targets", []) if
                   (t["job_id"], t["round_id"]) == (state["job_id"], state["round_id"])), None)
    if target is None:
        issues.append("round_not_covered")
    else:
        row["state"] = target.get("state", "unknown")
        if row["state"] in ("unknown", "dead"):
            issues.append("observation_" + row["state"])
        successful = target["last_successful_check_at"]
        if target.get("last_error"):
            issues.append("observation_failed")
        if successful is None:
            issues.append("observation_missing")
        else:
            age = now - successful
            row["last_success_age_seconds"] = age
            if age < 0 or age >= max_age:
                issues.append("observation_stale")
    return row


def check(run_path: str | Path, work_seconds: float = 15, max_age: float = 30,
          renew_before: float = 60, limit: int = 20, now: float | None = None) -> dict[str, Any]:
    """Bound the next work block using current queues, freshness and expiry.

    Reading is advisory: it neither wakes an ended controller nor guarantees that
    a live observer will stay healthy. Full recovery scanning remains unchanged.
    """
    for number in (work_seconds, max_age, renew_before):
        protocol.require(type(number) in (int, float) and math.isfinite(number) and number > 0,
                         "supervision intervals must be positive finite numbers")
    current = time.time() if now is None else now
    jobs.timestamp(current)
    recovered = runs.recover(run_path, limit=limit, now=current)
    if now is None:
        current = time.time()
    errors = list(recovered["errors"])
    coverage = []
    for item in recovered["jobs"]:
        if item.get("state") == "unreadable" or item.get("closed"):
            continue
        try:
            state = jobs.load(Path(recovered["run"]["index_path"]), item["job_id"])
            protocol.require(state["revision"] == item["revision"], "job changed during checkpoint; recheck")
            resources = jobs.resources_for(state["active_request"])
            if resources is not None or state["launch"] is not None or state["submission"] != "prepared":
                row = coverage_for(state, recovered["watches"], current, max_age, renew_before)
                if resources is None:
                    row["issues"].append("resources_missing")
                elif state["resources"] is not None and resources != state["resources"]:
                    row["issues"].append("resources_changed")
                coverage.append(row)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({"job_id": item["job_id"], "error": str(exc)})
    can_work = not errors and not recovered["counts"]["open"] and all(not c["issues"] for c in coverage)
    budget = min(work_seconds, 30.0, max_age)
    for row in coverage:
        if row["remaining_seconds"] is not None:
            budget = min(budget, max(0, row["remaining_seconds"] - renew_before))
        if row["last_success_age_seconds"] is not None:
            budget = min(budget, max(0, max_age - row["last_success_age_seconds"]))
    budget = budget if can_work else 0.0
    can_work = can_work and budget > 0
    keys = ("event_ref", "watch_path", "seq", "job_id", "round_id", "kind", "priority",
            "age_seconds", "overdue", "status", "revision", "evidence_path", "note", "is_current_round")
    def brief(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: item.get(k) for k in keys} for item in items]
    return {"run_path": recovered["run"]["run_path"], "checked_at": current,
            "can_work": can_work, "work_budget_seconds": budget, "recheck_by": current + budget,
            "coverage": coverage, "counts": recovered["counts"],
            "action_required": brief(recovered["action_required"]),
            "waiting_user": brief(recovered["waiting_user"]),
            "more_action_required": recovered["more_action_required"],
            "more_waiting_user": recovered["more_waiting_user"],
            "complete": not errors, "errors": errors, "executes_commands": False,
            "note": "Inspect pending evidence and live UI; approvals remain manual. Preserve waiting_user notes. "
                    "Renew an expiring observer before independent work; use manual checks during handoff. "
                    "No automatic wake-up, renewal, cancellation or health guarantee."}


def main() -> int:
    """Return nonzero when the controller must reconcile before independent work."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--work-seconds", type=float, default=15)
    parser.add_argument("--max-age", type=float, default=30)
    parser.add_argument("--renew-before", type=float, default=60)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    try:
        result = check(args.run, args.work_seconds, args.max_age, args.renew_before, args.limit)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if result["can_work"] else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc), "complete": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
