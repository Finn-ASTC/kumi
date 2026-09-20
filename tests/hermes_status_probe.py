"""No-model, unchanged-profile matrix through a real Hermes Python environment."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def fingerprints(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path, required=True)
    parser.add_argument("--aux-root", type=Path, help="optional known auxiliary-capable checkout")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--skills", type=Path, default=Path(__file__).resolve().parents[1] / "skills")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="status-probe-", dir=args.root)).resolve()
    profiles = root / "profiles"
    for name in ("a", "b", "modified", "malformed", "missing"):
        home = profiles / name
        home.mkdir(parents=True)
        enabled = "[]" if name == "b" else "[orch-usage]"
        config = "plugins:\n  enabled: " + enabled + "\n"
        if name == "malformed":
            config = "plugins: [PRIVATE invalid yaml\n"
        (home / "config.yaml").write_text(config)
        if name != "missing":
            shutil.copytree(args.skills / "agent-hermes/plugins/orch-usage", home / "plugins/orch-usage",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if name == "modified":
            (home / "plugins/orch-usage/__init__.py").write_text("raise RuntimeError('PRIVATE must not execute')")
        if name == "b":
            (home / "usage-hooks").mkdir()
            (home / "usage-hooks/events.sqlite3").write_text("PRIVATE historical bytes, not a validated database")
    before = fingerprints(profiles)
    results = []
    env = {**os.environ, "HERMES_SAFE_MODE": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("a", "b", "a", "modified", "malformed", "missing"):
        command = [sys.executable, str(args.skills.resolve() / "agent-orchestrator/scripts/hermes_status.py"),
                   "--hermes-root", str(args.hermes_root.resolve()), "--profile-home", str(profiles / name)]
        result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0 and not result.stderr, result.stderr
        assert "PRIVATE" not in result.stdout
        report = json.loads(result.stdout)
        assert report["layers"]["main_loop"]["status"] == {
            "a": "candidate", "b": "unavailable", "modified": "unknown",
            "malformed": "unknown", "missing": "unavailable"}[name]
        assert not report["capture_verified"] and report["totals"] is None
        assert not report["layers"]["orchestration"]["metering_required"]
        results.append(dict(profile=name, command=command, report=report))
    if args.aux_root:
        command = [sys.executable, str(args.skills.resolve() / "agent-orchestrator/scripts/hermes_status.py"),
                   "--hermes-root", str(args.aux_root.resolve()), "--profile-home", str(profiles / "a")]
        result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0 and not result.stderr, result.stderr
        report = json.loads(result.stdout)
        assert report["layers"]["auxiliary"]["status"] == "candidate"
        assert not report["capture_verified"]
        results.append(dict(profile="a", command=command, report=report))
    assert before == fingerprints(profiles), "status changed a profile"
    output = dict(status="passed", root=str(root), profiles_unchanged=True, model_calls=0,
                  plugin_discovery=False, results=results)
    (root / "result.json").write_text(json.dumps(output, indent=2))
    print(json.dumps(dict(status="passed", root=str(root), profiles_unchanged=True, checks=len(results))))


if __name__ == "__main__":
    main()
