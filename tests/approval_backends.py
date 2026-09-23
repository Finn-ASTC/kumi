"""Versioned shadow backend contract; model-specific behavior stays in adapters."""

import json
import math
import re
import subprocess
import tempfile
from typing import Any

import jev_backend
from jev_backend import protocol

VERSION = 1
LABELS = {"allow", "deny", "escalate"}
TAG = re.compile(r"[a-z][a-z0-9_.-]{0,95}")
MAX_RESPONSE_BYTES = 65536


def validate_config(config: dict[str, Any]) -> None:
    """An explicit adapter selection never silently falls back to another provider."""
    protocol.require(
        isinstance(config, dict)
        and set(config)
        == {
            "version",
            "adapter",
            "model",
            "data_location",
            "timeout_seconds",
            "max_request_bytes",
            "argv",
        },
        "invalid backend config",
    )
    protocol.require(
        type(config["version"]) is int and config["version"] == VERSION,
        "invalid backend version",
    )
    protocol.require(
        config["adapter"] in ("rules", "jev", "command"), "unknown adapter"
    )
    protocol.require(
        isinstance(config["model"], str)
        and 0 < len(config["model"]) <= 128
        and config["model"].isprintable(),
        "exact model identifier required",
    )
    protocol.require(
        config["data_location"] in ("local", "remote"),
        "explicit data location required",
    )
    seconds = config["timeout_seconds"]
    protocol.require(
        type(seconds) in (int, float) and math.isfinite(seconds) and 0 < seconds <= 30,
        "invalid timeout",
    )
    size = config["max_request_bytes"]
    protocol.require(
        type(size) is int and 1024 <= size <= 65536, "invalid input byte budget"
    )
    argv = config["argv"]
    if config["adapter"] == "command":
        protocol.require(
            isinstance(argv, list)
            and 1 <= len(argv) <= 32
            and all(
                isinstance(arg, str) and "\x00" not in arg and len(arg) <= 4096
                for arg in argv
            )
            and bool(argv[0]),
            "invalid adapter argv",
        )
    else:
        protocol.require(argv is None, "built-in adapters do not accept argv")
        expected = (
            ("local", "rules-v1")
            if config["adapter"] == "rules"
            else ("remote", jev_backend.MODEL)
        )
        protocol.require(
            (config["data_location"], config["model"]) == expected,
            "adapter location or model mismatch",
        )


def default_config(adapter: str = "rules") -> dict[str, Any]:
    """Provide pinned built-ins; external commands always require explicit configuration."""
    protocol.require(adapter in ("rules", "jev"), "custom adapter needs a config file")
    return dict(
        version=VERSION,
        adapter=adapter,
        model="rules-v1" if adapter == "rules" else jev_backend.MODEL,
        data_location="local" if adapter == "rules" else "remote",
        timeout_seconds=8,
        max_request_bytes=16000,
        argv=None,
    )


def validate_answer(
    answer: dict[str, Any], config: dict[str, Any], packet: dict[str, Any]
) -> dict[str, Any]:
    """Validate transport-independent suggestions; no shared confidence cutoff exists."""
    fields = {
        "version",
        "request_id",
        "input_sha256",
        "model",
        "candidate",
        "scores",
        "score_semantics",
        "confidence",
        "confidence_semantics",
        "usage",
    }
    protocol.require(
        isinstance(answer, dict) and set(answer) == fields,
        "invalid adapter response fields",
    )
    protocol.require(
        type(answer["version"]) is int
        and answer["version"] == VERSION
        and answer["request_id"] == packet["request_id"]
        and answer["input_sha256"] == packet["input_sha256"]
        and answer["model"] == config["model"],
        "response binding/model mismatch",
    )
    protocol.require(
        answer["candidate"] in LABELS
        and answer["score_semantics"] in ("probability", "logit", "score", "none"),
        "invalid candidate or score semantics",
    )
    scores = answer["scores"]
    if answer["score_semantics"] == "none":
        protocol.require(scores is None, "missing-score semantics mismatch")
    else:
        protocol.require(
            isinstance(scores, dict)
            and set(scores) == LABELS
            and all(
                type(v) in (float, int) and math.isfinite(v) for v in scores.values()
            ),
            "incomplete or invalid scores",
        )
        if answer["score_semantics"] == "probability":
            protocol.require(
                all(0 <= v <= 1 for v in scores.values())
                and math.isclose(sum(scores.values()), 1, abs_tol=1e-6),
                "invalid probability distribution",
            )
    confidence = answer["confidence"]
    semantics = answer["confidence_semantics"]
    protocol.require(
        (confidence is None and semantics is None)
        or (
            type(confidence) in (float, int)
            and math.isfinite(confidence)
            and isinstance(semantics, str)
            and TAG.fullmatch(semantics)
        ),
        "confidence needs explicit backend semantics",
    )
    usage = answer["usage"]
    protocol.require(
        isinstance(usage, dict)
        and set(usage) == {"input_tokens", "output_tokens"}
        and all(
            value is None or type(value) is int and value >= 0
            for value in usage.values()
        ),
        "invalid usage",
    )
    return answer


def invoke(config: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    """One attempt via the selected adapter; subprocess timeout bounds the full wait."""
    validate_config(config)
    if config["adapter"] == "jev":
        result = jev_backend.evaluate(
            jev_backend.question(packet["state"]), config["timeout_seconds"]
        )
        if result["status"] != "ok":
            return result
        value = result["answer"]
        answer = dict(
            version=VERSION,
            request_id=packet["request_id"],
            input_sha256=packet["input_sha256"],
            model=value["model"],
            candidate=value["candidate"],
            scores=value["probabilities"],
            score_semantics="probability",
            confidence=value["confidence"],
            confidence_semantics=value["confidence_semantics"],
            usage=value["usage"],
        )
    elif config["adapter"] == "command":
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                completed = subprocess.run(
                    config["argv"],
                    input=json.dumps(packet, ensure_ascii=False).encode(),
                    stdout=stdout,
                    stderr=stderr,
                    timeout=config["timeout_seconds"],
                    check=False,
                )
                stdout.seek(0)
                raw = stdout.read(MAX_RESPONSE_BYTES + 1)
            if completed.returncode or len(raw) > MAX_RESPONSE_BYTES:
                return {"status": "adapter_error", "request_attempted": True}
            answer = protocol.parse_json(raw.decode("utf-8"))
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "request_attempted": True}
        except (OSError, ValueError, KeyError, TypeError):
            return {"status": "adapter_error", "request_attempted": True}
    else:
        return {"status": "rules_only", "request_attempted": False}
    try:
        return {
            "status": "ok",
            "request_attempted": True,
            "answer": validate_answer(answer, config, packet),
        }
    except (ValueError, KeyError, TypeError):
        return {"status": "invalid_response", "request_attempted": True}
