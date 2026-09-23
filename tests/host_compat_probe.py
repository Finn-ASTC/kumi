"""Inspect installed CLI versions/help; no model tasks or approval actions.

Development-only probe. A matching CLI surface never certifies native behavior.
Host executables may perform their own startup/cache work even for --help.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any


# Last observations before the 2026-09-23 audit, not minimum supported versions.
BASELINES = {"codex": "0.155.0", "omp": "18.2.6", "hermes": "0.21.3",
             "opencode": "1.18.29", "herdr": "0.9.1", "tmux": "3.7c"}
VERSIONS = {
    "codex": r"^codex-cli (\d+\.\d+\.\d+(?:[-+][\w.-]+)?)\s*$",
    "omp": r"^omp[/ v]+(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)(?:\s|$)",
    "hermes": r"^Hermes Agent v(\d+\.\d+\.\d+)(?:\s|$)",
    "opencode": r"^(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)\s*$",
    "herdr": r"^herdr (\d+\.\d+\.\d+)(?:\s|$)",
    "tmux": r"^tmux (\d+\.\d+[a-z]?)(?:\s|$)",
}
SURFACES = {
    "codex": [
        (("--help",), ("--sandbox", "--add-dir", "--cd", "--ask-for-approval", "--no-alt-screen")),
        (("exec", "--help"), ("--sandbox", "--add-dir", "--cd", "--json", "--output-last-message")),
        (("resume", "--help"), ("--sandbox", "--add-dir", "--cd")),
    ],
    "omp": [
        (("--help",), ("--session-dir", "--resume", "--profile", "--config", "--approval-mode", "--no-title")),
        (("ps", "--help"), ("--dir", "--json", "--timeout")),
    ],
    "hermes": [(("--help",), ("--resume", "--in", "--cli", "--pass-session-id"))],
    "opencode": [(("run", "--help"), ("--pure", "--session", "--format", "--dir", "--agent"))],
    "herdr": [
        (("tab", "create", "--help"), ("--workspace", "--cwd", "--label", "--no-focus")),
        (("workspace", "create", "--help"), ("--cwd", "--label", "--no-focus")),
    ],
    "tmux": [],  # Real socket/transport checks live in test_tmux_integration.py.
}
OUTPUT_LIMIT = 512 * 1024


def capture(argv: list[str], timeout: float) -> dict[str, Any]:
    """Retain bounded help text internally and only hashes/error categories publicly."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        status, code = "ok", None
        try:
            completed = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=stdout,
                                       stderr=stderr, timeout=timeout, check=False)
            code = completed.returncode
            if code:
                status = "command_failed"
        except subprocess.TimeoutExpired:
            status = "timeout"
        except OSError:
            status = "execution_failed"
        stdout.seek(0)
        raw = stdout.read(OUTPUT_LIMIT + 1)
        stderr.seek(0)
        errors = stderr.read(OUTPUT_LIMIT + 1)
    truncated = len(raw) > OUTPUT_LIMIT or len(errors) > OUTPUT_LIMIT
    if truncated and status == "ok":
        status = "output_limit"
    return {"status": status, "exit_code": code,
            "text": (raw[:OUTPUT_LIMIT] + b"\n" + errors[:OUTPUT_LIMIT]).decode("utf-8", "replace"),
            "stdout_sha256": hashlib.sha256(raw[:OUTPUT_LIMIT]).hexdigest(),
            "stderr_sha256": hashlib.sha256(errors[:OUTPUT_LIMIT]).hexdigest(),
            "truncated": truncated, "stderr_present": bool(errors)}


def parse_version(host: str, output: str) -> str | None:
    """Ignore dependency versions and update banners; require one host version."""
    matches = re.findall(VERSIONS[host], output, re.MULTILINE)
    return matches[0] if len(matches) == 1 else None


