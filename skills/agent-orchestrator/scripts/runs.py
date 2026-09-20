#!/usr/bin/env python3
"""Persistent run storage and read-only recovery across immutable watch inventories."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any
import uuid

import protocol


ID = re.compile(r"[0-9a-f]{32}")


def state_root() -> Path:
    """Follow XDG state semantics; relative environment values are not valid roots."""
    value = os.environ.get("XDG_STATE_HOME", "")
    base = Path(value) if value and Path(value).is_absolute() else Path.home() / ".local/state"
    return base.resolve() / "agent-orchestrator"


def fingerprint(path: str | Path) -> str:
    """Pin immutable manifests without reading their contents into model context."""
    return hashlib.sha256(protocol.read_bytes(path)).hexdigest()


def publish_once(path: Path, value: dict[str, Any]) -> None:
    """Allow exact attachment retries while rejecting replacement identities."""
    try:
        protocol.publish(path, value)
    except FileExistsError:
        protocol.require(protocol.read_json(path) == value, "run membership identity changed")
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def init_run(root: str | Path | None = None, temporary: bool = False) -> dict[str, Any]:
    """Allocate an explicit run; no task, observer or controller is started."""
    protocol.require(not (root is not None and temporary), "choose --root or --temporary")
    base = Path(root).resolve() if root is not None else None if temporary else state_root() / "runs"
    if base is not None:
        if root is None:
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
        protocol.require(base.is_dir(), "run parent root must exist")
    directory = Path(tempfile.mkdtemp(prefix="orch-run-", dir=base)).resolve()
    for name in ("rounds", "watches", "jobs", "requests", "watch-registry"):
        (directory / name).mkdir(mode=0o700)
    run = {"version": 1, "run_id": uuid.uuid4().hex, "run_path": str(directory / "run.json"),
           "rounds_root": str(directory / "rounds"), "watches_root": str(directory / "watches"),
           "index_path": str(directory / "jobs"), "temporary": temporary,
           "created_at": protocol.utc_now()}
    publish_once(directory / "run.json", run)
    return run


def load_run(run_path: str | Path) -> dict[str, Any]:
    """Reject relocated/redirected run manifests; contracts contain absolute paths."""
    path = Path(run_path).resolve()
    run = protocol.read_json(path)
    protocol.require(path.name == "run.json" and type(run.get("version")) is int and run["version"] == 1,
                     "invalid run manifest/version")
    protocol.require(isinstance(run.get("run_id"), str) and ID.fullmatch(run["run_id"]), "invalid run identity")
    protocol.require(run.get("run_path") == str(path) and type(run.get("temporary")) is bool,
                     "run path/temporary flag changed")
    for key, name in (("rounds_root", "rounds"), ("watches_root", "watches"), ("index_path", "jobs")):
        protocol.require(run.get(key) == str(path.parent / name), "run storage path changed")
        protocol.require((path.parent / name).is_dir() and (path.parent / name).resolve() == path.parent / name,
                         "run storage directory missing or redirected")
    for name in ("requests", "watch-registry"):
        protocol.require((path.parent / name).is_dir() and (path.parent / name).resolve() == path.parent / name,
                         "run registry directory missing or redirected")
    return run


def track_request(run_path: str | Path, request_path: str | Path) -> None:
    """Bind a newly prepared round without claiming that it has been submitted."""
    run = load_run(run_path)
    path = Path(request_path).resolve()
    request = protocol.load_request(path)
    protocol.require(path.parent.parent == Path(run["rounds_root"]), "request is outside this run's round storage")
    reference = {"version": 1, "run_id": run["run_id"], "run_path": run["run_path"],
                 "job_id": request["job_id"], "round_id": request["round_id"]}
    publish_once(path.parent / "run-ref.json", reference)
    entry = {**reference, "request_path": str(path), "request_sha256": fingerprint(path)}
    publish_once(Path(run["run_path"]).parent / "requests" / (request["round_id"] + ".json"), entry)


def run_for_request(request_path: str | Path) -> dict[str, Any] | None:
    """Infer membership only from pinned local records, never a cwd or session name."""
    path = Path(request_path).resolve()
    reference_path = path.parent / "run-ref.json"
    if not os.path.lexists(reference_path):
        possible = path.parent.parent.parent / "run.json"
        protocol.require(not (path.parent.parent.name == "rounds" and possible.exists()),
                         "managed request has missing run membership; reconcile before continuing")
        return None
    reference = protocol.read_json(reference_path)
    run = load_run(reference["run_path"])
    request = protocol.load_request(path)
    expected = {"version": 1, "run_id": run["run_id"], "run_path": run["run_path"],
                "job_id": request["job_id"], "round_id": request["round_id"]}
    protocol.require(reference == expected and path.parent.parent == Path(run["rounds_root"]),
                     "request run membership changed")
    member_path = Path(run["run_path"]).parent / "requests" / (request["round_id"] + ".json")
    protocol.require(member_path.is_file(), "request run membership is missing")
    protocol.require(protocol.read_json(member_path) == {**expected, "request_path": str(path),
                     "request_sha256": fingerprint(path)}, "request run membership changed")
    return run


def check_index(request_path: str | Path, index: str | Path) -> None:
    """Managed contracts and their index must share the same pinned run."""
    index = Path(index).resolve()
    run = run_for_request(request_path)
    if run is not None:
        protocol.require(run["index_path"] == str(index), "managed job must use its run index")
    elif index.name == "jobs" and (index.parent / "run.json").exists():
        load_run(index.parent / "run.json")
        raise protocol.ProtocolError("run index cannot adopt an unmanaged request")


def round_storage(root: str | Path | None, run_path: str | Path | None, temporary: bool,
                  previous: str | Path | None) -> tuple[str | Path, dict[str, Any] | None]:
    """Choose persistent defaults while retaining explicit legacy --root behavior."""
    protocol.require(sum((root is not None, run_path is not None, temporary)) <= 1,
                     "choose only one of --root, --run, --temporary")
    inherited = run_for_request(previous) if previous else None
    if inherited:
        protocol.require(not temporary, "follow-up inherits its run; do not change storage")
        if root is not None:
            protocol.require(Path(root).resolve() == Path(inherited["rounds_root"]), "follow-up inherits run storage")
        if run_path is not None:
            protocol.require(load_run(run_path) == inherited, "follow-up cannot switch runs")
        return inherited["rounds_root"], inherited
    if previous:
        protocol.require(run_path is None and not temporary, "legacy follow-up retains legacy storage; do not adopt history")
        return root if root is not None else Path(previous).resolve().parent.parent, None
    if root is not None:
        base = Path(root).resolve()
        if base.name == "rounds" and (base.parent / "run.json").exists():
            run = load_run(base.parent / "run.json")
            return base, run
        return root, None
    run = load_run(run_path) if run_path is not None else init_run(temporary=temporary)
    return run["rounds_root"], run


def watch_storage(request_paths: list[str], root: str | Path | None,
                  run_path: str | Path | None, temporary: bool) -> tuple[str | Path | None, dict[str, Any] | None]:
    """A managed watch belongs to exactly one run; explicit standalone roots still work."""
    protocol.require(sum((root is not None, run_path is not None, temporary)) <= 1,
                     "choose only one of --root, --run, --temporary")
    memberships = [run_for_request(p) for p in request_paths]
    present = [m for m in memberships if m is not None]
    shared = present[0] if present and len(present) == len(memberships) and all(m == present[0] for m in present) else None
    if run_path is not None:
        selected = load_run(run_path)
        protocol.require(shared == selected, "watch requests must belong to the selected run")
        return selected["watches_root"], selected
    if root is not None:
        return root, shared
    if temporary:
        protocol.require(not present, "managed watches inherit run retention; use an explicit root to override")
        return None, None
    if shared:
        return shared["watches_root"], shared
    protocol.require(not present, "mixed runs require an explicit standalone watch --root")
    base = state_root() / "watches"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    return base, None


def attach_watch(run_path: str | Path, watch_path: str | Path) -> dict[str, Any]:
    """Retain every replacement watch, including the old watch's unresolved reviews."""
    run = load_run(run_path)
    path = Path(watch_path).resolve()
    config = protocol.read_json(path)
    protocol.require(path.name == "watch.json" and type(config.get("version")) is int and config["version"] == 1,
                     "invalid watch manifest")
    protocol.require(isinstance(config.get("targets"), list) and bool(config["targets"]), "empty watch inventory")
    for target in config["targets"]:
        protocol.require(run_for_request(target["request_path"]) == run, "watch contains a foreign run request")
        request = protocol.load_request(target["request_path"])
        protocol.require(all(target.get(k) == request[k] for k in protocol.IDENTITY_FIELDS[1:]),
                         "watch request identity mismatch")
        resources = protocol.read_json(Path(target["request_path"]).parent / "resources.json")
        protocol.require(protocol.validate_resources(resources, request) == target["resources"],
                         "watch resources changed")
    watch_id = config.get("watch_id", hashlib.sha256(str(path).encode()).hexdigest()[:32])
    protocol.require(isinstance(watch_id, str) and ID.fullmatch(watch_id), "invalid watch_id")
    entry = {"version": 1, "watch_id": watch_id, "watch_path": str(path),
             "config_sha256": fingerprint(path), "run_id": run["run_id"]}
    publish_once(Path(run["run_path"]).parent / "watch-registry" / (watch_id + ".json"), entry)
    return entry


