#!/usr/bin/env python3
"""Local submission index and recovery receipts. Never launches agents or sends input."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterator
import uuid

import protocol


ID = re.compile(r"[0-9a-f]{32}")
OUTCOMES = {"accepted": {"target_active", "matching_result"},
            "uncertain": {"timeout", "transport_error", "unconfirmed"},
            "rejected": {"not_sent"}}


def require_text(value: Any, label: str) -> None:
    """Require a nonempty text field, retaining ordinary Unicode."""
    protocol.require(isinstance(value, str) and bool(value.strip()) and "\x00" not in value,
                     f"invalid {label}")


def timestamp(value: Any) -> float:
    """Reject boolean, nonfinite and negative epoch values."""
    protocol.require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
                     "invalid timestamp")
    return float(value)


def clock(now: float | None) -> float:
    """Allow deterministic crash and expiry tests without sleeping."""
    return timestamp(time.time() if now is None else now)


def fingerprint(path: Path) -> str:
    """Pin file contents without placing terminal transcripts in index output."""
    return hashlib.sha256(protocol.read_bytes(path)).hexdigest()


def sync_directory(path: Path) -> None:
    """Persist new directory entries after atomically publishing a revision."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked(index: Path) -> Iterator[None]:
    """Serialize local index mutations; a process crash releases the kernel lock."""
    with (index / ".lock").open("a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise protocol.ProtocolError("index update in progress; recover before retrying") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def job_directory(index: Path, job: str) -> Path:
    """Never interpolate an unchecked job identifier into a filesystem path."""
    protocol.require(isinstance(job, str) and ID.fullmatch(job), "invalid job_id")
    return index / job


def request_record(path: str | Path) -> dict[str, Any]:
    """Bind an index entry to the complete current contract."""
    path = Path(path).resolve()
    request = protocol.load_request(path)
    return {"request_path": str(path), "round_id": request["round_id"],
            "request_sha256": fingerprint(path)}


def resources_for(path: str) -> dict[str, Any] | None:
    """Resources may be unknown at registration, before terminal allocation."""
    request_path = Path(path)
    resource_path = request_path.parent / "resources.json"
    if not os.path.lexists(resource_path):
        return None
    value = protocol.validate_resources(protocol.read_json(resource_path), protocol.load_request(path))
    return {"path": str(resource_path), "sha256": fingerprint(resource_path), "record": value}


def check_contract(state: dict[str, Any]) -> dict[str, Any]:
    """Fail closed if a pinned active contract or resource record was replaced."""
    path = Path(state["active_request"])
    import runs
    runs.check_index(path, state["index_path"])
    request = protocol.load_request(path)
    pinned = state["rounds"][-1]
    protocol.require(request["job_id"] == state["job_id"] and
                     request["round_id"] == state["round_id"] == pinned["round_id"] and
                     str(path) == pinned["request_path"] and
                     fingerprint(path) == pinned["request_sha256"], "active request changed")
    if state["resources"] is not None:
        protocol.require(resources_for(str(path)) == state["resources"], "resource record changed")
    binding = protocol.read_json(Path(state["rounds"][0]["request_path"]).parent / "job-index.json")
    protocol.require(binding == {"version": 1, "job_id": state["job_id"],
                                 "index_path": state["index_path"]}, "job index binding changed")
    return request


def load(index: Path, job: str) -> dict[str, Any]:
    """Read an immutable latest revision; missing/gapped/corrupt history is an error."""
    directory = job_directory(index, job)
    if not directory.is_dir():
        raise FileNotFoundError(f"job is not registered: {directory}")
    paths = sorted(directory.glob("*.json"))
    protocol.require(bool(paths), "job has no committed revision; retry registration")
    protocol.require(all(p.name == f"{i:012d}.json" for i, p in enumerate(paths, 1)),
                     "invalid or missing index revision")
    state = protocol.read_json(paths[-1])
    protocol.require(type(state.get("version")) is int and state["version"] == 1,
                     "unsupported job index version")
    protocol.require(type(state.get("revision")) is int and state["revision"] == len(paths) and
                     state.get("job_id") == job and state.get("index_path") == str(index),
                     "index identity/revision mismatch")
    protocol.require(state.get("submission") in ("prepared", *OUTCOMES) and
                     isinstance(state.get("attempts"), list) and
                     isinstance(state.get("rounds"), list) and bool(state["rounds"]),
                     "invalid job state")
    check_contract(state)
    return state


def save(index: Path, state: dict[str, Any], action: str, now: float) -> dict[str, Any]:
    """One committed file is both the current snapshot and immutable audit event."""
    state["revision"] += 1
    state["event"] = action
    state["recorded_at"] = now
    directory = job_directory(index, state["job_id"])
    directory.mkdir(mode=0o700, exist_ok=True)
    protocol.publish(directory / f'{state["revision"]:012d}.json', state)
    sync_directory(directory)
    sync_directory(index)
    return state


def register(index: str | Path, request_path: str, now: float | None = None) -> dict[str, Any]:
    """Register an unsent initial round once, before launching or submitting work."""
    current = clock(now)
    index = Path(index).resolve()
    record = request_record(request_path)
    request = protocol.load_request(record["request_path"])
    protocol.require("previous_request" not in request, "register the initial round; use activate for follow-ups")
    import runs
    runs.check_index(record["request_path"], index)
    index.mkdir(mode=0o700, parents=True, exist_ok=True)
    with locked(index):
        # Reserve the canonical index before committing. A lost registration reply
        # or a crash here can safely repeat registration in this same index.
        binding = {"version": 1, "job_id": request["job_id"], "index_path": str(index)}
        path = Path(record["request_path"]).parent / "job-index.json"
        try:
            protocol.publish(path, binding)
            sync_directory(path.parent)
        except FileExistsError:
            protocol.require(protocol.read_json(path) == binding, "job already bound to a different index")
        directory = job_directory(index, request["job_id"])
        if list(directory.glob("*.json")):
            state = load(index, request["job_id"])
            protocol.require(state["rounds"][0] == record, "initial request changed")
            return state
        protocol.require(not directory.exists(), "empty job history; reconcile missing revisions before registration")
        protocol.require(not os.path.lexists(request["result_path"]),
                         "register before submission; historical results require explicit reconciliation")
        state = {"version": 1, "revision": 0, "index_path": str(index),
                 "job_id": request["job_id"], "round_id": request["round_id"],
                 "active_request": record["request_path"], "rounds": [record],
                 "resources": resources_for(record["request_path"]),
                 "submission": "prepared", "attempts": [], "lease": None,
                 "launch": None, "native": {"session_id": None, "turn_id": None},
                 "monitor": {"owner": None, "watch_path": None, "expires_at": None},
                 "deadline": None, "closed": None, "completion": {}}
        return save(index, state, "register", current)


def expected(state: dict[str, Any], revision: int) -> None:
    """Require the revision the controller actually read, on every mutation."""
    protocol.require(type(revision) is int and revision == state["revision"],
                     "job revision conflict; recover before deciding")
    protocol.require(state["closed"] is None, "job is closed")


def duration(value: Any) -> float:
    """Bound a submission lease; expiry grants no permission to resend."""
    protocol.require(type(value) in (int, float) and math.isfinite(value) and 1 <= value <= 86400,
                     "lease_seconds must be between 1 and 86400")
    return float(value)


def claim(index: str | Path, job: str, revision: int, owner: str,
          lease_seconds: float = 300, now: float | None = None) -> dict[str, Any]:
    """Acquire an unowned/expired job with a fresh local fencing token."""
    current = clock(now)
    require_text(owner, "owner")
    expires_at = current + duration(lease_seconds)
    index = Path(index).resolve()
    with locked(index):
        state = load(index, job)
        expected(state, revision)
        protocol.require(state["lease"] is None or state["lease"]["expires_at"] <= current,
                         "job is owned by a live lease; reconcile with its controller")
        state["lease"] = {"owner": owner, "token": uuid.uuid4().hex, "expires_at": expires_at}
        return save(index, state, "claim", current)


def result_view(state: dict[str, Any]) -> dict[str, Any]:
    """Keep response validity separate from transport acceptance and verification."""
    request = check_contract(state)
    path = Path(request["result_path"])
    if not os.path.lexists(path):
        return {"status": "missing", "path": str(path)}
    try:
        raw = protocol.read_bytes(path)
        result = protocol.validate_result(request, protocol.parse_json(raw.decode("utf-8")))
        return {"status": result["status"], "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                "blocked_reason": result["blocked_reason"], "error": result["error"],
                "output": result["output"][:500]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "invalid", "path": str(path), "error": str(exc)}


def evidence(payload: dict[str, Any]) -> dict[str, Any]:
    """Retain an explicit evidence reference and its observed content fingerprint."""
    require_text(payload.get("note"), "evidence note")
    require_text(payload.get("evidence_path"), "evidence_path")
    path = Path(payload["evidence_path"]).resolve()
    return {"path": str(path), "sha256": fingerprint(path), "note": payload["note"]}


def target_key(resources: dict[str, Any]) -> tuple[Any, ...]:
    """Use real transport IDs, including the tmux server, never visible labels."""
    value = resources["record"]
    return (value["mode"] == "tmux", tuple(value.get("tmux_selector") or []),
            value["session"], value["pane"])


def begin(index: Path, state: dict[str, Any], now: float) -> None:
    """Persist uncertainty BEFORE external input, reserving one attempt and target."""
    protocol.require(state["submission"] in ("prepared", "rejected"),
                     "accepted/uncertain submission cannot be sent again; reconcile target evidence")
    protocol.require(result_view(state)["status"] == "missing", "result already exists; inspect it before input")
    resources = resources_for(state["active_request"])
    protocol.require(resources is not None, "record resources before beginning submission")
    for directory in sorted(index.iterdir()):
        if not directory.is_dir() or not ID.fullmatch(directory.name) or directory.name == state["job_id"]:
            continue
        other = load(index, directory.name)
        if other["closed"] is None and other["resources"] is not None:
            protocol.require(target_key(other["resources"]) != target_key(resources),
                             "target belongs to another open job in this index")
    state["resources"] = resources
    state["submission"] = "uncertain"
    state["attempts"].append({"attempt_id": uuid.uuid4().hex, "round_id": state["round_id"],
                              "owner": state["lease"]["owner"], "begun_at": now,
                              "status": "uncertain", "receipts": []})


def receipt(state: dict[str, Any], payload: dict[str, Any], now: float) -> None:
    """Resolve only the current attempt; timeout and missing output are never rejection."""
    protocol.require(bool(state["attempts"]), "begin an attempt before recording a receipt")
    attempt = state["attempts"][-1]
    protocol.require(attempt["round_id"] == state["round_id"] and
                     attempt["attempt_id"] == payload.get("attempt_id"), "attempt identity mismatch")
    status, kind = payload.get("status"), payload.get("kind")
    protocol.require(isinstance(status, str) and status in OUTCOMES and kind in OUTCOMES[status],
                     "receipt outcome requires matching evidence kind")
    protocol.require(attempt["status"] == "uncertain" or attempt["status"] == status == "accepted",
                     "resolved submission cannot change outcome")
    view = result_view(state)
    if kind == "matching_result":
        protocol.require(view["status"] in ("success", "error", "blocked"), "no matching valid result")
    if status == "rejected":
        protocol.require(view["status"] == "missing", "a result contradicts not_sent; reconcile it")
    item = {"status": status, "kind": kind, "recorded_at": now, "evidence": evidence(payload)}
    if kind == "matching_result":
        item["result"] = view
    attempt["receipts"].append(item)
    attempt["status"] = status
    state["submission"] = status


def update(state: dict[str, Any], payload: dict[str, Any]) -> None:
    """Attach explicit launch, native identity and monitoring facts; null means unknown."""
    protocol.require(bool(payload) and not payload.keys() - {"launch", "native", "monitor", "deadline"},
                     "unknown or empty metadata fields")
    if "launch" in payload:
        launch = payload["launch"]
        protocol.require(isinstance(launch, dict) and set(launch) == {"argv", "profile"}, "invalid launch metadata")
        protocol.require(isinstance(launch["argv"], list) and bool(launch["argv"]), "launch argv must be an array")
        require_text(launch["argv"][0], "launch executable")
        for arg in launch["argv"]:
            protocol.require(isinstance(arg, str) and "\x00" not in arg, "invalid launch argument")
        protocol.require(launch["profile"] is None or isinstance(launch["profile"], str), "invalid launch profile")
    if "native" in payload:
        native = payload["native"]
        protocol.require(isinstance(native, dict) and set(native) == {"session_id", "turn_id"}, "invalid native identity")
        for value in native.values():
            if value is not None:
                require_text(value, "native identity")
    if "monitor" in payload:
        monitor = payload["monitor"]
        protocol.require(isinstance(monitor, dict) and set(monitor) == {"owner", "watch_path", "expires_at"},
                         "invalid monitor metadata")
        if monitor["owner"] is not None:
            require_text(monitor["owner"], "monitor owner")
        if monitor["expires_at"] is not None:
            timestamp(monitor["expires_at"])
        if monitor["watch_path"] is not None:
            require_text(monitor["watch_path"], "watch path")
            path = Path(monitor["watch_path"]).resolve()
            config = protocol.read_json(path)
            protocol.require(type(config.get("version")) is int and config["version"] == 1 and
                             any(t.get("request_path") == state["active_request"] and
                                 t.get("job_id") == state["job_id"] and t.get("round_id") == state["round_id"]
                                 for t in config.get("targets", [])), "watch does not include active round")
            monitor["watch_path"] = str(path)
    if payload.get("deadline") is not None:
        timestamp(payload["deadline"])
    state.update(payload)


def activate(state: dict[str, Any], payload: dict[str, Any]) -> None:
    """Select a single direct successor; orphan prepared directories remain unsent."""
    require_text(payload.get("request_path"), "request_path")
    record = request_record(payload["request_path"])
    request = protocol.load_request(record["request_path"])
    import runs
    runs.check_index(record["request_path"], state["index_path"])
    protocol.require(request.get("previous_request") == state["active_request"],
                     "follow-up previous_request must be the active round")
    protocol.require(result_view(state)["status"] in ("success", "error", "blocked"),
                     "active round needs a valid result before follow-up")
    previous = protocol.load_request(state["active_request"])
    protocol.require(all(request[k] == previous[k] for k in ("job_id", "cwd", "depth", "max_depth")),
                     "follow-up must retain job, cwd and depth")
    protocol.require(record["round_id"] not in {r["round_id"] for r in state["rounds"]}, "round already indexed")
    protocol.require(not os.path.lexists(request["result_path"]), "activate follow-up before submitting it")
    resource = resources_for(record["request_path"])
    protocol.require(state["resources"] is None or
                     (resource is not None and resource["record"] == state["resources"]["record"]),
                     "follow-up resource identity changed")
    state.update(active_request=record["request_path"], round_id=record["round_id"],
                 resources=resource, submission="prepared")
    state["rounds"].append(record)
    state["native"]["turn_id"] = None
    state["monitor"] = {"owner": None, "watch_path": None, "expires_at": None}
    state["completion"] = {}


def change(index: str | Path, job: str, revision: int, token: str, action: str,
           payload: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Apply one versioned mutation while holding a live submission lease."""
    current = clock(now)
    protocol.require(isinstance(payload, dict), "payload must be an object")
    index = Path(index).resolve()
    with locked(index):
        state = load(index, job)
        expected(state, revision)
        lease = state["lease"]
        protocol.require(lease is not None and lease["token"] == token and lease["expires_at"] > current,
                         "invalid or expired submission lease; recover before deciding")
        if action == "begin":
            protocol.require(not payload, "begin takes no payload")
            begin(index, state, current)
        elif action == "receipt":
            receipt(state, payload, current)
        elif action == "update":
            update(state, payload)
        elif action == "activate":
            activate(state, payload)
        elif action in ("accept", "host"):
            import completion
            handler = completion.record_acceptance if action == "accept" else completion.record_host
            handler(state, payload, current)
        elif action == "renew":
            lease["expires_at"] = current + duration(payload.get("lease_seconds"))
        elif action == "release":
            state["lease"] = None
        elif action == "close":
            outcome = payload.get("outcome")
            protocol.require(outcome in ("completed", "failed", "cancelled"), "invalid closure outcome")
            if outcome == "completed":
                protocol.require(result_view(state)["status"] == "success", "completion needs a valid success response")
                import completion
                checked = completion.summarize(state, result_view(state), current)
                protocol.require(checked["ready_to_complete"], "completion conditions unmet: " + "; ".join(checked["reasons"]))
            state["closed"] = {"outcome": outcome, "recorded_at": current, "evidence": evidence(payload)}
            if outcome == "completed":
                state["closed"]["completion"] = checked
            state["lease"] = None
        else:
            raise protocol.ProtocolError("unknown index action")
        return save(index, state, action, current)


def recover(index: str | Path, job: str, now: float | None = None) -> dict[str, Any]:
    """Return local recovery facts without sending, claiming or adopting a result."""
    current = clock(now)
    state = load(Path(index).resolve(), job)
    result = result_view(state)
    import completion
    completion_status = completion.summarize(state, result, current)
    lease = state["lease"]
    expired = lease is None or lease["expires_at"] <= current
    if lease:
        # Lease tokens come only from a successful claim/renew/change reply.
        # This is local cooperative fencing, not an access-control boundary.
        lease.pop("token")
    if state["closed"] is not None:
        action = "closed"
    elif result["status"] in ("success", "error", "blocked"):
        action = "verify_response"
    elif result["status"] == "invalid":
        action = "inspect_invalid_response"
    elif state["submission"] == "uncertain":
        action = "reconcile_without_resending"
    elif state["submission"] == "accepted":
        action = "monitor_existing_attempt"
    else:
        action = "claim_and_check_target"
    expiry = state["monitor"]["expires_at"]
    return {"state": state, "result": result, "completion": completion_status,
            "next_action": action, "lease_expired": expired,
            "monitor_expired": None if expiry is None else expiry <= current,
            "history_directory": str(Path(index).resolve() / job), "executes_commands": False,
            "note": "Local receipts do not prove target readiness, live monitoring or exactly-once input. "
                    "Recheck the exact target before input; uncertainty survives lease expiry."}


def inventory(index: str | Path, now: float | None = None) -> dict[str, Any]:
    """Find registered jobs, preserving unreadable entries instead of hiding failures."""
    index = Path(index).resolve()
    items = []
    for directory in sorted(index.iterdir()):
        if not directory.is_dir() or not ID.fullmatch(directory.name):
            continue
        try:
            view = recover(index, directory.name, now)
            state = view["state"]
            items.append({key: state[key] for key in
                          ("job_id", "round_id", "revision", "active_request", "submission", "closed")} |
                         {key: view[key] for key in ("next_action", "lease_expired", "monitor_expired")} |
                         {"result_status": view["result"]["status"], "completion": view["completion"]})
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            items.append({"job_id": directory.name, "state": "unreadable", "error": str(exc)})
    return {"index_path": str(index), "jobs": items, "executes_commands": False}


def compact(state: dict[str, Any]) -> dict[str, Any]:
    """Keep CLI replies bounded to the active round/attempt, with history on disk."""
    attempts = [a for a in state["attempts"] if a["round_id"] == state["round_id"]]
    latest = [{**attempts[-1], "receipts": attempts[-1]["receipts"][-1:]}] if attempts else []
    return {**state, "rounds": state["rounds"][-1:], "attempts": latest,
            "round_count": len(state["rounds"]), "attempt_count": len(state["attempts"]),
            "history_directory": str(Path(state["index_path"]) / state["job_id"])}


def main() -> int:
    """Expose explicit index handles, CAS versions and structured evidence payloads."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    actions = ("register", "list", "recover", "claim", "begin", "receipt", "update", "activate",
               "renew", "release", "close", "accept", "host", "check-completion")
    for name in actions:
        command = commands.add_parser(name)
        command.add_argument("--index", required=True, help="one shared persistent index directory per run")
        if name == "register":
            command.add_argument("--request", required=True, help="unsent initial request.json")
        elif name != "list":
            command.add_argument("--job", required=True)
        if name == "recover":
            command.add_argument("--history", action="store_true", help="include all round/attempt metadata")
        if name not in ("register", "list", "recover", "check-completion"):
            command.add_argument("--expect-revision", required=True, type=int)
            if name == "claim":
                command.add_argument("--owner", required=True)
                command.add_argument("--lease-seconds", type=float, default=300)
            else:
                command.add_argument("--token", required=True, help="token returned by claim; never infer ownership")
                if name not in ("begin", "release"):
                    command.add_argument("--input", required=True, help="UTF-8 JSON payload; see submission reference")
    args = parser.parse_args()
    try:
        if args.command == "register":
            output = register(args.index, args.request)
        elif args.command == "list":
            output = inventory(args.index)
        elif args.command == "recover":
            output = recover(args.index, args.job)
        elif args.command == "check-completion":
            view = recover(args.index, args.job)
            output = {"job_id": args.job, "round_id": view["state"]["round_id"], **view["completion"]}
        elif args.command == "claim":
            output = claim(args.index, args.job, args.expect_revision, args.owner, args.lease_seconds)
        else:
            payload = protocol.read_json(args.input) if getattr(args, "input", None) else {}
            output = change(args.index, args.job, args.expect_revision, args.token, args.command, payload)
        if args.command == "recover":
            if not args.history:
                output["state"] = compact(output["state"])
        elif args.command not in ("list", "check-completion"):
            output = compact(output)
            if args.command == "register" and output["lease"] is not None:
                output["lease"] = {k: v for k, v in output["lease"].items() if k != "token"}
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        if args.command == "check-completion" and not output["ready_to_complete"]:
            return 1
        return 0
    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc), "kind": "missing"}), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print(json.dumps({"error": str(exc), "kind": "invalid"}), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
