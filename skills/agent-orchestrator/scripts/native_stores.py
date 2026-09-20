"""Read exact OpenCode sessions and non-additive Hermes cumulative snapshots."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

import protocol
import usage


@contextmanager
def database(path: str) -> Iterator[sqlite3.Connection]:
    """Read a coherent SQLite view including WAL; never initialize or migrate it."""
    db = None
    try:
        db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db
    except sqlite3.Error as exc:
        raise protocol.ProtocolError(f"cannot read native SQLite store: {exc}") from exc
    finally:
        if db is not None:
            db.close()


def identity(value: Any, label: str) -> str:
    """Require an explicit native identity, never a newest-session heuristic."""
    protocol.require(isinstance(value, str) and bool(value.strip()), f"{label} must be a nonempty string")
    return value


def counter(value: Any, label: str) -> int | None:
    """Preserve absent counters as unknown and reject malformed present values."""
    protocol.require(value is None or type(value) is int and value >= 0,
                     f"native {label} must be a nonnegative integer or null")
    return value


def add_known(*values: int | None) -> int | None:
    """A normalized bucket is unknown if any constituent bucket is missing."""
    return None if any(v is None for v in values) else sum(v for v in values if v is not None)


def read_opencode(path: str, session_id: str | None,
                  selected_native_ids: set[str] | None = None) -> dict[str, Any]:
    """Read message usage once; session totals and step-finish parts overlap it."""
    with Path(path).open("rb") as stream:
        is_sqlite = stream.read(16) == b"SQLite format 3\x00"
    children = None
    if is_sqlite:
        session_id = identity(session_id, "SQLite session_id")
        with database(path) as db:
            row = db.execute("SELECT id, parent_id FROM session WHERE id=?", (session_id,)).fetchone()
            protocol.require(row is not None, "native session ID not found")
            parent = row["parent_id"]
            children = [r[0] for r in db.execute("SELECT id FROM session WHERE parent_id=? ORDER BY id",
                                               (session_id,))]
            messages = []
            for row in db.execute("SELECT id, session_id, data FROM message WHERE session_id=? ORDER BY id",
                                  (session_id,)):
                info = protocol.parse_json(row["data"])
                for name, value in (("id", row["id"]), ("sessionID", row["session_id"])):
                    protocol.require(name not in info or info[name] == value, "native message identity mismatch")
                    info[name] = value
                messages.append(info)
    else:
        value = protocol.parse_json(Path(path).read_text(encoding="utf-8"))
        info = value.get("info")
        protocol.require(isinstance(info, dict), "OpenCode export needs session info")
        actual = identity(info.get("id"), "native session ID")
        protocol.require(session_id is None or session_id == actual, "native session identity mismatch")
        session_id, parent = actual, info.get("parentID")
        protocol.require(isinstance(value.get("messages"), list), "OpenCode export needs messages array")
        messages = []
        for item in value["messages"]:
            protocol.require(isinstance(item, dict) and isinstance(item.get("info"), dict),
                             "invalid OpenCode message info")
            messages.append(item["info"])
    protocol.require(parent is None or isinstance(parent, str), "invalid parent session ID")
    by_id: dict[str, dict[str, Any]] = {}
    for info in messages:
        native_id = identity(info.get("id"), "native message ID")
        protocol.require(info.get("sessionID") == session_id, "native message session mismatch")
        protocol.require(native_id not in by_id, "duplicate native message ID")
        by_id[native_id] = info
    users = [key for key, info in by_id.items() if info.get("role") == "user"]
    calls = []
    pending = []
    for key, info in by_id.items():
        protocol.require(info.get("role") in ("user", "assistant"), "unsupported OpenCode message role")
        if info["role"] != "assistant":
            continue
        native_id = info.get("parentID")
        protocol.require(isinstance(native_id, str) and native_id in users,
                         "OpenCode assistant parent must be a user message in this session")
        time = info.get("time")
        protocol.require(isinstance(time, dict), "invalid OpenCode message time")
        completed = time.get("completed")
        protocol.require(completed is None or type(completed) is int and completed >= 0,
                         "invalid OpenCode completion timestamp")
        if completed is None:
            protocol.require(selected_native_ids is None or native_id not in selected_native_ids,
                             "selected OpenCode turn has an incomplete message; retry after native writing settles")
        tokens = info.get("tokens")
        counters = dict.fromkeys(usage.COUNTERS)
        if tokens is not None:
            protocol.require(isinstance(tokens, dict), "invalid native tokens")
            cache = tokens.get("cache", {})
            protocol.require(isinstance(cache, dict), "invalid native cache tokens")
            inp, out, reasoning = (counter(tokens.get(k), k) for k in ("input", "output", "reasoning"))
            read, write = (counter(cache.get(k), k) for k in ("read", "write"))
            counters = dict(input_tokens=add_known(inp, read, write), cached_input_tokens=read,
                            output_tokens=add_known(out, reasoning))
            # The native initial zero object can survive completion when the
            # provider reports no usage, even without an explicit message error.
            if not any(counters.values()):
                counters = dict.fromkeys(usage.COUNTERS)
        sample = dict(sample_id=key, source=f"opencode:{session_id}", measurement="delta",
                      provider=info.get("providerID"), model=info.get("modelID"), **counters)
        usage.validate_sample({**sample, "attempt_id": "validation", "purpose": "task"})
        if completed is None:
            pending.append(dict(native_id=native_id, sample_id=key))
            continue
        calls.append(dict(native_id=native_id, sample=sample))
    return dict(host="opencode", session_id=session_id, native_ids=users, calls=calls,
                parent_session_id=parent, child_session_ids=children, pending=pending)


def columns(db: sqlite3.Connection, table: str) -> set[str]:
    """Inspect only adapter-owned table names."""
    protocol.require(table in ("sessions", "session_model_usage"), "unsupported native table")
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def hermes_rows(db: sqlite3.Connection, table: str, session_id: str) -> list[dict[str, Any]] | None:
    """Select usage metadata only; absent legacy columns become explicit nulls."""
    available = columns(db, table)
    if not available:
        return None
    key = "id" if table == "sessions" else "session_id"
    protocol.require(key in available, "unsupported Hermes usage schema")
    fields = ("model", "billing_provider", "billing_base_url", "billing_mode", "task", "api_call_count",
              "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
    projection = ",".join(name if name in available else f"NULL AS {name}" for name in fields)
    rows = []
    for row in db.execute(f"SELECT {projection} FROM {table} WHERE {key}=?", (session_id,)):
        raw = {k: counter(row[k], k) for k in fields if k.endswith("tokens")}
        # Endpoints may contain credentials: hash the full native bucket identity, never emit it.
        bucket = [row[k] for k in fields[:5]]
        rows.append(dict(bucket_id=hashlib.sha256(json.dumps(bucket).encode()).hexdigest(),
                         model=row["model"], provider=row["billing_provider"], task=row["task"],
                         api_call_count=counter(row["api_call_count"], "api_call_count"), native_counters=raw,
                         counters=dict(input_tokens=add_known(raw["input_tokens"], raw["cache_read_tokens"],
                                                              raw["cache_write_tokens"]),
                                       cached_input_tokens=raw["cache_read_tokens"], output_tokens=raw["output_tokens"])))
    return sorted(rows, key=lambda row: row["bucket_id"])


def inspect_hermes(path: str, session_id: str | None) -> dict[str, Any]:
    """Keep overlapping cumulative views outside the additive round ledger."""
    session_id = identity(session_id, "SQLite session_id")
    with database(path) as db:
        rows = hermes_rows(db, "sessions", session_id)
        protocol.require(rows is not None and len(rows) == 1, "native session ID not found or ambiguous")
        models = hermes_rows(db, "session_model_usage", session_id)
        cols = columns(db, "sessions")
        parent = None
        children = None
        if "parent_session_id" in cols:
            parent = db.execute("SELECT parent_session_id FROM sessions WHERE id=?", (session_id,)).fetchone()[0]
            children = [r[0] for r in db.execute("SELECT id FROM sessions WHERE parent_session_id=? ORDER BY id",
                                               (session_id,))]
    return dict(host="hermes", session_id=session_id, source=f"hermes:{session_id}",
                observed_at=datetime.now(timezone.utc).isoformat(), measurement="cumulative_snapshot",
                round_attributable=False, source_coverage="unknown", parent_session_id=parent,
                child_session_ids=children, session_usage=rows[0], model_usage=models,
                note="Non-additive views: model/task buckets may overlap session totals and include auxiliary usage. "
                     "Native persistence can lag; zeros do not prove full coverage. No round deltas or costs inferred.")
