"""Durable controller review receipts. Bookkeeping only; never approves or sends input."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import re
from pathlib import Path
import time
from typing import Any, Iterator

import protocol


STATUSES = ("open", "waiting_user", "handled")
KINDS = {"attention", "review_due", "result", "result_changed", "invalid_result", "state_changed",
         "observation_error", "observation_recovered", "stalled", "deadline"}


def timestamp(value: Any) -> None:
    """Validate persisted times without treating booleans as epochs."""
    protocol.require(type(value) in (int, float) and math.isfinite(value), "invalid review timestamp")


def fingerprint(event: dict[str, Any]) -> str:
    """Bind a receipt to the complete immutable event, including evidence identity."""
    return hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def context(watch_path: str) -> tuple[Path, set[tuple[str, str]]]:
    """Use only the pinned watch inventory, including for legacy watch directories."""
    directory = Path(watch_path).resolve().parent
    config = protocol.read_json(directory / "watch.json")
    protocol.require(type(config.get("version")) is int and config["version"] == 1,
                     "unsupported watch version")
    return directory, {(target["job_id"], target["round_id"]) for target in config["targets"]}


def load_event(directory: Path, identities: set[tuple[str, str]], seq: int) -> dict[str, Any]:
    """Reject a replaced, misnamed or foreign event before interpreting its review state."""
    protocol.require(type(seq) is int and seq > 0, "event seq must be a positive integer")
    event = protocol.read_json(directory / "events" / f"{seq:012d}.json")
    protocol.require(type(event.get("seq")) is int and event["seq"] == seq, "event sequence mismatch")
    protocol.require((event.get("job_id"), event.get("round_id")) in identities,
                     "event does not belong to the pinned watch")
    protocol.require(event.get("kind") in KINDS and isinstance(event.get("message"), str),
                     "invalid event kind/message")
    protocol.require(event.get("evidence_path") == str(directory / "evidence" / f"{seq:012d}.json"),
                     "event evidence identity mismatch")
    timestamp(event.get("observed_at"))
    for field, length in (("issue_id", 32), ("correlation_key", 64)):
        if field in event:
            protocol.require(isinstance(event[field], str) and
                             re.fullmatch(r"[0-9a-f]{%d}" % length, event[field]), "invalid event association")
    return event


def latest_review(directory: Path, event: dict[str, Any]) -> dict[str, Any]:
    """Validate the immutable receipt chain; no receipt means unreviewed revision zero."""
    latest: dict[str, Any] = {"revision": 0, "status": "open", "note": None, "recorded_at": None}
    event_key = fingerprint(event)
    for path in sorted((directory / "reviews" / f'{event["seq"]:012d}').glob("*.json")):
        receipt = protocol.read_json(path)
        revision = latest["revision"] + 1
        protocol.require(type(receipt.get("version")) is int and receipt["version"] == 1,
                         "unsupported review version")
        protocol.require(type(receipt.get("revision")) is int and receipt["revision"] == revision and
                         path.name == f"{revision:012d}.json", "review revision chain is incomplete or invalid")
        protocol.require(all(type(receipt.get(key)) is type(event[key]) and receipt[key] == event[key]
                             for key in ("seq", "job_id", "round_id")) and
                         receipt.get("event_fingerprint") == event_key, "review event identity changed")
        protocol.require(receipt.get("status") in STATUSES, "invalid review status")
        validate_note(receipt.get("note"))
        timestamp(receipt.get("recorded_at"))
        validate_outcome(receipt, event)
        latest = receipt
    return latest


def validate_note(note: Any) -> None:
    """Require a bounded explanation of the actual review or outstanding decision."""
    protocol.require(isinstance(note, str) and bool(note.strip()) and len(note) <= 2000,
                     "review note must be nonempty and at most 2000 characters")
    protocol.validate_json_values(note)


def evidence_ref(value: str | None) -> dict[str, str] | None:
    """Pin an explicit controller evidence file; the contents are not interpreted."""
    if value is None:
        return None
    path = Path(value).resolve(strict=True)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return {"path": str(path), "sha256": digest.hexdigest()}


def validate_outcome(receipt: dict[str, Any], event: dict[str, Any]) -> None:
    """Keep reported resolution distinct from review and external input."""
    for field in ("action_evidence", "resolution_evidence"):
        ref = receipt.get(field)
        if ref is not None:
            protocol.require(isinstance(ref, dict) and set(ref) == {"path", "sha256"} and
                             isinstance(ref["path"], str) and Path(ref["path"]).is_absolute() and
                             isinstance(ref["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]),
                             "invalid review evidence reference")
    resolved = receipt.get("resolved_at")
    protocol.require((resolved is None) == (receipt.get("resolution_evidence") is None),
                     "resolution requires both resolved_at and evidence")
    if resolved is not None:
        timestamp(resolved)
        protocol.require(receipt["status"] == "handled" and
                         event["observed_at"] <= resolved <= receipt["recorded_at"],
                         "invalid resolution status/time")


@contextmanager
def review_lock(directory: Path) -> Iterator[None]:
    """Serialize controller receipts independently of the long-running observer lock."""
    directory.mkdir(mode=0o700, exist_ok=True)
    with (directory / ".lock").open("a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise protocol.ProtocolError("review update in progress; reread pending state before retrying") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def record_review(watch_path: str, seq: int, status: str, note: str,
                  expected_revision: int, now: float | None = None, *,
                  action_evidence: str | None = None, resolution_evidence: str | None = None,
                  resolved_at: float | None = None) -> dict[str, Any]:
    """Append one checked transition; exact lost-reply retries are idempotent."""
    protocol.require(status in STATUSES, "invalid review status")
    protocol.require(type(expected_revision) is int and expected_revision >= 0, "invalid expected revision")
    validate_note(note)
    recorded_at = time.time() if now is None else now
    timestamp(recorded_at)
    directory, identities = context(watch_path)
    event = load_event(directory, identities, seq)
    outcome = {"action_evidence": evidence_ref(action_evidence),
               "resolution_evidence": evidence_ref(resolution_evidence), "resolved_at": resolved_at}
    validate_outcome({**outcome, "status": status, "recorded_at": recorded_at}, event)
    with review_lock(directory / "reviews"):
        latest = latest_review(directory, event)
        if (latest["revision"] == expected_revision + 1 and
                latest["status"] == status and latest["note"] == note and
                all(latest.get(k) == v for k, v in outcome.items())):
            path = directory / "reviews" / f"{seq:012d}" / f'{latest["revision"]:012d}.json'
            return {"path": str(path), "review": latest, "duplicate": True}
        protocol.require(latest["revision"] == expected_revision,
                         "review revision conflict; reread pending state before deciding")
        receipt = {"version": 1, "seq": seq, "job_id": event["job_id"], "round_id": event["round_id"],
                   "event_fingerprint": fingerprint(event), "revision": expected_revision + 1,
                   "status": status, "note": note, "recorded_at": recorded_at, **outcome}
        target = directory / "reviews" / f"{seq:012d}"
        target.mkdir(mode=0o700, exist_ok=True)
        path = target / f'{receipt["revision"]:012d}.json'
        protocol.publish(path, receipt)
    return {"path": str(path), "review": receipt, "duplicate": False}


def priority(event: dict[str, Any]) -> int | None:
    """Surface reviewable events; ordinary working/recovered transitions stay in the log."""
    if event["kind"] == "state_changed":
        return {"blocked": 0, "unknown": 1, "dead": 1, "idle": 2, "done": 2}.get(event["message"])
    if event["kind"] == "result" and event["message"].startswith("blocked:"):
        return 0
    return {"attention": 0, "result_changed": 0, "review_due": 1, "observation_error": 1, "invalid_result": 1,
            "deadline": 1, "result": 2, "stalled": 2}.get(event["kind"])


def review_status(watch_path: str, seq: int) -> dict[str, Any]:
    """Recover the current revision, including handled events, and locate its audit trail."""
    directory, identities = context(watch_path)
    event = load_event(directory, identities, seq)
    return {"seq": seq, "review": latest_review(directory, event),
            "event_path": str(directory / "events" / f"{seq:012d}.json"),
            "history_directory": str(directory / "reviews" / f"{seq:012d}")}


def attach_related(items: list[dict[str, Any]], history: list[dict[str, Any]]) -> None:
    """Add bounded advisory history without transferring any review/approval state."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in history:
        if record.get("correlation_key"):
            grouped.setdefault(record["correlation_key"], []).append(record)
    for item in items:
        related = [r for r in grouped.get(item.get("correlation_key"), [])
                   if (r["watch_path"], r["seq"]) != (item["watch_path"], item["seq"])]
        related.sort(key=lambda r: (r["reviewed_at"], r["watch_path"], r["seq"]), reverse=True)
        item["related_review_count"] = len(related)
        item["related_reviews"] = [{k: r[k] for k in
            ("watch_path", "seq", "status", "note", "reviewed_at", "action_evidence",
             "resolution_evidence", "resolved_at")} for r in related[:3]]


