"""Read exact indexed rounds and annotate reviews without rewriting transition history."""

import copy
from pathlib import Path
from typing import Any

import completion
import jobs
import protocol


def select(
    state: dict[str, Any], round_id: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Reconstruct the last active snapshot; overlays are explicitly later annotations.

    Historical scans are opt-in. Normal recovery still reads only the active state.
    Legacy transitions lack a contemporaneous checked verdict, so remain unknown.
    """
    protocol.require(
        isinstance(round_id, str) and jobs.ID.fullmatch(round_id), "invalid round_id"
    )
    records = [r for r in state["rounds"] if r["round_id"] == round_id]
    protocol.require(len(records) == 1, "round is not uniquely indexed in this job")
    if round_id == state["round_id"]:
        jobs.check_contract(state)
        return copy.deepcopy(state), None
    selected = None
    transition = None
    directory = Path(state["index_path"]) / state["job_id"]
    for sequence in range(1, state["revision"] + 1):
        event = protocol.read_json(directory / f"{sequence:012d}.json")
        protocol.require(
            type(event.get("version")) is int
            and event["version"] == 1
            and type(event.get("revision")) is int
            and event["revision"] == sequence
            and event.get("job_id") == state["job_id"]
            and event.get("index_path") == state["index_path"],
            "historical index identity/revision changed",
        )
        if event["round_id"] == round_id:
            protocol.require(transition is None, "historical round was reactivated")
            selected = event
        elif selected is not None and transition is None:
            protocol.require(
                event["event"] == "activate", "historical transition is missing"
            )
            receipt = event.get("last_transition")
            if receipt is not None:
                protocol.require(
                    type(receipt.get("version")) is int
                    and receipt["version"] == 1
                    and receipt["request_sha256"] == records[0]["request_sha256"]
                    and receipt["from_round_id"] == round_id
                    and receipt["to_round_id"] == event["round_id"]
                    and receipt["revision"] == sequence,
                    "historical transition identity changed",
                )
                transition = receipt
            else:
                transition = {
                    "from_round_id": round_id,
                    "to_round_id": event["round_id"],
                    "revision": sequence,
                    "recorded_at": event["recorded_at"],
                    "acceptance_status": "unknown_legacy",
                }
    protocol.require(
        selected is not None and transition is not None,
        "indexed round history is missing",
    )
    protocol.require(
        selected["rounds"][-1] == records[0], "historical request binding changed"
    )
    jobs.check_contract(selected)
    annotation = state.get("historical_acceptance", {}).get(round_id)
    if annotation is not None:
        selected.setdefault("completion", {})["acceptance"] = annotation
    return selected, transition


def status(state: dict[str, Any], round_id: str) -> dict[str, Any]:
    """Query one review and its original transition without exposing leases or live-host claims."""
    selected, transition = select(state, round_id)
    result = jobs.result_view(selected)
    return {
        "job_id": state["job_id"],
        "round_id": round_id,
        "current_round_id": state["round_id"],
        "revision": state["revision"],
        "is_current_round": round_id == state["round_id"],
        "request_path": selected["active_request"],
        "result": result,
        "acceptance": completion.acceptance_summary(selected, result),
        "acceptance_record": selected.get("completion", {}).get("acceptance"),
        "transition": transition,
        "executes_commands": False,
        "note": "Review validity is checked now; transition facts retain the original decision. "
        "Legacy transition checks are unknown. This query does not establish live host readiness.",
    }


def transition(state: dict[str, Any], to_round_id: str, now: float) -> dict[str, Any]:
    """Require a valid review of success, while preserving error/blocked continuations."""
    result = jobs.result_view(state)
    protocol.require(
        result["status"] in ("success", "error", "blocked"),
        "active round needs a valid result before follow-up",
    )
    review = completion.acceptance_summary(state, result)
    if result["status"] == "success":
        protocol.require(
            review["valid"],
            "success round needs valid acceptance before follow-up: "
            + (review["reason"] or review["status"]),
        )
    acceptance = state.get("completion", {}).get("acceptance")
    return {
        "version": 1,
        "from_round_id": state["round_id"],
        "to_round_id": to_round_id,
        "revision": state["revision"] + 1,
        "recorded_at": now,
        "request_sha256": state["rounds"][-1]["request_sha256"],
        "result_sha256": result["sha256"],
        "response_status": result["status"],
        "acceptance_status": review["status"],
        "acceptance_recorded_at": acceptance.get("recorded_at") if acceptance else None,
        "acceptance_recorded_revision": acceptance.get("recorded_revision")
        if acceptance
        else None,
    }
