"""No-model integration through Hermes discovery, profile scopes and real API hook emitters."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--skills", type=Path, default=Path(__file__).resolve().parents[1] / "skills")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="hook-probe-", dir=args.root)).resolve()
    skills = args.skills.resolve()
    homes = {name: root / name for name in ("a", "b", "off")}
    for name, home in homes.items():
        (home / "plugins").mkdir(parents=True)
        shutil.copytree(skills / "agent-hermes/plugins/orch-usage", home / "plugins/orch-usage",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (home / "config.yaml").write_text("plugins:\n  enabled: " + ("[]" if name == "off" else "[orch-usage]") + "\n")
    os.environ["HERMES_HOME"] = str(homes["off"])
    sys.path.insert(0, str(args.hermes_root.resolve()))
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import set_multiplex_active
    from hermes_cli import lifecycle, plugins
    from agent.api_request_hooks import ApiRequestHooksMixin
    from agent.turn_api_request import _fire_pre_api_request_hook
    from agent.turn_response_intake import _fire_post_api_request_hook
    set_multiplex_active(True)

    class ProbeAgent(ApiRequestHooksMixin, SimpleNamespace):
        pass

    agent = ProbeAgent(session_id="fixture-session", platform="cli", model="fixture-model", provider="openai",
                       base_url="https://SECRET.invalid", api_mode="chat_completions", max_tokens=20, tools=[], client=None)
    response = SimpleNamespace(model="fixture-model", id="fixture-response", usage=SimpleNamespace(
        prompt_tokens=100, completion_tokens=20, total_tokens=120,
        prompt_tokens_details=SimpleNamespace(cached_tokens=60, cache_write_tokens=10),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=8)))
    message = SimpleNamespace(content="PRIVATE RESPONSE", tool_calls=None, role="assistant")

    for name, turn in (("off", "off-turn"), ("a", "a1"), ("b", "b1"), ("a", "a2")):
        token = set_hermes_home_override(homes[name])
        try:
            plugins.discover_plugins()
            assert plugins.has_hook("post_api_request") == (name != "off")
            lifecycle.invoke_hook("pre_llm_call", session_id=agent.session_id, turn_id=turn, task_id="fixture-task",
                                  user_message="PRIVATE USER", model=agent.model, platform="cli")
            context = dict(api_call_count=1, api_request_id=turn + ":api:1", effective_task_id="fixture-task", turn_id=turn)
            _fire_pre_api_request_hook(agent, {"model": agent.model, "messages": []}, [], [], messages=[],
                original_user_message="PRIVATE USER", approx_tokens=123, total_chars=500, retry_count=0,
                api_start_time=1.0, **context)
            _fire_post_api_request_hook(agent, response, message, "stop", api_messages=[],
                                       api_duration=0.1, api_start_time=1.0, **context)
            lifecycle.invoke_hook("on_session_end", session_id=agent.session_id, turn_id=turn,
                                  completed=True, failed=False, interrupted=False, model=agent.model, platform="cli")
        finally:
            reset_hermes_home_override(token)
    assert not (homes["off"] / "usage-hooks").exists()
    transcript = []

    def cli(tool, *argv):
        command = [sys.executable, str(skills / "agent-orchestrator/scripts" / tool), *map(str, argv)]
        result = subprocess.run(command, cwd=root, text=True, capture_output=True)
        transcript.append(dict(argv=command, code=result.returncode, stdout=result.stdout, stderr=result.stderr))
        (root / "commands.json").write_text(json.dumps(transcript, indent=2))
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    task = root / "task.txt"
    task.write_text("Synthetic recorded-hook integration; no provider request was made.")
    request = cli("protocol.py", "prepare", "--cwd", root, "--root", root, "--parent-depth", 0,
                  "--task-file", task, "--brief")
    for name, expected in (("a", 2), ("b", 1)):
        database = homes[name] / "usage-hooks/events.sqlite3"
        inspection = cli("usage.py", "inspect-native", "--host", "hermes", "--log", database,
                         "--session-id", agent.session_id)
        assert len(inspection["turns"]) == expected, inspection
        assert all(t["calls"] == 1 for t in inspection["turns"]), inspection
        assert not inspection["integrity_issues"], inspection
        with closing(sqlite3.connect(database)) as db:
            data = json.dumps(db.execute("SELECT * FROM orch_usage_events").fetchall())
            assert "PRIVATE" not in data and "SECRET" not in data
        if name == "a":
            manifest = root / "import.json"
            manifest.write_text(json.dumps(dict(version=1, host="hermes", log_path=str(database),
                session_id=agent.session_id, mappings=[dict(native_id="a1", request_path=request["request_path"],
                attempt_id="fixture", purpose="task", role="author", phase="verification")])))
            assert cli("usage.py", "import-native", "--manifest", manifest, "--dry-run")["selected_calls"] == 1
            assert cli("usage.py", "import-native", "--manifest", manifest)["imported"] == 1
            assert cli("usage.py", "import-native", "--manifest", manifest)["duplicates"] == 1
    summary = cli("usage.py", "summary", "--request", request["request_path"])
    assert summary["totals"]["input_tokens"] == 100, summary
    report = dict(status="passed", root=str(root), real_plugin_discovery=True, real_hook_emitters=True,
        profile_sequence=["a", "b", "a"], disabled_profile_no_store=True, native_model_calls=0,
        imported_scoped_tokens=summary["totals"], note="Synthetic provider response; no full native model run or all-host coverage claim.")
    (root / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
