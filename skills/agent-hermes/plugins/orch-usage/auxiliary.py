"""Persist auxiliary accounting observations without inventing round ownership."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any
import uuid

from .recorder import COUNTERS, text


def record_aux_event(home: str | Path, data: dict[str, Any]) -> int:
    """Deduplicate one native observation ID; conflicting redelivery fails closed."""
    observation = text(data.get("observation_id"), True)
    sid = text(data.get("session_id"), True)
    task = text(data.get("task"), True)
    if data.get("turn_id") is not None:
        raise ValueError("auxiliary turn ownership is unsupported")
    if task in ("moa_reference", "moa_aggregator"):
        raise ValueError("main-loop-accounted MoA usage cannot enter auxiliary storage")
    raw = data.get("usage")
    measured = None
    if raw is not None:
        if not isinstance(raw, dict):
            raise ValueError("auxiliary usage must be an object or null")
        measured = {key: raw.get(key) for key in COUNTERS}
        if any(value is not None and (type(value) is not int or value < 0)
               for value in measured.values()):
            raise ValueError("auxiliary counters must be nonnegative integers or null")
    payload = json.dumps(dict(task=task, model=text(data.get("model")),
                              provider_hint=text(data.get("provider_hint")), usage=measured),
                         sort_keys=True, ensure_ascii=False, allow_nan=False)
    directory = Path(home).resolve() / "usage-hooks"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("usage directory must not be redirected")
    path = directory / "auxiliary.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("auxiliary store must be a regular file")
    finally:
        os.close(fd)
    with closing(sqlite3.connect(path, timeout=1.0)) as db, db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS orch_aux_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                   "version INTEGER NOT NULL,store_id TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO orch_aux_meta VALUES(1,1,?)", (uuid.uuid4().hex,))
        if db.execute("SELECT version FROM orch_aux_meta WHERE singleton=1").fetchone() != (1,):
            raise ValueError("unsupported auxiliary store version")
        db.execute("CREATE TABLE IF NOT EXISTS orch_aux_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                   "observation_id TEXT NOT NULL UNIQUE,session_id TEXT NOT NULL,payload TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS orch_aux_session ON orch_aux_events(session_id,seq)")
        # Lock before checking identity: concurrent redelivery cannot add rows or advance
        # sqlite_sequence (INSERT OR IGNORE would leave false sequence gaps).
        db.execute("UPDATE orch_aux_meta SET version=version WHERE singleton=1")
        existing = db.execute("SELECT seq,session_id,payload FROM orch_aux_events WHERE observation_id=?",
                              (observation,)).fetchone()
        if existing:
            if existing[1:] != (sid, payload):
                raise ValueError("conflicting auxiliary observation ID")
            return existing[0]
        cursor = db.execute("INSERT INTO orch_aux_events(observation_id,session_id,payload) VALUES(?,?,?)",
                            (observation, sid, payload))
        return cursor.lastrowid
