"""Read auxiliary observations as separate, incomplete, session-scoped evidence."""

from typing import Any

import native_stores
import protocol

COUNTERS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "reasoning_tokens", "prompt_tokens", "total_tokens")
LIMITATIONS = ("round_ownership_unknown", "responses_without_accounting_context_unknown",
               "internal_provider_retries_unknown", "errors_without_response_unknown",
               "streaming_and_direct_accounting_paths_unknown", "native_children_unknown",
               "hook_delivery_unverified", "history_before_enable_unknown")


def _counters(raw: Any) -> dict[str, int | None]:
    result: dict[str, int | None] = dict(input_tokens=None, cached_input_tokens=None, output_tokens=None)
    if raw is None:
        return result
    protocol.require(isinstance(raw, dict) and set(raw) == set(COUNTERS), "invalid auxiliary usage")
    for key, value in raw.items():
        native_stores.counter(value, key)
    total_input = native_stores.add_known(raw["input_tokens"], raw["cache_read_tokens"], raw["cache_write_tokens"])
    total = native_stores.add_known(total_input, raw["output_tokens"])
    for key, expected in (("prompt_tokens", total_input), ("total_tokens", total)):
        protocol.require(raw[key] is None or expected is None or raw[key] == expected,
                         "auxiliary canonical usage buckets disagree")
    if total_input is not None and raw["output_tokens"] is not None and total:
        result.update(input_tokens=total_input, cached_input_tokens=raw["cache_read_tokens"],
                      output_tokens=raw["output_tokens"])
    return result


def inspect(path: str, session_id: str) -> dict[str, Any]:
    """Inspect committed observations; no round import or certified host total."""
    sid = native_stores.identity(session_id, "auxiliary session_id")
    with native_stores.database(path) as db:
        meta = db.execute("SELECT version,store_id FROM orch_aux_meta WHERE singleton=1").fetchone()
        protocol.require(meta is not None and meta["version"] == 1, "unsupported auxiliary store")
        store = meta["store_id"]
        protocol.require(isinstance(store, str) and len(store) == 32 and
                         all(c in "0123456789abcdef" for c in store), "invalid auxiliary store identity")
        count, maximum = db.execute("SELECT COUNT(*),COALESCE(MAX(seq),0) FROM orch_aux_events").fetchone()
        watermark = db.execute("SELECT seq FROM sqlite_sequence WHERE name='orch_aux_events'").fetchone()
        protocol.require(count == maximum and watermark is not None and watermark[0] == maximum,
                         "auxiliary event sequence has missing records")
        rows = db.execute("SELECT * FROM orch_aux_events WHERE session_id=? ORDER BY seq", (sid,)).fetchall()
    protocol.require(bool(rows), "native auxiliary session ID not found")
    receipts = []
    seen = set()
    for row in rows:
        observation = native_stores.identity(row["observation_id"], "auxiliary observation_id")
        protocol.require(observation not in seen, "duplicate auxiliary identity")
        seen.add(observation)
        payload = protocol.parse_json(row["payload"])
        protocol.require(isinstance(payload, dict) and set(payload) == {"task", "model", "provider_hint", "usage"},
                         "invalid auxiliary payload")
        task = native_stores.identity(payload["task"], "auxiliary task")
        protocol.require(task not in ("moa_reference", "moa_aggregator"), "overlapping MoA auxiliary usage")
        for key in ("model", "provider_hint"):
            if payload[key] is not None:
                native_stores.identity(payload[key], "auxiliary " + key)
        receipts.append(dict(source="hermes-aux:" + sid, sample_id="aux:" + observation,
                             observation_id=observation, sequence=row["seq"], native_turn_id=None,
                             **{key: payload[key] for key in ("task", "model", "provider_hint")},
                             **_counters(payload["usage"])))
    totals = {key: (sum(r[key] for r in receipts) if all(r[key] is not None for r in receipts) else None)
              for key in ("input_tokens", "cached_input_tokens", "output_tokens")}
    return dict(host="hermes", session_id=sid, store_id=store, persisted_sequence=maximum,
                observed_responses=len(receipts), unassigned_responses=len(receipts),
                unknown_responses=sum(r["input_tokens"] is None or r["output_tokens"] is None for r in receipts),
                receipts=receipts, observed_totals=totals, totals=None, coverage_complete=False,
                coverage_scope="observed_auxiliary_accounting_responses", limitations=list(LIMITATIONS))