def list_runs(root: str | Path | None = None) -> dict[str, Any]:
    """Discover manifests without creating default directories or moving old runs."""
    base = Path(root).resolve() if root is not None else state_root() / "runs"
    entries = []
    for path in sorted(base.glob("*/run.json")):
        try:
            entries.append(load_run(path))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            entries.append({"run_path": str(path), "error": str(exc)})
    return {"root": str(base), "runs": entries, "executes_commands": False}


def recover(run_path: str | Path, limit: int = 20, action_offset: int = 0,
            waiting_offset: int = 0, job_id: str | None = None,
            now: float | None = None) -> dict[str, Any]:
    """Aggregate pinned queues with global event identities; never inspect a terminal."""
    import jobs
    import reviews
    import watch

    protocol.require(type(limit) is int and 1 <= limit <= 200, "invalid recovery limit")
    protocol.require(all(type(v) is int and v >= 0 for v in (action_offset, waiting_offset)), "invalid queue offset")
    protocol.require(job_id is None or (isinstance(job_id, str) and ID.fullmatch(job_id)), "invalid job filter")
    run = load_run(run_path)
    directory = Path(run["run_path"]).parent
    errors: list[dict[str, str]] = []
    rounds, watchers, action, waiting = [], [], [], []
    review_history: list[dict[str, Any]] = []
    known_requests: set[str] = set()
    known_watches: set[str] = set()
    inventory = jobs.inventory(run["index_path"], now)["jobs"]
    active = {j["job_id"]: j["round_id"] for j in inventory if "round_id" in j}
    for job in inventory:
        if job.get("state") == "unreadable":
            errors.append({"path": str(Path(run["index_path"]) / job["job_id"]), "error": job["error"]})
    for member in sorted((directory / "requests").glob("*.json")):
        try:
            record = protocol.read_json(member)
            known_requests.add(record["request_path"])
            protocol.require(member.name == record["round_id"] + ".json" and
                             run_for_request(record["request_path"]) == run, "round membership changed")
            if job_id is None or record["job_id"] == job_id:
                rounds.append({k: record[k] for k in ("request_path", "job_id", "round_id")} |
                              {"indexed_job": record["job_id"] in active,
                               "is_current_round": active.get(record["job_id"]) == record["round_id"]
                               if record["job_id"] in active else None})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({"path": str(member), "error": str(exc)})
    for member in sorted((directory / "watch-registry").glob("*.json")):
        try:
            entry = protocol.read_json(member)
            known_watches.add(entry["watch_path"])
            protocol.require(type(entry.get("version")) is int and entry["version"] == 1 and
                             entry.get("run_id") == run["run_id"] and
                             member.name == entry["watch_id"] + ".json", "watch membership changed")
            path = Path(entry["watch_path"])
            protocol.require(path.is_absolute() and path.name == "watch.json" and path.resolve() == path,
                             "watch manifest path changed")
            protocol.require(fingerprint(path) == entry["config_sha256"], "registered watch manifest changed")
            config = protocol.read_json(path)
            watch_id = config.get("watch_id", hashlib.sha256(str(path).encode()).hexdigest()[:32])
            protocol.require(isinstance(watch_id, str) and ID.fullmatch(watch_id) and watch_id == entry["watch_id"],
                             "watch identity changed")
            for target in config["targets"]:
                protocol.require(run_for_request(target["request_path"]) == run, "watch target lost run membership")
            identities = {(t["job_id"], t["round_id"]) for t in config["targets"]
                          if job_id is None or t["job_id"] == job_id}
            if not identities:
                continue
            opened, unanswered = reviews.pending_items(str(path), now=now, identities=identities, reviewed=review_history)
            for destination, source in ((action, opened), (waiting, unanswered)):
                for item in source:
                    destination.append({**item, "watch_path": str(path), "watch_id": entry["watch_id"],
                                        "event_ref": f'{entry["watch_id"]}:{item["seq"]}',
                                        "is_current_round": active.get(item["job_id"]) == item["round_id"]
                                        if item["job_id"] in active else None})
            health = {"watch_id": entry["watch_id"], "watch_path": str(path),
                      "observer_active": watch.observer_active(path.parent)}
            try:
                state = protocol.read_json(path.parent / "state.json")
                protocol.require(isinstance(state.get("targets"), dict), "invalid watch health targets")
                if state.get("checked_at") is not None:
                    reviews.timestamp(state["checked_at"])
                for _, round_id in identities:
                    health_record = state["targets"].get(round_id, {})
                    protocol.require(isinstance(health_record, dict), "invalid target health record")
                    for field in ("last_checked_at", "last_successful_check_at"):
                        if health_record.get(field) is not None:
                            reviews.timestamp(health_record[field])
                health.update(last_checked_at=state.get("checked_at"), targets=[
                    {"job_id": job, "round_id": round_id,
                     "last_checked_at": state.get("targets", {}).get(round_id, {}).get("last_checked_at"),
                     "last_successful_check_at": state.get("targets", {}).get(round_id, {}).get("last_successful_check_at"),
                     "last_error": state.get("targets", {}).get(round_id, {}).get("error"),
                     "state": state.get("targets", {}).get(round_id, {}).get("state", "unknown"),
                     "read_started_at": state.get("targets", {}).get(round_id, {}).get("read_started_at"),
                     "read_duration_seconds": state.get("targets", {}).get(round_id, {}).get("read_duration_seconds")}
                    for job, round_id in sorted(identities)])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                health.update(last_checked_at=None, targets=[], error=str(exc))
                errors.append({"path": str(path.parent / "state.json"), "error": str(exc)})
            watchers.append(health)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({"path": str(member), "error": str(exc)})
    # A failed attachment can leave a fully prepared artifact. Discover its
    # presence without adopting it or treating an empty registry as complete.
    for root, filename, known in ((run["rounds_root"], "request.json", known_requests),
                                  (run["watches_root"], "watch.json", known_watches)):
        for child in sorted(Path(root).iterdir()):
            if child.is_dir():
                path = str(child / filename)
                if path not in known:
                    errors.append({"path": path, "error": "unregistered run artifact; reconcile or retry its attachment"})
    reviews.attach_related(action + waiting, review_history)
    action.sort(key=lambda e: (e["priority"], e["observed_at"], e["event_ref"]))
    waiting.sort(key=lambda e: (e["observed_at"], e["event_ref"]))
    page = action[action_offset:action_offset + limit]
    waiting_page = waiting[waiting_offset:waiting_offset + limit]
    return {"run": run, "jobs": [j for j in inventory if job_id is None or j["job_id"] == job_id],
            "rounds": rounds, "watches": watchers, "action_required": page, "waiting_user": waiting_page,
            "counts": {"open": len(action), "waiting_user": len(waiting)},
            "more_action_required": len(action) > action_offset + limit,
            "next_action_offset": action_offset + len(page),
            "more_waiting_user": len(waiting) > waiting_offset + limit,
            "next_waiting_offset": waiting_offset + len(waiting_page),
            "complete": not errors, "errors": errors, "executes_commands": False,
            "note": "Read existing waiting_user notes before asking again. Never transfer approval by seq/text. "
                    "Recheck live identities before input. Observer lock snapshots do not prove healthy monitoring; "
                    "use per-target last_successful_check_at. Queue offsets reset after changes."}


