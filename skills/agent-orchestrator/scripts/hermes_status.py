"""Inspect optional metering prerequisites in the intended Hermes Python environment.

Run in a fresh process with explicit checkout and profile paths. This does not
discover plugins, execute callbacks, inspect conversations, or verify capture.
"""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import importlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

MAIN_HOOKS = frozenset({"pre_llm_call", "pre_api_request", "post_api_request",
                        "api_request_error", "on_session_end"})


def host_capabilities(root: Path) -> dict[str, Any]:
    """Read the host's advertised names, without treating them as a semantic test."""
    unknown = dict(status="unknown", main_loop_hooks=None, auxiliary_hook=None,
                   missing_main_hooks=None, reason="host_import_failed")
    expected = root / "hermes_cli/plugins.py"
    if not expected.is_file():
        return {**unknown, "reason": "host_source_missing"}
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            module = importlib.import_module("hermes_cli.plugins")
        if Path(module.__file__).resolve() != expected.resolve():
            return {**unknown, "reason": "host_origin_mismatch"}
        hooks = module.VALID_HOOKS
        if not isinstance(hooks, (set, frozenset, list, tuple)) or not all(isinstance(h, str) for h in hooks):
            return {**unknown, "reason": "host_registry_unrecognized"}
        missing = sorted(MAIN_HOOKS - set(hooks))
        return dict(status="inspected", main_loop_hooks=not missing, auxiliary_hook="on_aux_usage" in hooks,
                    missing_main_hooks=missing, reason=None)
    except (Exception, SystemExit):
        # Native import errors can carry config/endpoint text. Report only a stable
        # category, never raw exception messages or captured host stdout/stderr.
        return unknown


def configured_plugin(home: Path) -> tuple[bool | None, str]:
    """Inspect explicit YAML opt-in only; native migration/discovery may differ."""
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            yaml = importlib.import_module("yaml")
            config = yaml.safe_load((home / "config.yaml").read_text())
        if not isinstance(config, dict) or not isinstance(config.get("plugins"), dict):
            return None, "configuration_unknown"
        plugin = config["plugins"]
        enabled, disabled = plugin.get("enabled"), plugin.get("disabled", [])
        if not isinstance(disabled, list) or not all(isinstance(item, str) for item in disabled):
            return None, "configuration_unknown"
        if "orch-usage" in disabled:
            return False, "explicitly_disabled"
        if not isinstance(enabled, list) or not all(isinstance(item, str) for item in enabled):
            return None, "configuration_unknown"
        return (True, "explicitly_enabled") if "orch-usage" in enabled else (False, "not_enabled")
    except (Exception, SystemExit):
        return None, "configuration_unreadable"


def plugin_files(home: Path) -> str:
    """Compare this package's entire plugin with the profile-local installation."""
    source = Path(__file__).resolve().parents[2] / "agent-hermes/plugins/orch-usage"
    installed = home / "plugins/orch-usage"

    def files(directory: Path) -> set[str]:
        return {p.relative_to(directory).as_posix() for p in directory.rglob("*")
                if p.is_file() and "__pycache__" not in p.relative_to(directory).parts and p.suffix != ".pyc"}

    try:
        expected = files(source)
        if not {"__init__.py", "plugin.yaml"}.issubset(expected):
            return "unreadable"
        if not installed.exists():
            return "different" if installed.is_symlink() else "missing"
        if not installed.is_dir():
            return "different"
        if files(installed) != expected:
            return "different"
        return "matches_package" if all((source / name).read_bytes() == (installed / name).read_bytes()
                                        for name in expected) else "different"
    except OSError:
        return "unreadable"


def store_presence(home: Path, name: str) -> str:
    """Check presence only; retained or empty stores prove neither capture nor zero cost."""
    try:
        directory = home / "usage-hooks"
        if directory.is_symlink():
            return "redirected_uninspected"
        mode = (directory / name).lstat().st_mode
        if stat.S_ISLNK(mode):
            return "redirected_uninspected"
        return "present_uninspected" if stat.S_ISREG(mode) else "not_regular"
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


def layer(hooks: bool | None, files: str, enabled: bool | None, safe_mode: bool) -> dict[str, Any]:
    """Negative facts deny only optional metering; unknown prerequisites stay unknown."""
    reasons = []
    if hooks is not True:
        reasons.append("native_hooks_missing" if hooks is False else "native_hooks_unknown")
    if files != "matches_package":
        reasons.append("plugin_" + files)
    if enabled is not True:
        reasons.append("plugin_not_enabled" if enabled is False else "plugin_configuration_unknown")
    if safe_mode:
        reasons.append("host_safe_mode")
    unavailable = hooks is False or files == "missing" or enabled is False or safe_mode
    return dict(status="unavailable" if unavailable else "unknown" if reasons else "candidate",
                reasons=reasons)


def inspect(root: Path, home: Path) -> dict[str, Any]:
    """Return independent prerequisites and presence observations, never a launch gate."""
    root, home = root.resolve(), home.resolve()
    # This tool is a fresh-process probe. Pin profile for imports; avoid native
    # config loaders (which may migrate/write) and disable import-time pyc writes.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    os.environ["HERMES_HOME"] = str(home)
    host = host_capabilities(root)
    enabled, config_reason = configured_plugin(home)
    files = plugin_files(home)
    safe_mode = os.environ.get("HERMES_SAFE_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
    return dict(version=1, hermes_root=str(root), profile_home=str(home), python=sys.executable,
        host=host, plugin=dict(files=files, configured_enabled=enabled, configuration_reason=config_reason,
                               discovery="not_run", safe_mode=safe_mode),
        layers=dict(orchestration=dict(metering_required=False, health="not_checked"),
                    main_loop=layer(host["main_loop_hooks"], files, enabled, safe_mode),
                    auxiliary=layer(host["auxiliary_hook"], files, enabled, safe_mode)),
        stores=dict(main_loop=store_presence(home, "events.sqlite3"),
                    auxiliary=store_presence(home, "auxiliary.sqlite3")),
        capture_verified=False, coverage_complete=False, totals=None,
        limitations=["advertised_names_not_semantic_contract", "plugin_discovery_not_verified",
                     "full_host_dependencies_not_checked", "running_process_not_inspected",
                     "stores_not_validated", "hook_delivery_not_verified"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path, required=True, help="absolute intended Hermes source checkout")
    parser.add_argument("--profile-home", type=Path, required=True, help="absolute already-resolved target profile")
    args = parser.parse_args()
    if not args.hermes_root.is_absolute() or not args.profile_home.is_absolute():
        parser.error("Hermes checkout and profile home must be absolute paths")
    try:
        print(json.dumps(inspect(args.hermes_root, args.profile_home), ensure_ascii=False, allow_nan=False))
        return 0
    except OSError:
        print(json.dumps(dict(error="status_paths_unreadable")), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
