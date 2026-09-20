"""Read exact sessions from the optional Hermes usage-hook recorder."""

from typing import Any

import native_stores
import protocol
import usage

LIMITATIONS = ["auxiliary_calls_unknown", "internal_provider_retries_unknown", "native_children_unknown",
               "hook_delivery_unverified", "history_before_enable_unknown"]
RAW = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
       "reasoning_tokens", "prompt_tokens", "total_tokens")


def is_store(path: str) -> bool:
    """Distinguish the plugin store from the cumulative native state.db, without mutation."""
    with native_stores.database(path) as db:
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='orch_usage_meta'").fetchone() is not None


def _sample(payload: dict[str, Any], source: str, sample_id: str, error: bool) -> dict[str, Any]:
    counters = dict.fromkeys(usage.COUNTERS)
    raw = payload["usage"]
    if raw is not None:
        protocol.require(isinstance(raw, dict) and set(raw) == set(RAW), "invalid saved hook usage")
        for key, value in raw.items():
            native_stores.counter(value, key)
        total_input = native_stores.add_known(raw["input_tokens"], raw["cache_read_tokens"], raw["cache_write_tokens"])
        total = native_stores.add_known(total_input, raw["output_tokens"])
        for key, expected in (("prompt_tokens", total_input), ("total_tokens", total)):
            protocol.require(raw[key] is None or expected is None or raw[key] == expected,
                             "hook canonical usage buckets disagree")
        if not error and not payload["has_moa"] and any(v for v in raw.values() if v is not None):
            counters = dict(input_tokens=total_input, cached_input_tokens=raw["cache_read_tokens"],
                            output_tokens=raw["output_tokens"])
    sample = dict(source=source, sample_id=sample_id, measurement="delta", model=payload["model"],
                  provider=payload["provider"], **counters)
    usage.validate_sample({**sample, "attempt_id": "validate", "purpose": "task"})
    return sample


def read_log(path: str, session_id: str | None, selected_native_ids: set[str] | None = None) -> dict[str, Any]:
    """Use durable start occurrences as call IDs; reused native request IDs are not deduplicated."""
    sid = native_stores.identity(session_id, "hook session_id")
    with native_stores.database(path) as db:
        meta = db.execute("SELECT version,store_id FROM orch_usage_meta WHERE singleton=1").fetchone()
        protocol.require(meta is not None and type(meta["version"]) is int and meta["version"] == 1,
                         "unsupported Hermes hook store")
        store = meta["store_id"]
        protocol.require(isinstance(store, str) and len(store) == 32 and
                         all(c in "0123456789abcdef" for c in store), "invalid hook store identity")
        count, maximum = db.execute("SELECT COUNT(*),COALESCE(MAX(seq),0) FROM orch_usage_events").fetchone()
        watermark = db.execute("SELECT seq FROM sqlite_sequence WHERE name='orch_usage_events'").fetchone()
        protocol.require(count == maximum and watermark is not None and watermark[0] == maximum,
                         "hook event sequence has missing records")
        rows = db.execute("SELECT * FROM orch_usage_events WHERE session_id=? ORDER BY seq", (sid,)).fetchall()
    protocol.require(bool(rows), "native hook session ID not found")
    turns: dict[str, dict[str, Any]] = {}
    active: dict[tuple[str, str], int] = {}
    calls, issues, receipts = [], [], []
    source = "hermes:" + sid
    for row in rows:
        turn = native_stores.identity(row["turn_id"], "hook turn_id")
        info = turns.setdefault(turn, dict(started=False, closed=False, last_sequence=row["seq"]))
        info["last_sequence"] = row["seq"]
        kind = row["kind"]
        protocol.require(kind in ("turn_start", "turn_end", "request_start", "request_end", "request_error"),
                         "invalid hook event kind")
        payload = protocol.parse_json(row["payload"])
        protocol.require(set(payload) == {"model", "provider", "usage", "has_moa", "completed", "failed", "interrupted", "prompt_sha256"}
                         and type(payload["has_moa"]) is bool, "invalid hook payload")
        digest = payload["prompt_sha256"]
        protocol.require(digest is None or isinstance(digest, str) and len(digest) == 64 and
                         all(c in "0123456789abcdef" for c in digest), "invalid prompt receipt hash")
        for flag in ("completed", "failed", "interrupted"):
            protocol.require(payload[flag] is None or type(payload[flag]) is bool, "invalid saved lifecycle flag")
        if kind == "turn_end":
            info["closed"] = True
            info["close_sequence"] = row["seq"]
            continue
        if info["closed"]:
            issues.append(dict(native_id=turn, code="event_after_turn_end", sequence=row["seq"]))
            info["closed"] = False
        if kind == "turn_start":
            if info["started"]:
                issues.append(dict(native_id=turn, code="duplicate_turn_start", sequence=row["seq"]))
            info["started"] = True
            info["prompt_sha256"] = digest
            continue
        request = native_stores.identity(row["request_id"], "hook request_id")
        key = (turn, request)
        if kind == "request_start":
            if key in active:
                issues.append(dict(native_id=turn, code="overlapping_request_start", sequence=row["seq"]))
            active[key] = row["seq"]
            continue
        begin = active.pop(key, None)
        if begin is None:
            issues.append(dict(native_id=turn, code="orphan_terminal_event", sequence=row["seq"]))
            continue
        sample = _sample(payload, source, f"hook:{store}:{begin}", kind == "request_error")
        calls.append(dict(native_id=turn, sample=sample))
        receipts.append(dict(native_id=turn, api_request_id=request, sample_id=sample["sample_id"],
                             start_sequence=begin, end_sequence=row["seq"], outcome=kind))
    pending = [dict(native_id=turn, sample_id=f"hook:{store}:{seq}") for (turn, _), seq in active.items()]
    for turn, info in turns.items():
        if not info["started"] or not info["closed"]:
            pending.append(dict(native_id=turn, sample_id="unsettled:" + turn))
    if selected_native_ids is not None:
        protocol.require(not any(p["native_id"] in selected_native_ids for p in pending),
                         "selected Hermes turn has pending calls or missing turn boundaries")
        protocol.require(not any(i["native_id"] in selected_native_ids for i in issues),
                         "selected Hermes turn has hook integrity issues")
    return dict(host="hermes", session_id=sid, native_ids=list(turns), calls=calls, pending=pending,
                parent_session_id=None, child_session_ids=None, measurement="delta", coverage_complete=False,
                coverage_scope="observed_main_loop_hooks", limitations=LIMITATIONS, integrity_issues=issues,
                turn_receipts=turns, call_receipts=receipts, store_id=store, persisted_sequence=maximum)