def help_options(output: str) -> set[str]:
    """Read option declaration lines, excluding prose/example substring matches."""
    options = set()
    for line in output.splitlines():
        if re.match(r"^\s*(?:-[A-Za-z0-9],\s*)?--[a-z]", line):
            declaration = re.split(r"\s{2,}", line.strip(), maxsplit=1)[0]
            options.update(re.findall(r"--[a-z][a-z0-9-]*", declaration))
    return options


def inspect_host(host: str, executable: str | None = None, timeout: float = 15) -> dict[str, Any]:
    """Check a fixed argv allowlist without using a shell or modifying settings."""
    path = shutil.which(executable or host)
    report: dict[str, Any] = {"host": host, "executable": path, "version": None,
                             "previous_observed_version": BASELINES[host], "version_changed": None,
                             "status": "missing", "native_behavior_verified": False, "checks": []}
    if path is None:
        return report
    path = str(Path(path).absolute())
    report["executable"] = path
    version = capture([path, "-V" if host == "tmux" else "--version"], timeout)
    report["version_probe"] = {k: v for k, v in version.items() if k != "text"}
    if version["status"] == "ok":
        report["version"] = parse_version(host, version["text"])
    if report["version"] is not None:
        report["version_changed"] = report["version"] != BASELINES[host]
    else:
        report["status"] = "unknown_version"
        return report
    for args, required in SURFACES[host]:
        result = capture([path, *args], timeout)
        missing = sorted(set(required) - help_options(result["text"]))
        report["checks"].append({"argv": list(args), "required_options": list(required),
                                  "missing_options": missing,
                                  **{k: v for k, v in result.items() if k != "text"}})
    report["status"] = "advertised" if all(
        c["status"] == "ok" and not c["missing_options"] for c in report["checks"]
    ) else "needs_review"
    if not report["checks"]:
        report["status"] = "version_only"
    return report


def inspect_omo(path: Path) -> dict[str, Any]:
    """Read explicitly selected package metadata, not config, tokens or CLI installers."""
    report: dict[str, Any] = {"host": "omo", "status": "unknown", "version": None,
                             "previous_observed_version": "4.19.4", "version_changed": None,
                             "native_behavior_verified": False, "loaded_plugin_verified": False}
    try:
        with path.open("rb") as stream:
            raw = stream.read(OUTPUT_LIMIT + 1)
        if len(raw) > OUTPUT_LIMIT:
            return report
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get("name") not in ("oh-my-openagent", "oh-my-opencode")
                or not isinstance(data.get("version"), str)
                or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", data["version"])):
            return report
        report.update(status="metadata_only", version=data["version"],
                      version_changed=data["version"] != "4.19.4")
    except (OSError, ValueError):
        pass
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=tuple(BASELINES), action="append", help="repeat; default all")
    parser.add_argument("--executable", action="append", default=[], metavar="HOST=PATH")
    parser.add_argument("--omo-package-json", type=Path, help="optional installed package, not cache wrapper")
    args = parser.parse_args()
    overrides = {}
    for override in args.executable:
        host, sep, path = override.partition("=")
        if not sep or host not in BASELINES or not path:
            parser.error("--executable requires a known HOST and nonempty PATH")
        overrides[host] = path
    hosts = list(dict.fromkeys(args.host or BASELINES))
    reports = [inspect_host(host, overrides.get(host)) for host in hosts]
    if args.omo_package_json:
        reports.append(inspect_omo(args.omo_package_json))
    print(json.dumps({"schema_version": 1, "observed_at": datetime.now(timezone.utc).isoformat(),
                      "baseline_date": "2026-09-20", "model_tasks_started": 0, "hosts": reports,
                      "limits": ["help_is_not_native_behavior", "no_upstream_latest_check",
                                 "no_approval_or_stop_actions", "no_running_session_inspection",
                                 "host_startup_side_effects_possible"]}, indent=2))
    return int(any(r["status"] not in ("advertised", "version_only", "metadata_only") for r in reports))


if __name__ == "__main__":
    raise SystemExit(main())
