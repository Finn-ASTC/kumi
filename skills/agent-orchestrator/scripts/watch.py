#!/usr/bin/env python3
"""Read-only terminal supervision with persistent, compact events. Never sends input."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator
import uuid

import protocol


ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
# A mention of approval in a task, answer or tool output is not a prompt. Keep
# line-shaped cues for partial/unknown layouts; these remain suspicion, not proof.
PROMPT_CUE = re.compile(
    r"^\s*(?:[›❯>]\s*)?(?:"
    r"(?:allow once\s*/\s*deny|(?:允许一次|批准)\s*/\s*拒绝)\s*$|"
    r"\d+\.\s*(?:yes, proceed|allow once|approve|deny)\b|"
    r"(?:would you like to run|do you want to|trust (?:this|the))\b.*[?？]\s*$|"
    r"(?:permission required|requires approval|sign in|log in)\b|"
    r"(?:是否允许|请求权限|需要授权))", re.I)
# Match explicit frame starts, never normalize command arguments or numbers.
DIALOG_START = re.compile(r"^\s*(?:Command:|Would you like to run the following command\?)", re.I)
DIALOG_END = re.compile(r"^\s*allow once\s*/\s*deny\s*$|^\s*\d+\.\s*(?:No|Cancel)\b.*\(esc\)", re.I)
STATES = {"working", "idle", "done", "blocked", "unknown", "dead"}


def digest(value: Any) -> str:
    """Hash data locally rather than copying unchanged data into model context."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def atomic_save(path: Path, value: dict[str, Any]) -> None:
    """Replace controller state atomically; protocol results remain immutable."""
    fd, temporary = tempfile.mkstemp(prefix=".watch-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(directory: Path) -> Iterator[None]:
    """Allow one observer process per watch directory, including during a run."""
    with (directory / ".lock").open("a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise protocol.ProtocolError("watch already has an active observer") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def init_watch(request_paths: list[str], root: str | Path | None = None,
               tmux_selector: list[str] | None = None, deadline: float | None = None,
               review_interval: float = 30, stall_after: float = 120,
               run_path: str | None = None, temporary: bool = False) -> dict[str, Any]:
    """Pin explicit request/resource identities before allocating private watch files."""
    protocol.require(bool(request_paths), "at least one request is required")
    protocol.require(math.isfinite(review_interval) and review_interval > 0 and
                     math.isfinite(stall_after) and stall_after > 0, "invalid observation intervals")
    protocol.require(deadline is None or math.isfinite(deadline), "invalid deadline")
    if tmux_selector is not None:
        protocol.validate_tmux_selector(tmux_selector)
    targets = []
    seen: set[tuple[Any, ...]] = set()
    for request_path in request_paths:
        path = Path(request_path).resolve()
        raw = protocol.read_bytes(path)
        request = protocol.load_request(path, raw=raw)
        resources = protocol.validate_resources(protocol.read_json(path.parent / "resources.json"), request)
        selector = None
        if resources["mode"] == "tmux":
            selector = resources.get("tmux_selector", tmux_selector)
            protocol.validate_tmux_selector(selector)
            protocol.require(tmux_selector is None or selector == tmux_selector,
                             "tmux selector conflicts with resource record")
        key = (resources["mode"] == "tmux", tuple(selector or []), resources["session"], resources["pane"])
        protocol.require(key not in seen, "one active request per target is required")
        seen.add(key)
        targets.append({"request_path": str(path), "job_id": request["job_id"],
                        "round_id": request["round_id"], "resources": resources,
                        "request_sha256": hashlib.sha256(raw).hexdigest(),
                        "tmux_selector": selector})
    import runs
    root, run = runs.watch_storage(request_paths, root, run_path, temporary)
    directory = Path(tempfile.mkdtemp(prefix="orch-watch-", dir=root)).resolve()
    (directory / "events").mkdir(mode=0o700)
    (directory / "evidence").mkdir(mode=0o700)
    config = {"version": 1, "watch_id": uuid.uuid4().hex, "targets": targets, "deadline": deadline,
              "review_interval": review_interval, "stall_after": stall_after}
    protocol.publish(directory / "watch.json", config)
    atomic_save(directory / "state.json", {"cursor": 0, "targets": {}})
    if run is not None:
        runs.attach_watch(run["run_path"], directory / "watch.json")
    return {"watch_path": str(directory / "watch.json"), "watch_id": config["watch_id"],
            "run_path": run["run_path"] if run else None, "targets": len(targets), "cursor": 0}


def run_read(argv: list[str]) -> str:
    """Execute a bounded, explicit read command without a shell."""
    result = subprocess.run(argv, capture_output=True, text=True, timeout=4)
    if result.returncode:
        raise protocol.ProtocolError(f"{argv[0]} read failed: {result.stderr.strip()[:500]}")
    return result.stdout


def observe(target: dict[str, Any]) -> dict[str, Any]:
    """Read exact herdr or tmux identities and the terminal's bottom screen."""
    resources = target["resources"]
    pane = resources["pane"]
    if resources["mode"] == "tmux":
        cli = ["tmux", *target["tmux_selector"]]
        identity = run_read(cli + ["display-message", "-p", "-t", pane,
                                  "#{session_name}\t#{pane_id}\t#{pane_dead}"]).strip().split("\t")
        protocol.require(len(identity) == 3 and identity[:2] == [resources["session"], pane],
                         "tmux target identity missing or changed")
        # Only the visible screen: tmux can move cleared frames into scrollback.
        # Including history would revive old approvals on each full repaint.
        screen = run_read(cli + ["capture-pane", "-p", "-t", pane, "-S", "0"])
        state = "dead" if identity[2] == "1" else "unknown"
    else:
        cli = ["herdr", "--session", resources["session"]]
        data = json.loads(run_read(cli + ["agent", "get", pane]))
        protocol.require("error" not in data, "herdr returned an error envelope")
        agent = data.get("result", {}).get("agent", {})
        protocol.require(agent.get("pane_id") == pane and agent.get("name") == resources["agent"],
                         "herdr target identity missing or changed")
        state = agent.get("agent_status", "unknown")
        screen = run_read(cli + ["pane", "read", pane, "--source", "detection", "--lines", "80"])
    protocol.require(state in STATES, "unrecognized agent state")
    cleaned = ANSI.sub("", screen).replace("\r", "")
    bounded = "\n".join(cleaned.splitlines()[-80:])[-32000:]
    return {"state": state, "screen": bounded, "capture": {
        "source": "visible_screen" if resources["mode"] == "tmux" else "detection",
        "line_limit": 80, "character_limit": 32000,
        "locally_truncated": len(cleaned.splitlines()) > 80 or
            len("\n".join(cleaned.splitlines()[-80:])) > 32000,
        "completeness": "unknown"}}


def collect(target: dict[str, Any]) -> dict[str, Any]:
    """Keep response validity and transport observations independent."""
    started = time.monotonic()
    snapshot: dict[str, Any] = {"result": None, "read_started_at": time.time()}
    try:
        pinned = target.get("request_sha256")
        protocol.require(isinstance(pinned, str) and re.fullmatch(r"[0-9a-f]{64}", pinned),
                         "watch lacks a valid request pin; reconcile request and recreate watch; retain old pending reviews")
        raw_request = protocol.read_bytes(target["request_path"])
        protocol.require(hashlib.sha256(raw_request).hexdigest() == pinned, "watched request changed")
        request = protocol.load_request(target["request_path"], raw=raw_request)
        protocol.require(all(request[key] == target[key] for key in ("job_id", "round_id")),
                         "watched request identity changed")
        live_record = protocol.read_json(Path(target["request_path"]).parent / "resources.json")
        protocol.require(live_record == target["resources"], "watched resources changed; reconcile before resuming")
        try:
            result_path = Path(request["result_path"])
            result, raw = None, None
            if os.path.lexists(result_path):
                raw = protocol.read_bytes(result_path)
                try:
                    value = protocol.validate_result(request, protocol.parse_json(raw.decode("utf-8")))
                    result = {"valid": True, "value": value}
                except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                    result = {"valid": False, "error": str(exc)}
            publication = track_publication(Path(target["request_path"]), request, result, raw)
            snapshot.update(result=result, publication=publication)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            snapshot["file_error"] = str(exc)
        try:
            snapshot.update(observe(target))
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
            snapshot["transport_error"] = str(exc)
        errors = [f"{channel}: {snapshot[channel]}" for channel in ("file_error", "transport_error")
                  if channel in snapshot]
        if errors:
            snapshot["error"] = (snapshot.get("file_error", snapshot.get("transport_error"))
                                 if len(errors) == 1 else "; ".join(errors))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        snapshot["error"] = str(exc)
    snapshot.update(observed_at=time.time(), read_duration_seconds=time.monotonic() - started)
    return snapshot


def track_publication(request_path: Path, request: dict[str, Any],
                      result: dict[str, Any] | None, raw: bytes | None) -> dict[str, Any] | None:
    """Pin first observed valid bytes once per round, shared by successive watches.

    This is an observation receipt, not a write lock on the child's result. The
    atomic no-overwrite publication also arbitrates concurrent observers. Unseen
    writes between samples cannot be detected.
    """
    path = request_path.parent / "publication.json"
    current = hashlib.sha256(raw).hexdigest() if raw is not None else None
    if result is not None and result["valid"] and not os.path.lexists(path):
        assert raw is not None
        first = {"version": 1, "job_id": request["job_id"], "round_id": request["round_id"],
                 "request_fingerprint": digest(request), "observed_at": time.time(),
                 "sha256": current, "result_text": raw.decode("utf-8")}
        try:
            protocol.publish(path, first)
        except FileExistsError:
            pass  # Another watch won; validate and compare its receipt below.
    if not os.path.lexists(path):
        return None
    first = protocol.read_json(path)
    protocol.require(type(first.get("version")) is int and first["version"] == 1 and
                     all(first.get(k) == request[k] for k in ("job_id", "round_id")) and
                     first.get("request_fingerprint") == digest(request),
                     "publication receipt identity changed or invalid")
    text = first.get("result_text")
    protocol.require(isinstance(text, str) and
                     hashlib.sha256(text.encode("utf-8")).hexdigest() == first.get("sha256"),
                     "publication receipt content changed or invalid")
    protocol.validate_result(request, protocol.parse_json(text))
    stamp = first.get("observed_at")
    protocol.require(type(stamp) in (int, float) and math.isfinite(stamp),
                     "invalid publication observation time")
    return {"path": str(path), "first_sha256": first["sha256"], "current_sha256": current,
            "first_observed_at": stamp, "changed": current != first["sha256"]}


def dialog_key(screen: str, state: str) -> str | None:
    """Hash recognized frames or prompt-shaped lines, never arbitrary keywords.

    Frames keep every command line and option. Only text outside a positively
    delimited frame is excluded. This is association evidence, never approval.
    """
    lines = screen.splitlines()
    regions = []
    covered: set[int] = set()
    start = None
    for index, line in enumerate(lines):
        if start is None and DIALOG_START.search(line):
            start = index
        if start is not None and DIALOG_END.search(line):
            regions.append(lines[start:index + 1])
            covered.update(range(start, index + 1))
            start = None
    for index, line in enumerate(lines):
        if index not in covered and PROMPT_CUE.search(line):
            # Bare controls have no frame start. Retain preceding context while
            # excluding trailing progress, as for the legacy Allow/Deny shape.
            end = index + 1 if re.fullmatch(r"\s*allow once\s*/\s*deny\s*", line, re.I) else index + 9
            regions.append(lines[max(0, index - 8):end])
    return digest(regions) if regions else (digest(screen) if state == "blocked" else None)


def signals(snapshot: dict[str, Any], old: dict[str, Any], now: float,
            config: dict[str, Any]) -> list[tuple[str, str]]:
    """Emit changes, suspected dialogs and bounded fallbacks; never authorize actions."""
    events: list[tuple[str, str]] = []
    result = snapshot.get("result")
    publication = snapshot.get("publication")
    result_key = digest([result, publication])
    if publication and result_key != old.get("result_key") and (
            publication["changed"] or old.get("publication_changed")):
        message = ("Published result changed or disappeared; inspect the first publication and current evidence. "
                   if publication["changed"] else
                   "First publication bytes restored after a change; prior violation still requires review. ")
        events.append(("result_changed", message + publication["path"]))
    elif result is not None and result_key != old.get("result_key"):
        if result["valid"]:
            value = result["value"]
            events.append(("result", value["status"] + ": " + value["output"][:600]))
        else:
            events.append(("invalid_result", result["error"][:600]))
    # A failed read cannot establish that the published file disappeared.
    if result is not None or publication is not None or "error" not in snapshot:
        old["result_key"] = result_key
        old["publication_changed"] = bool(publication and publication["changed"])
    error = snapshot.get("error")
    if error != old.get("error"):
        events.append(("observation_error" if error else "observation_recovered", str(error or "reads recovered")[:600]))
    old["error"] = error
    ui_observed = "state" in snapshot and "screen" in snapshot
    if not ui_observed:
        # A blind interval cannot prove the same dialog stayed on screen.
        old["attention_key"] = None
        old.pop("state_only_blocked", None)
        old.pop("fallback_issue", None)
    if not error:
        old["last_successful_check_at"] = now
    if ui_observed:
        state, screen = snapshot["state"], snapshot["screen"]
        screen_key = digest(screen)
        if screen_key != old.get("screen_key"):
            old.update(screen_key=screen_key, changed_at=now, stalled=False)
        state_changed = state != old.get("state")
        if state_changed:
            events.append(("state_changed", state))
            old["state"] = state
        # Keep dialog_key's state-only fallback for conservative input guards,
        # but a host's stale blocked flag is not evidence of a visible dialog.
        attention_key = dialog_key(screen, "working")
        state_only_blocked = state == "blocked" and attention_key is None
        blocked_review = state_only_blocked and not old.get("state_only_blocked")
        old["state_only_blocked"] = state_only_blocked
        dialog_changed = attention_key != old.get("attention_key")
        if attention_key and dialog_changed:
            old["attention_issue"] = uuid.uuid4().hex
            events.append(("attention", "Suspected native dialog; inspect evidence before responding."))
        old["attention_key"] = attention_key
        if state_changed or dialog_changed or "fallback_issue" not in old:
            old.update(fallback_issue=uuid.uuid4().hex, fallback_delay=config["review_interval"],
                       surfaced_at=now, surfaced_key=screen_key)
        if blocked_review:
            events.append(("review_due", "Host reports blocked without a recognized dialog; inspect live UI."))
        if "surfaced_at" not in old:
            old.update(surfaced_at=now, surfaced_key=screen_key)
        delay = old.get("fallback_delay", config["review_interval"])
        if screen_key != old["surfaced_key"] and now - old["surfaced_at"] >= delay:
            events.append(("review_due", "Changed UI needs review; hook/keyword detection may miss a dialog."))
            old.update(surfaced_at=now, surfaced_key=screen_key,
                       fallback_delay=min(delay * 2, max(300, config["review_interval"])))
        if state in ("working", "unknown") and now - old["changed_at"] >= config["stall_after"] and not old.get("stalled"):
            events.append(("stalled", "No screen progress; inspect target and deadline."))
            old["stalled"] = True
    if config["deadline"] is not None and now >= config["deadline"] and not old.get("deadline_reported"):
        events.append(("deadline", "Observation deadline reached; target has not been cancelled."))
        old["deadline_reported"] = True
    old["last_checked_at"] = now
    return events


def sweep(path: Path, now: float | None = None) -> dict[str, Any]:
    """Publish completed reads immediately; caller holds the watch lock.

    ``now`` is a deterministic replay clock. Production uses each read's actual
    completion timestamp, including failures, independently of other targets.
    """
    directory = path.parent
    config = protocol.read_json(path)
    protocol.require(config.get("version") == 1, "unsupported watch version")
    state = protocol.read_json(directory / "state.json")
    existing = [int(p.stem) for name in ("events", "evidence")
                for p in (directory / name).glob("*.json")]
    state["cursor"] = max([state["cursor"], *existing])
    emitted = []
    with ThreadPoolExecutor(max_workers=min(8, len(config["targets"]))) as pool:
        futures = {pool.submit(collect, target): target for target in config["targets"]}
        for future in as_completed(futures):
            target, snapshot = futures[future], future.result()
            observed = snapshot.get("observed_at", time.time()) if now is None else now
            old = state["targets"].setdefault(target["round_id"], {})
            changes = signals(snapshot, old, observed, config)
            for field in ("read_started_at", "read_duration_seconds"):
                if field in snapshot:
                    old[field] = snapshot[field]
            for kind, message in changes:
                state["cursor"] += 1
                seq = state["cursor"]
                evidence = directory / "evidence" / f"{seq:012d}.json"
                protocol.publish(evidence, {"target": target, **snapshot, "observed_at": observed})
                event = {"seq": seq, "job_id": target["job_id"], "round_id": target["round_id"],
                         "kind": kind, "message": message, "evidence_path": str(evidence),
                         "evidence_sha256": hashlib.sha256(protocol.read_bytes(evidence)).hexdigest(),
                         "screen_tail": snapshot.get("screen", "")[-1000:], "observed_at": observed}
                if kind in ("attention", "review_due"):
                    key = old["attention_key"] if kind == "attention" else old["fallback_issue"]
                    event.update(issue_id=old["attention_issue" if kind == "attention" else "fallback_issue"],
                                 correlation_key=digest([target["job_id"], target["round_id"],
                                                         target["resources"], kind, key]),
                                 confidence=("suspected_dialog" if kind == "attention" else
                                             "unconfirmed_blocked" if old.get("state_only_blocked") else "unknown_ui"))
                protocol.publish(directory / "events" / f"{seq:012d}.json", event)
                emitted.append(event)
            # Per-target progress is durable even while another read is in flight.
            state["checked_at"] = observed
            atomic_save(directory / "state.json", state)
    return {"cursor": state["cursor"], "events": emitted, "checked_targets": len(config["targets"])}


def poll_watch(watch_path: str, now: float | None = None) -> dict[str, Any]:
    """Perform one sweep; unchanged healthy observations return an empty event list."""
    path = Path(watch_path).resolve()
    with locked(path.parent):
        return sweep(path, now)


def read_events(watch_path: str, after: int = 0, limit: int = 50) -> dict[str, Any]:
    """Read durable events after a consumer-owned cursor without probing terminals."""
    protocol.require(after >= 0 and 1 <= limit <= 200, "invalid event cursor/limit")
    directory = Path(watch_path).resolve().parent
    protocol.read_json(directory / "watch.json")
    paths = sorted(p for p in (directory / "events").glob("*.json") if int(p.stem) > after)
    events = [protocol.read_json(path) for path in paths[:limit]]
    return {"cursor": events[-1]["seq"] if events else after, "events": events,
            "more": len(paths) > limit}


def observer_active(directory: Path) -> bool:
    """Check the existing observer lock without creating it or waiting for its owner."""
    try:
        with (directory / ".lock").open("r") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        pass
    return False


def run_observer(watch_path: str, interval: float = 15, duration: float = 300,
                 stop_on_event: bool = False) -> dict[str, Any]:
    """Run with a durable planned expiry; this does not schedule a successor."""
    protocol.require(math.isfinite(interval) and interval > 0 and
                     math.isfinite(duration) and duration > 0,
                     "interval and duration must be positive finite numbers")
    path = Path(watch_path).resolve()
    with locked(path.parent):
        config = protocol.read_json(path)
        started_at = time.time()
        end = time.monotonic() + duration
        runtime = {"version": 1, "watch_id": config.get("watch_id"),
                   "instance_id": uuid.uuid4().hex, "pid": os.getpid(),
                   "started_at": started_at, "expires_at": started_at + duration,
                   "stopped_at": None, "reason": None}
        atomic_save(path.parent / "observer.json", runtime)
        reason = "failed"
        try:
            while True:
                started = time.monotonic()
                output = sweep(path)
                if output["events"]:
                    print(json.dumps(output, ensure_ascii=False), flush=True)
                    if stop_on_event:
                        reason = "stop_on_event"
                        break
                remaining = end - time.monotonic()
                if remaining <= 0:
                    reason = "duration_elapsed"
                    break
                time.sleep(min(remaining, max(0, interval - (time.monotonic() - started))))
        finally:
            runtime.update(stopped_at=time.time(), reason=reason)
            atomic_save(path.parent / "observer.json", runtime)
    return {"observer_stopped": True, "reason": reason, "watch_path": str(path),
            "cursor": output["cursor"], "targets_cancelled": False}


def wait_events(watch_path: str, after: int = 0, timeout: float = 30,
                limit: int = 50) -> dict[str, Any]:
    """Wait for durable events from an existing observer; never recapture or send input."""
    protocol.require(math.isfinite(timeout) and 0 <= timeout <= 60,
                     "wait timeout must be finite and between 0 and 60 seconds")
    directory = Path(watch_path).resolve().parent
    end = time.monotonic() + timeout
    while True:
        # Check the lock first: if the observer finishes during this iteration,
        # the subsequent read still drains everything it published before exit.
        active = observer_active(directory)
        output = read_events(watch_path, after, limit)
        remaining = end - time.monotonic()
        reason = ("events" if output["events"] else "observer_stopped" if not active else
                  "timeout" if remaining <= 0 else None)
        if reason:
            state = protocol.read_json(directory / "state.json")
            return {**output, "reason": reason, "observer_active": active,
                    "last_checked_at": state.get("checked_at")}
        time.sleep(min(0.1, remaining))


def main() -> int:
    """Run a bounded observer, or inspect its persistent events."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--request", action="append", required=True)
    storage = init.add_mutually_exclusive_group()
    storage.add_argument("--root", help="explicit watch parent; same-run requests remain registered")
    storage.add_argument("--run", help="explicit run.json; otherwise inferred from requests")
    storage.add_argument("--temporary", action="store_true", help="temporary standalone watch for legacy requests")
    selector = init.add_mutually_exclusive_group()
    selector.add_argument("--tmux-socket")
    selector.add_argument("--tmux-server")
    selector.add_argument("--tmux-default-server", action="store_true")
    init.add_argument("--deadline", help="ISO8601 with timezone; observation only")
    init.add_argument("--review-interval", type=float, default=30)
    init.add_argument("--stall-after", type=float, default=120)
    for name in ("poll", "run", "events", "wait"):
        command = commands.add_parser(name)
        command.add_argument("--watch", required=True)
        if name == "run":
            command.add_argument("--interval", type=float, default=15)
            command.add_argument("--duration", type=float, default=300)
            command.add_argument("--stop-on-event", action="store_true")
        if name in ("events", "wait"):
            command.add_argument("--after", type=int, default=0)
            command.add_argument("--limit", type=int, default=50)
        if name == "wait":
            command.add_argument("--timeout", type=float, default=30,
                                 help="wait up to 60 seconds for an existing observer's events")
    pending = commands.add_parser("pending", help="list unresolved reviews independently of delivery cursors")
    pending.add_argument("--watch", required=True)
    pending.add_argument("--limit", type=int, default=20)
    pending.add_argument("--overdue-after", type=float, default=30)
    pending.add_argument("--waiting-after", type=int, default=0, help="page waiting-user items by event sequence")
    pending.add_argument("--action-offset", type=int, default=0, help="browse open items; reset after queue changes")
    status = commands.add_parser("review-status", help="read latest review revision and locate immutable receipts")
    status.add_argument("--watch", required=True)
    status.add_argument("--seq", type=int, required=True)
    detail = commands.add_parser("detail", help="read full captured event evidence; never infer complete operation")
    detail.add_argument("--watch", required=True)
    detail.add_argument("--seq", type=int, required=True)
    review = commands.add_parser("review", help="record controller review state; never approves a native dialog")
    review.add_argument("--watch", required=True)
    review.add_argument("--seq", type=int, required=True)
    review.add_argument("--status", choices=("open", "waiting_user", "handled"), required=True)
    review.add_argument("--expected-revision", type=int, required=True)
    review.add_argument("--note-file", required=True, help="UTF-8 explanation of the review or pending decision")
    review.add_argument("--action-evidence", help="explicit file recording attempted input/action")
    review.add_argument("--resolution-evidence", help="explicit file recording verified native resolution")
    review.add_argument("--resolved-at", type=float, help="observed resolution time, Unix seconds")
    args = parser.parse_args()
    try:
        if args.command == "init":
            tmux_selector = (["-S", str(Path(args.tmux_socket).resolve())] if args.tmux_socket else
                             ["-L", args.tmux_server] if args.tmux_server else
                             [] if args.tmux_default_server else None)
            deadline = None
            if args.deadline:
                stamp = datetime.fromisoformat(args.deadline.replace("Z", "+00:00"))
                protocol.require(stamp.utcoffset() is not None, "deadline must include a timezone")
                deadline = stamp.timestamp()
            output = init_watch(args.request, args.root, tmux_selector, deadline,
                                args.review_interval, args.stall_after, args.run, args.temporary)
        elif args.command == "events":
            output = read_events(args.watch, args.after, args.limit)
        elif args.command == "wait":
            output = wait_events(args.watch, args.after, args.timeout, args.limit)
        elif args.command == "poll":
            output = poll_watch(args.watch)
        elif args.command in ("pending", "review", "review-status", "detail"):
            import reviews
            if args.command == "pending":
                output = reviews.pending(args.watch, args.limit, args.overdue_after,
                                         waiting_after=args.waiting_after, action_offset=args.action_offset)
            elif args.command == "review-status":
                output = reviews.review_status(args.watch, args.seq)
            elif args.command == "detail":
                output = reviews.event_detail(args.watch, args.seq)
            else:
                output = reviews.record_review(args.watch, args.seq, args.status,
                    Path(args.note_file).read_text(encoding="utf-8"), args.expected_revision,
                    action_evidence=args.action_evidence, resolution_evidence=args.resolution_evidence,
                    resolved_at=args.resolved_at)
        else:
            output = run_observer(args.watch, args.interval, args.duration, args.stop_on_event)
        print(json.dumps(output, ensure_ascii=False, allow_nan=False), flush=True)
        return 0
    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc), "kind": "missing"}), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc), "kind": "invalid"}), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
