"""Exercise an installed Hermes approval transport module with synthetic requests.

No model, command execution, plugin discovery, user profile or real approval.
This checks the native library contract, not a working human approval UI.
"""

import argparse
import asyncio
from dataclasses import FrozenInstanceError
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
from typing import Any


def probe(source: Path) -> dict[str, Any]:
    """Load the exact source module without starting Hermes or loading profiles."""
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("kumi_native_approval_transport", source)
    if spec is None or spec.loader is None:
        raise ValueError("approval transport source unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    def request():
        return module.ApprovalRequest.create(
            command="synthetic operation; never executed", description="Fixture only",
            pattern_key="fixture", pattern_keys=("fixture",), session_key="fixture-session",
            surface="cli", allow_session=False, allow_permanent=False, timeout_seconds=1)

    current = request()
    rows = []

    def check(name, callback, expected_failure=None, expected_choice="deny", **kwargs):
        result = module.invoke_approval_transport(callback, current, timeout_seconds=kwargs.pop("timeout", 1),
                                                  poll_interval=0.005, **kwargs)
        assert (result.choice, result.failure) == (expected_choice, expected_failure), (name, result)
        rows.append(dict(case=name, choice=result.choice, failure=result.failure))

    check("deny", lambda req: req.respond("deny"))
    check("synthetic_once_positive_control", lambda req: req.respond("once"), expected_choice="once")

    async def async_deny(req):
        await asyncio.sleep(0)
        return req.respond("deny")

    check("async_deny", async_deny)
    check("unbound_dictionary", lambda req: {"choice": "once"}, "invalid")
    check("stale_request_id", lambda req: module.ApprovalDecision("old", req.digest, "once"), "stale")
    check("stale_digest", lambda req: module.ApprovalDecision(req.request_id, "old", "once"), "stale")
    check("permanent_scope_not_offered", lambda req: req.respond("always"), "invalid")
    check("session_scope_not_offered", lambda req: req.respond("session"), "invalid")

    def raises(req):
        raise RuntimeError("Synthetic transport failure")

    check("transport_exception", raises, "error")
    check("interrupted", lambda req: req.respond("once"), "interrupted", is_interrupted=lambda: True)

    release, finished = threading.Event(), threading.Event()

    def late_once(req):
        try:
            release.wait(timeout=2)
            return req.respond("once")
        finally:
            finished.set()

    try:
        check("timeout_before_late_once", late_once, "timeout", timeout=0.03)
    finally:
        release.set()
        assert finished.wait(timeout=2), "fixture callback failed to settle"
    old = current
    current = request()
    assert current.request_id != old.request_id and current.digest != old.digest
    check("old_reply_for_new_request", lambda req: old.respond("once"), "stale")
    check("new_request_after_timeout", lambda req: req.respond("deny"))
    try:
        current.command = "changed"
    except FrozenInstanceError:
        rows.append(dict(case="immutable_request", status="passed"))
    else:
        raise AssertionError("native request became mutable")
    return dict(status="passed", source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                checks=rows, model_calls=0, commands_executed=0, real_approvals=0,
                plugin_discovery=False, profile_changed=False, native_tui_verified=False,
                limits=["library_contract_only", "no_human_ui", "no_kumi_control_receipt_bridge",
                        "no_background_stop_verification"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    source = args.hermes_root.resolve() / "hermes_cli/approval_transport.py"
    root = Path(tempfile.mkdtemp(prefix="approval-probe-", dir=args.root)).resolve()
    report = probe(source)
    (root / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(status=report["status"], root=str(root), checks=len(report["checks"]),
                         model_calls=0, real_approvals=0)))


if __name__ == "__main__":
    main()
