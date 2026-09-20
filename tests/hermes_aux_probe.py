"""Optional native auxiliary probe; --template-home makes two real model requests."""

import argparse
from contextlib import closing
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
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
    parser.add_argument("--template-home", type=Path, help="copy this profile's model/approval configuration for LIVE calls")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="aux-probe-", dir=args.root)).resolve()
    root.chmod(0o700)
    homes = {name: root / name for name in ("a", "b", "off")}
    import yaml
    for name, home in homes.items():
        home.mkdir(mode=0o700)
        config = {}
        if args.template_home:
            config = yaml.safe_load((args.template_home / "config.yaml").read_text())
            for filename in (".env", "auth.json"):
                source = args.template_home / filename
                if source.is_file():
                    shutil.copyfile(source, home / filename)
                    (home / filename).chmod(0o600)
        # This library-only probe loads only the collector. Model, auxiliary,
        # approval and security configuration remain byte-equivalent after YAML parsing.
        config["plugins"] = {"enabled": [] if name == "off" else ["orch-usage"],
                             "allow_tool_override": False}
        (home / "config.yaml").write_text(yaml.safe_dump(config))
        (home / "config.yaml").chmod(0o600)
        shutil.copytree(args.skills / "agent-hermes/plugins/orch-usage", home / "plugins/orch-usage",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.environ["HERMES_HOME"] = str(homes["a"])
    sys.path.insert(0, str(args.hermes_root.resolve()))
    if args.template_home:
        from dotenv import load_dotenv
        load_dotenv(homes["a"] / ".env")
    from agent.aux_accounting import set_accounting_context, reset_accounting_context
    from agent.auxiliary_client import _validate_llm_response
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli import plugins
    from hermes_state import SessionDB
    from agent.secret_scope import set_multiplex_active
    if not args.template_home:
        set_multiplex_active(True)
    results = []
    sequence = ("a",) if args.template_home else ("off", "a", "b", "a")
    for name in sequence:
        home = homes[name]
        token = set_hermes_home_override(home)
        db = SessionDB(home / "state.db")
        db.create_session("aux-probe-session", source="cli")
        context = set_accounting_context(db, "aux-probe-session")
        try:
            plugins.discover_plugins()
            assert plugins.has_hook("on_aux_usage") == (name != "off")
            if args.template_home:
                from agent.title_generator import generate_title
                from tools.approval_smart import _smart_approve
                title = generate_title("Verify exact monetary parsing in a small Python command line program", timeout=45)
                verdict = _smart_approve("python -c 'print(1 + 1)'", "Read-only arithmetic test; command will not be executed")
                results.append(dict(title_generated=bool(title), approval_verdict=verdict))
            else:
                for task in ("title_generation", "approval"):
                    response = SimpleNamespace(model="fixture", usage=SimpleNamespace(prompt_tokens=100,
                        completion_tokens=20, total_tokens=120), choices=[SimpleNamespace(
                        message=SimpleNamespace(content="PRIVATE"))])
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        assert pool.submit(copy_context().run, _validate_llm_response, response, task).result() is response
        finally:
            reset_accounting_context(context)
            db.close()
            reset_hermes_home_override(token)
    reports = []
    for name in (("a",) if args.template_home else ("a", "b")):
        home = homes[name]
        path = home / "usage-hooks/auxiliary.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            events = db.execute("SELECT observation_id,session_id,payload FROM orch_aux_events ORDER BY seq").fetchall()
        assert {json.loads(row[2])["task"] for row in events} == {"title_generation", "approval"}
        assert len(events) == (2 if args.template_home or name == "b" else 4)
        token = set_hermes_home_override(home)
        try:
            for observation, sid, payload in events:
                plugins.invoke_hook("on_aux_usage", observation_id=observation, session_id=sid,
                                    turn_id=None, **json.loads(payload))
        finally:
            reset_hermes_home_override(token)
        backup = home / "aux-backup.sqlite3"
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        commands = []
        for source in (path, backup):
            command = [sys.executable, str(args.skills.resolve() / "agent-orchestrator/scripts/usage.py"),
                       "hermes-aux", "--log", str(source), "--session-id", "aux-probe-session"]
            result = subprocess.run(command, cwd=root, text=True, capture_output=True)
            commands.append(dict(argv=command, code=result.returncode, stdout=result.stdout, stderr=result.stderr))
            assert result.returncode == 0, result.stderr
        (home / "commands.json").write_text(json.dumps(commands, indent=2))
        report, duplicate = [json.loads(command["stdout"]) for command in commands]
        assert report == duplicate
        assert report["observed_responses"] == len(events) and report["unknown_responses"] == 0
        assert report["totals"] is None and not report["coverage_complete"]
        with closing(sqlite3.connect(home / "state.db")) as db:
            native = db.execute("SELECT task,input_tokens,cache_read_tokens,cache_write_tokens,output_tokens "
                                "FROM session_model_usage WHERE session_id='aux-probe-session'").fetchall()
        for task, uncached, read, write, output in native:
            observed = [r for r in report["receipts"] if r["task"] == task]
            assert sum(r["input_tokens"] for r in observed) == uncached + read + write
            assert sum(r["output_tokens"] for r in observed) == output
        (home / "inspection.json").write_text(json.dumps(report, indent=2))
        reports.append(dict(profile=name, observed_responses=report["observed_responses"],
                            observed_totals=report["observed_totals"], native_cumulative_matches=True))
    assert not (homes["off"] / "usage-hooks").exists()
    output = dict(status="passed", root=str(root), live_model_calls=bool(args.template_home),
                  profile_sequence=list(sequence), reports=reports, outcomes=results,
                  repeated_delivery_deduplicated=True, backup_receipts_identical=True,
                  command_executed=False, coverage_complete=False)
    (root / "result.json").write_text(json.dumps(output, indent=2))
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
