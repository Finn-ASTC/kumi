"""Allowlisted local observations; one durable transaction per delivered hook."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any
import uuid

COUNTERS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "reasoning_tokens", "prompt_tokens", "total_tokens")
KINDS = ("turn_start", "request_start", "request_end", "request_error", "turn_end")


def text(value: Any, required: bool = False) -> str | None:
    """Bound identity/route labels; never stringify arbitrary native objects."""
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("invalid hook identity or label")
    return value


def record_event(home: str | Path, kind: str, data: dict[str, Any]) -> int:
    """Persist only IDs and numeric telemetry, never the broad hook payload."""
    if kind not in KINDS:
        raise ValueError("unsupported usage hook event")
    sid, turn = text(data.get("session_id"), True), text(data.get("turn_id"), True)
    request = text(data.get("api_request_id"), True) if kind.startswith("request_") else None
    measured = None
    raw = data.get("usage") if kind == "request_end" else None
    if raw is not None:
        if not isinstance(raw, dict):
            raise ValueError("usage must be a canonical counter object or null")
        measured = {k: raw.get(k) for k in COUNTERS}
        if any(v is not None and (type(v) is not int or v < 0) for v in measured.values()):
            raise ValueError("usage counters must be nonnegative integers or null")
    payload = dict(model=text(data.get("response_model") or data.get("model")),
                   provider=text(data.get("provider")), usage=measured, has_moa=bool(data.get("moa_references")))
    prompt = data.get("user_message") if kind == "turn_start" else None
    payload["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest() if isinstance(prompt, str) else None
    # Even safe-looking request/response/error objects can contain private text.
    # This whitelist is shared by all events; lifecycle booleans are advisory only.
    for flag in ("completed", "failed", "interrupted"):
        value = data.get(flag)
        if value is not None and type(value) is not bool:
            raise ValueError("invalid lifecycle flag")
        payload[flag] = value
    directory = Path(home).resolve() / "usage-hooks"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("usage directory must not be redirected")
    path = directory / "events.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("usage store must be a regular file")
    finally:
        os.close(fd)
    with closing(sqlite3.connect(path, timeout=1.0)) as db, db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS orch_usage_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                   "version INTEGER NOT NULL,store_id TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO orch_usage_meta VALUES(1,1,?)", (uuid.uuid4().hex,))
        version, = db.execute("SELECT version FROM orch_usage_meta WHERE singleton=1").fetchone()
        if version != 1:
            raise ValueError("unsupported usage store version")
        db.execute("CREATE TABLE IF NOT EXISTS orch_usage_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                   "session_id TEXT NOT NULL,turn_id TEXT NOT NULL,request_id TEXT,kind TEXT NOT NULL,payload TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS orch_usage_session ON orch_usage_events(session_id,seq)")
        cursor = db.execute("INSERT INTO orch_usage_events(session_id,turn_id,request_id,kind,payload) VALUES(?,?,?,?,?)",
                            (sid, turn, request, kind, json.dumps(payload, ensure_ascii=False, allow_nan=False)))
        sequence = cursor.lastrowid
    return sequence
