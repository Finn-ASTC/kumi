"""Synthetic host-format stores for the control lab; never read user logs or call models."""

from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3

import e2e_runner


def write_store(root: Path, host: str, session: str, turn: str) -> Path:
    """Record one synthetic 100-input/60-cached/20-output sample per host format."""
    root.mkdir(mode=0o700)
    if host == "hermes":
        path = e2e_runner.SCRIPTS.parents[1] / "agent-hermes/plugins/orch-usage/recorder.py"
        spec = importlib.util.spec_from_file_location("control_fixture_recorder", path)
        if spec is None or spec.loader is None:
            raise ValueError("Hermes fixture recorder unavailable")
        recorder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(recorder)
        data = dict(session_id=session, turn_id=turn, api_request_id="fixture-request",
                    model="fixture", provider="fixture")
        for kind in ("turn_start", "request_start", "request_end", "turn_end"):
            payload = dict(data)
            if kind == "request_end":
                payload["usage"] = dict(input_tokens=30, cache_read_tokens=60, cache_write_tokens=10,
                                        output_tokens=20, reasoning_tokens=0, prompt_tokens=100, total_tokens=120)
            recorder.record_event(root, kind, payload)
        return root / "usage-hooks/events.sqlite3"
    if host == "opencode":
        path = root / "synthetic.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("CREATE TABLE session(id TEXT PRIMARY KEY,parent_id TEXT);"
                             "CREATE TABLE message(id TEXT PRIMARY KEY,session_id TEXT,data TEXT);")
            db.execute("INSERT INTO session VALUES (?,NULL)", (session,))
            for mid, info in ((turn, dict(role="user")), ("fixture-call", dict(
                role="assistant", parentID=turn, providerID="fixture", modelID="fixture",
                time=dict(completed=1), tokens=dict(input=30, output=20, reasoning=0,
                                                  cache=dict(read=60, write=10))))):
                db.execute("INSERT INTO message VALUES (?,?,?)", (mid, session, json.dumps(info)))
        return path
    if host == "codex":
        counts = dict(input_tokens=100, cached_input_tokens=60, output_tokens=20)
        rows = [{"type": "session_meta", "payload": {"id": session, "model_provider": "fixture"}},
                {"type": "turn_context", "payload": {"turn_id": turn, "model": "fixture"}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": counts, "last_token_usage": counts}}}]
    elif host == "omp":
        rows = [{"type": "session", "version": 3, "id": session},
                {"type": "message", "id": turn, "parentId": None, "message": {"role": "user"}},
                {"type": "message", "id": "fixture-call", "parentId": turn, "message": {
                    "role": "assistant", "provider": "fixture", "model": "fixture", "usage": {
                        "input": 30, "cacheRead": 60, "cacheWrite": 10, "output": 20, "totalTokens": 120}}}]
    else:
        raise ValueError("unknown synthetic host")
    path = root / "synthetic.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path