def main() -> int:
    """Manage run handles and read compact recovery queues without terminal I/O."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    storage = init.add_mutually_exclusive_group()
    storage.add_argument("--root", help="existing parent directory; defaults to XDG persistent state")
    storage.add_argument("--temporary", action="store_true", help="explicit short-lived system-temp run")
    listing = commands.add_parser("list")
    listing.add_argument("--root", help="parent directory used by init")
    attach = commands.add_parser("attach-watch", help="retain a watch of this run's pinned requests")
    attach.add_argument("--run", required=True)
    attach.add_argument("--watch", required=True)
    attach_request = commands.add_parser("attach-request", help="retry tracking a reconciled round inside this run")
    attach_request.add_argument("--run", required=True)
    attach_request.add_argument("--request", required=True)
    recovering = commands.add_parser("recover")
    recovering.add_argument("--run", required=True)
    recovering.add_argument("--job")
    recovering.add_argument("--limit", type=int, default=20)
    recovering.add_argument("--action-offset", type=int, default=0)
    recovering.add_argument("--waiting-offset", type=int, default=0)
    args = parser.parse_args()
    try:
        if args.command == "init":
            value = init_run(args.root, args.temporary)
        elif args.command == "list":
            value = list_runs(args.root)
        elif args.command == "attach-watch":
            value = attach_watch(args.run, args.watch)
        elif args.command == "attach-request":
            track_request(args.run, args.request)
            value = {"run_path": str(Path(args.run).resolve()), "request_path": str(Path(args.request).resolve()),
                     "submission_inferred": False, "executes_commands": False}
        else:
            value = recover(args.run, args.limit, args.action_offset, args.waiting_offset, args.job)
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return 0
    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc), "kind": "missing"}), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc), "kind": "invalid"}), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