def pending_items(watch_path: str, overdue_after: float = 30, now: float | None = None,
                  identities: set[tuple[str, str]] | None = None,
                  reviewed: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect validated queues; coalesce only unreviewed low-confidence reminders.

    A receipt for a later reminder covers older unreviewed reminders in that
    same local issue. Explicit waiting/open receipts remain independent. Newer
    events always require a fresh review, even after a handled receipt.
    """
    protocol.require(type(overdue_after) in (int, float) and math.isfinite(overdue_after) and
                     overdue_after > 0, "invalid overdue interval")
    current = time.time() if now is None else now
    timestamp(current)
    directory, pinned = context(watch_path)
    protocol.require(identities is None or identities <= pinned, "pending filter is outside watch inventory")
    records = []
    for path in sorted((directory / "events").glob("*.json")):
        protocol.require(path.name == f"{int(path.stem):012d}.json", "invalid event filename")
        event = load_event(directory, pinned, int(path.stem))
        if identities is not None and (event["job_id"], event["round_id"]) not in identities:
            continue
        review = latest_review(directory, event)
        rank = priority(event)
        if rank is None:
            continue
        age = max(0, current - event["observed_at"])
        item = {key: event[key] for key in ("seq", "job_id", "round_id", "kind", "evidence_path", "observed_at")}
        item.update({k: event[k] for k in ("issue_id", "correlation_key", "confidence") if k in event})
        item.update(message=event["message"][:200], priority=rank, age_seconds=age,
                    detected_at=event["observed_at"], occurrences=1, watch_path=str(directory / "watch.json"),
                    overdue=review["status"] == "open" and age >= overdue_after,
                    revision=review["revision"], status=review["status"],
                    note=review["note"], reviewed_at=review["recorded_at"],
                    **{k: review.get(k) for k in ("action_evidence", "resolution_evidence", "resolved_at")})
        records.append(item)
    history = [r for r in records if r["revision"]]
    action, waiting = [], []
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in records:
        if item["kind"] == "review_due" and item.get("issue_id"):
            key = (item["job_id"], item["round_id"], item["issue_id"])
            groups.setdefault(key, []).append(item)
        if item["status"] == "waiting_user":
            waiting.append(item)
        elif item["status"] == "open" and (item["revision"] or
                item["kind"] != "review_due" or not item.get("issue_id")):
            action.append(item)
    for group in groups.values():
        through = max((r["seq"] for r in group if r["revision"]), default=0)
        unreviewed = [r for r in group if not r["revision"] and r["seq"] > through]
        if unreviewed:
            latest = dict(unreviewed[-1])
            latest.update(occurrences=len(unreviewed), detected_at=unreviewed[0]["observed_at"],
                          first_seq=unreviewed[0]["seq"], first_evidence_path=unreviewed[0]["evidence_path"],
                          age_seconds=max(0, current - unreviewed[0]["observed_at"]))
            latest["overdue"] = latest["age_seconds"] >= overdue_after
            action.append(latest)
    attach_related(action + waiting, history)
    if reviewed is not None:
        reviewed.extend(history)
    action.sort(key=lambda item: (item["priority"], item["seq"]))
    return action, waiting


def pending(watch_path: str, limit: int = 20, overdue_after: float = 30,
            now: float | None = None, waiting_after: int = 0, action_offset: int = 0) -> dict[str, Any]:
    """Read unhandled events independently of delivery cursors, without terminal I/O."""
    protocol.require(type(limit) is int and 1 <= limit <= 200, "invalid pending limit")
    protocol.require(type(waiting_after) is int and waiting_after >= 0, "invalid waiting cursor")
    protocol.require(type(action_offset) is int and action_offset >= 0, "invalid action offset")
    action, waiting = pending_items(watch_path, overdue_after, now)
    directory = Path(watch_path).resolve().parent
    state = protocol.read_json(directory / "state.json")
    waiting_page = [item for item in waiting if item["seq"] > waiting_after]
    action_page = action[action_offset:action_offset + limit]
    return {"action_required": action_page, "waiting_user": waiting_page[:limit],
            "counts": {"open": len(action), "waiting_user": len(waiting),
                       "overdue": sum(item["overdue"] for item in action)},
            "more_action_required": len(action) > action_offset + limit,
            "next_action_offset": action_offset + len(action_page),
            "more_waiting_user": len(waiting_page) > limit,
            "next_waiting_after": waiting_page[:limit][-1]["seq"] if waiting_page else waiting_after,
            "last_checked_at": state.get("checked_at"),
            "note": "Review state is controller bookkeeping, not permission or proof of live UI resolution. "
                    "Recheck the target before input; waiting_user does not authorize an answer."}
