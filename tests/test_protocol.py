"""Behavioral regression checks; no live agents, servers or external configuration."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "skills/agent-orchestrator/scripts/protocol.py"
SPEC = importlib.util.spec_from_file_location("orch_protocol", TOOL)
protocol = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(protocol)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orch-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / "project with spaces"
        self.cwd.mkdir()
        self.task = self.root / "task.txt"
        self.task.write_text("Review this project without editing files.", encoding="utf-8")
        self.info = self.prepare()
        self.request = protocol.load_request(self.info["request_path"])

    def prepare(self, **overrides):
        values = dict(cwd=str(self.cwd), parent_depth=0, max_depth=None, previous=None,
                      task_file=str(self.task), root=str(self.root))
        values.update(overrides)
        return protocol.prepare(argparse.Namespace(**values))

    def response(self, **overrides):
        result = {field: self.request[field] for field in protocol.IDENTITY_FIELDS}
        result.update(status="success", output="No edits required.", error=None,
                      blocked_reason=None, completed_at="2026-09-17T12:00:00Z")
        result.update({field: [] for field in protocol.FILE_FIELDS})
        result.update(overrides)
        return result

    def validate(self, result, check_files=False):
        return protocol.validate_result(self.request, result, check_files)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(TOOL), *map(str, args)],
                              capture_output=True, text=True)

    def record(self, **overrides):
        values = dict(request=self.info["request_path"], mode="insider", session="user-session",
                      agent="child", pane="w1:p2", workspace=None, owns_agent=True,
                      owns_pane=True, owns_workspace=False, owns_session=False)
        values.update(overrides)
        return protocol.record(argparse.Namespace(**values))

    def cleanup(self):
        return protocol.cleanup_plan(argparse.Namespace(request=self.info["request_path"]))

    def test_read_only_success_is_valid(self):
        self.assertEqual(self.validate(self.response(), check_files=True)["status"], "success")

    def test_published_report_correction_is_taught_and_uses_a_fresh_round(self):
        rules = Path(self.info['prompt_path']).read_text().split('REQUEST=', 1)[0]
        for instruction in ('error after publishing', 'state the correction in normal output',
                            'wait for the controller', 'new round/path'):
            self.assertIn(instruction, rules)
        original = self.response(output='Artifact is correct; reported hash has a typo.')
        protocol.publish(self.info['result_path'], original)
        before = Path(self.info['result_path']).read_bytes()
        candidate = self.root / 'correction.json'
        candidate.write_text(json.dumps(self.response(output='Corrected artifact hash.')))
        denied = self.cli('write-result', '--request', self.info['request_path'], '--input', candidate)
        self.assertNotEqual(denied.returncode, 0)
        self.task.write_text('Correct the reported hash only; preserve artifacts and the previous response.')
        follow = self.prepare(cwd=None, parent_depth=None, previous=self.info['request_path'])
        corrected = self.response(job_id=follow['job_id'], round_id=follow['round_id'],
                                  output='Correction to previous round: the artifact hash is now verified.')
        candidate.write_text(json.dumps(corrected))
        written = self.cli('write-result', '--request', follow['request_path'], '--input', candidate)
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(Path(self.info['result_path']).read_bytes(), before)
        self.assertEqual(follow['job_id'], self.info['job_id'])
        self.assertNotEqual(follow['round_id'], self.info['round_id'])
        self.assertEqual(protocol.read_json(follow['result_path']), corrected)

    def test_status_reason_contract(self):
        for status, values in (("blocked", {"blocked_reason": "Which format?"}),
                               ("error", {"error": "Compiler unavailable"})):
            with self.subTest(status=status):
                self.validate(self.response(status=status, **values))
        for changes in ({"status": "idle"}, {"status": "blocked"}, {"status": "error"},
                        {"error": "unexpected"}, {"blocked_reason": "unexpected"},
                        {"status": "blocked", "blocked_reason": "   "}, {"output": []}):
            with self.subTest(changes=changes), self.assertRaises(protocol.ProtocolError):
                self.validate(self.response(**changes))

    def test_required_fields_and_types(self):
        for field in self.response():
            result = self.response()
            del result[field]
            with self.subTest(field=field), self.assertRaises(protocol.ProtocolError):
                self.validate(result)
        for field in protocol.FILE_FIELDS:
            with self.subTest(field=field), self.assertRaises(protocol.ProtocolError):
                self.validate(self.response(**{field: "not an array"}))

    def test_wrong_identity_and_unsupported_version_rejected(self):
        for changes in ({"round_id": "0" * 32}, {"job_id": "0" * 32},
                        {"schema_version": 2}, {"schema_version": True}, {"schema_version": "1"}):
            with self.subTest(changes=changes), self.assertRaises(protocol.ProtocolError):
                self.validate(self.response(**changes))

    def test_timestamps_require_timezone(self):
        for stamp in ("2026-09-17", "2026-09-17T12:00:00", "not a timestamp", 7):
            with self.subTest(stamp=stamp), self.assertRaises(protocol.ProtocolError):
                self.validate(self.response(completed_at=stamp))
        self.validate(self.response(completed_at="2026-09-17T20:00:00+08:00"))

    def test_prompt_encodes_task_and_actual_path_without_shell_evaluation(self):
        marker = self.root / "must-not-exist"
        task = f"第一行\n'quoted' `touch {marker}` $(touch {marker})\r\n\tend\u2028next"
        self.task.write_text(task, encoding="utf-8", newline="")
        info = self.prepare()
        prompt = Path(info["prompt_path"]).read_text()
        self.assertEqual(len(prompt.splitlines()), 1)
        envelope = json.loads(prompt.split("REQUEST=", 1)[1])
        self.assertEqual(envelope["task"], task)
        self.assertEqual(envelope["result_path"], info["result_path"])
        self.assertNotIn("$AGENT_RESULT_FILE", prompt)
        result = subprocess.run(["bash", "-c", 'p=$(cat "$1"); printf "%s" "$p"',
                                 "bash", info["prompt_path"]], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, prompt)
        self.assertFalse(marker.exists())

    def test_round_directories_private_unique_and_result_absent(self):
        other = self.prepare()
        self.assertNotEqual(other["job_id"], self.info["job_id"])
        self.assertNotEqual(other["result_path"], self.info["result_path"])
        self.assertFalse(Path(other["result_path"]).exists())
        self.assertEqual(Path(other["request_path"]).parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(Path(other["request_path"]).stat().st_mode & 0o777, 0o600)

    def test_followup_does_not_consume_old_success_or_blocked(self):
        for status in ("success", "blocked", "error"):
            with self.subTest(status=status):
                old = self.prepare()
                response = self.response(status=status, job_id=old["job_id"], round_id=old["round_id"],
                                         blocked_reason="Which format?" if status == "blocked" else None,
                                         error="Missing compiler" if status == "error" else None)
                protocol.publish(old["result_path"], response)
                new = self.prepare(cwd=None, parent_depth=None, previous=old["request_path"])
                self.assertEqual(new["job_id"], old["job_id"])
                self.assertEqual(new["depth"], old["depth"])
                self.assertNotEqual(new["round_id"], old["round_id"])
                self.assertFalse(Path(new["result_path"]).exists())
                self.assertEqual(protocol.read_json(old["result_path"]), response)
                with self.assertRaises(protocol.ProtocolError):
                    protocol.validate_result(protocol.load_request(new["request_path"]), response)

    def test_followup_requires_previous_response_and_no_depth_override(self):
        with self.assertRaises(FileNotFoundError):
            self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        protocol.publish(self.info["result_path"], self.response())
        with self.assertRaises(protocol.ProtocolError):
            self.prepare(previous=self.info["request_path"])

    def test_nested_depth_and_siblings(self):
        child = self.prepare(parent_depth=self.info["depth"], max_depth=self.info["max_depth"])
        self.assertEqual(child["depth"], 2)
        snapshot = set(self.root.iterdir())
        with self.assertRaises(protocol.ProtocolError):
            self.prepare(parent_depth=child["depth"], max_depth=child["max_depth"])
        self.assertEqual(set(self.root.iterdir()), snapshot)
        self.assertEqual(self.prepare()["depth"], 1)
        self.assertEqual(self.prepare(max_depth=4, parent_depth=2)["depth"], 3)

    def test_missing_or_negative_parent_depth_rejected(self):
        for depth in (None, -1):
            with self.subTest(depth=depth), self.assertRaises(protocol.ProtocolError):
                self.prepare(parent_depth=depth)

    def test_paths_reject_absolute_traversal_and_non_normalized_forms(self):
        for name in ("/tmp/out", "../out", "a/../../out", "a/../b", "./a", "a//b", "a/",
                     ".", "", "a\\b", "a\x00b", 4):
            with self.subTest(name=name), self.assertRaises(protocol.ProtocolError):
                self.validate(self.response(files_created=[name]))

    def test_paths_cannot_escape_through_symlinks(self):
        (self.cwd / "escape").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(protocol.ProtocolError):
            self.validate(self.response(files_created=["escape/task.txt"]))

    def test_duplicate_and_overlapping_file_declarations(self):
        for changes in ({"files_created": ["a", "a"]},
                        {"files_created": ["a"], "files_modified": ["a"]},
                        {"files_generated": ["a"], "files_deleted": ["a"]}):
            with self.subTest(changes=changes), self.assertRaises(protocol.ProtocolError):
                self.validate(self.response(**changes))

    def test_files_checked_exactly_without_extension_glob(self):
        (self.cwd / "工具 with spaces").write_text("hello")
        result = self.response(files_created=["工具 with spaces"], files_deleted=["old.txt"])
        self.validate(result, check_files=True)
        (self.cwd / "old.txt").write_text("still here")
        with self.assertRaises(protocol.ProtocolError):
            self.validate(result, check_files=True)
        with self.assertRaises(protocol.ProtocolError):
            self.validate(self.response(files_created=["missing.py"]), check_files=True)
        with self.assertRaises(protocol.ProtocolError):
            self.validate(self.response(files_created=["folder"]), check_files=True)

    def test_invalid_json_duplicate_keys_and_nonfinite_numbers(self):
        path = self.root / "bad.json"
        for raw in ('{"status":"success", "status":"error"}', '{"x":NaN}', '{"x":Infinity}',
                    '[]', '```json\n{}\n```', '{"partial":'):
            path.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                protocol.read_json(path)

    def test_atomic_publication_never_overwrites_existing_response(self):
        path = self.info["result_path"]
        response = self.response(status="blocked", blocked_reason="Which format?")
        protocol.publish(path, response)
        with self.assertRaises(FileExistsError):
            protocol.publish(path, self.response())
        self.assertEqual(protocol.read_json(path), response)
        self.assertFalse(list(Path(path).parent.glob(".result.json.*")))

    def test_concurrent_publishers_have_exactly_one_winner(self):
        candidate = self.root / "candidate.json"
        candidate.write_text(json.dumps(self.response()))
        def publish_once(_):
            return self.cli("write-result", "--request", self.info["request_path"], "--input", candidate)
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(publish_once, range(4)))
        self.assertEqual(sum(item.returncode == 0 for item in outcomes), 1)
        self.assertEqual(protocol.read_json(self.info["result_path"]), self.response())

    def test_cli_missing_invalid_and_valid_blocked_are_distinct(self):
        args = ("validate", "--request", self.info["request_path"])
        self.assertEqual(self.cli(*args).returncode, 2)
        Path(self.info["result_path"]).write_text("{broken")
        self.assertEqual(self.cli(*args).returncode, 3)
        Path(self.info["result_path"]).unlink()
        protocol.publish(self.info["result_path"], self.response(status="blocked", blocked_reason="Which format?"))
        valid = self.cli(*args)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertEqual(json.loads(valid.stdout)["status"], "blocked")

    def test_insider_cleanup_only_closes_child_pane(self):
        self.record()
        plan = self.cleanup()
        commands = [item["argv"] for item in plan["actions"] if "argv" in item]
        self.assertEqual(commands, [["herdr", "--session", "user-session", "pane", "close", "w1:p2"]])
        self.assertFalse(plan["executes_commands"])

    def test_insider_cannot_claim_parent_ownership(self):
        for changes in ({"owns_session": True}, {"owns_workspace": True, "workspace": "w1"}):
            with self.subTest(changes=changes), self.assertRaises(protocol.ProtocolError):
                self.record(**changes)

    def test_insider_new_workspace_cleanup_preserves_parent_and_session(self):
        self.record(pane="w2:p1", workspace="w2", owns_workspace=True,
                    parent_pane="w1:p1")
        actions = self.cleanup()["actions"]
        self.assertEqual(actions[0]["action"], "exit_agent")
        self.assertEqual([item["argv"] for item in actions if "argv" in item],
                         [["herdr", "--session", "user-session", "workspace", "close", "w2"]])

    def test_insider_new_tab_cleanup_preserves_parent_workspace(self):
        resources = self.record(pane="w1:p3", workspace="w1", tab="w1:t2", owns_tab=True,
                                parent_pane="w1:p1", parent_tab="w1:t1")
        commands = [item["argv"] for item in self.cleanup()["actions"] if "argv" in item]
        self.assertEqual(commands,
                         [["herdr", "--session", "user-session", "tab", "close", "w1:t2"]])
        protocol.publish(self.info["result_path"], self.response())
        new = self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        self.assertEqual(protocol.read_json(Path(new["request_path"]).parent / "resources.json"),
                         resources)

    def test_insider_container_ownership_requires_distinct_parent_identity(self):
        invalid = (
            {"workspace": "w1", "owns_workspace": True, "parent_pane": "w1:p1"},
            {"workspace": "w2", "pane": "w2:p1", "owns_workspace": True,
             "parent_pane": "current"},
            {"workspace": "w1", "tab": "w1:t1", "owns_tab": True},
            {"workspace": "w1", "tab": "w1:t1", "owns_tab": True,
             "parent_pane": "w1:p1", "parent_tab": "w1:t1"},
            {"workspace": "w1", "tab": "w1:t2", "owns_tab": True,
             "parent_pane": "w1:p1", "parent_tab": "w2:t1"},
            {"pane": "w1:p1", "parent_pane": "w1:p1"},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(protocol.ProtocolError):
                self.record(**changes)
            self.assertFalse((Path(self.info["request_path"]).parent / "resources.json").exists())

    def test_tab_ids_and_ownership_are_validated(self):
        invalid = (
            {"tab": "current"}, {"tab": "w2:t1"}, {"tab": "w1:t1\n"},
            {"owns_tab": True}, {"owns_tab": "yes", "tab": "w1:t1"},
            {"mode": "tmux", "pane": "%7", "tab": "w1:t1"},
            {"mode": "tmux", "pane": "%7", "owns_tab": True},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(protocol.ProtocolError):
                self.record(**changes)
            self.assertFalse((Path(self.info["request_path"]).parent / "resources.json").exists())

    def test_cli_records_insider_tab_and_parent_context(self):
        result = self.cli("record", "--request", self.info["request_path"], "--mode", "insider",
                          "--session", "user-session", "--agent", "child", "--pane", "w1:p3",
                          "--workspace", "w1", "--tab", "w1:t2", "--parent-pane", "w1:p1",
                          "--parent-tab", "w1:t1", "--owns-agent", "--owns-pane", "--owns-tab")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["owns_tab"])
        self.assertEqual(self.cleanup()["actions"][-1]["argv"][-3:], ["tab", "close", "w1:t2"])

    def test_legacy_pane_resources_remain_readable(self):
        resources = self.record()
        for key in ("tab", "owns_tab", "parent_pane", "parent_tab"):
            resources.pop(key)
        (Path(self.info["request_path"]).parent / "resources.json").write_text(json.dumps(resources))
        self.assertEqual(self.cleanup()["actions"][-1]["argv"][-3:], ["pane", "close", "w1:p2"])

    def test_cleanup_rejects_tampered_parent_container_claims(self):
        resources = self.record(pane="w1:p3", workspace="w1", tab="w1:t2", owns_tab=True,
                                parent_pane="w1:p1", parent_tab="w1:t1")
        path = Path(self.info["request_path"]).parent / "resources.json"
        for changes in ({"tab": "w1:t1"}, {"owns_workspace": True}):
            with self.subTest(changes=changes):
                path.write_text(json.dumps({**resources, **changes}))
                with self.assertRaises(protocol.ProtocolError):
                    self.cleanup()

    def test_cleanup_rejects_tampered_insider_ownership(self):
        resources = self.record()
        resources["owns_session"] = True
        (Path(self.info["request_path"]).parent / "resources.json").write_text(json.dumps(resources))
        with self.assertRaises(protocol.ProtocolError):
            self.cleanup()

    def test_shared_isolated_job_does_not_stop_session(self):
        self.record(mode="isolated", pane="w2:p1", workspace="w2", owns_workspace=True)
        commands = [item["argv"] for item in self.cleanup()["actions"] if "argv" in item]
        self.assertEqual(commands, [["herdr", "--session", "user-session", "workspace", "close", "w2"]])

    def test_isolated_owner_cleanup_order(self):
        self.record(mode="isolated", pane="w2:p1", workspace="w2", owns_workspace=True, owns_session=True)
        actions = self.cleanup()["actions"]
        self.assertEqual(actions[0]["action"], "exit_agent")
        self.assertEqual(actions[1]["argv"][-3:], ["workspace", "close", "w2"])
        self.assertEqual(actions[2]["argv"][-3:], ["session", "stop", "user-session"])
        self.assertEqual(actions[3]["argv"], ["herdr", "session", "delete", "user-session"])

    def test_tmux_cleanup_uses_exact_target(self):
        self.record(mode="tmux", pane="%7", owns_session=True, tmux_default_server=True)
        self.assertEqual(self.cleanup()["actions"][-1]["argv"],
                         ["tmux", "kill-session", "-t", "=user-session"])

    def test_prompt_keeps_unicode_compact_but_escapes_all_line_boundaries(self):
        task = "中文任务 café 😀\nnext\r\nline\u0085NEL\u2028LS\u2029PS\x7fDEL\u009bCSI"
        self.task.write_text(task, encoding="utf-8", newline="")
        info = self.prepare()
        prompt = Path(info["prompt_path"]).read_text()
        self.assertIn("中文任务 café 😀", prompt)
        self.assertEqual(len(prompt.splitlines()), 1)
        self.assertNotIn("\x7f", prompt)
        self.assertNotIn("\u009b", prompt)
        self.assertEqual(json.loads(prompt.split("REQUEST=", 1)[1])["task"], task)

    def test_tmux_socket_survives_followup_and_cleanup(self):
        socket = str(self.root / "private socket")
        self.record(mode="tmux", pane="%7", owns_session=True, tmux_socket=socket)
        protocol.publish(self.info["result_path"], self.response())
        followup = self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        plan = protocol.cleanup_plan(argparse.Namespace(request=followup["request_path"]))
        self.assertEqual(plan["actions"][-1]["argv"],
                         ["tmux", "-S", socket, "kill-session", "-t", "=user-session"])

    def test_tmux_record_requires_explicit_valid_server(self):
        for changes in ({}, {"tmux_socket": "relative"}, {"tmux_server": "-bad"},
                        {"tmux_socket": "/tmp/a", "tmux_default_server": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.record(mode="tmux", pane="%7", **changes)

    def test_legacy_tmux_cleanup_does_not_guess_default_server(self):
        resources = dict(job_id=self.request["job_id"], mode="tmux", pane="%7", session="old",
                         agent="child", workspace=None, owns_agent=True, owns_pane=True,
                         owns_workspace=False, owns_session=True)
        protocol.publish(Path(self.info["request_path"]).parent / "resources.json", resources)
        with self.assertRaises(ValueError):
            self.cleanup()

    def test_named_tmux_server_cli_and_cleanup(self):
        result = self.cli("record", "--request", self.info["request_path"], "--mode", "tmux",
                          "--session", "child", "--agent", "child", "--pane", "%7",
                          "--owns-pane", "--tmux-server", "private")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cleanup()["actions"][-1]["argv"],
                         ["tmux", "-L", "private", "kill-pane", "-t", "%7"])

    def test_borrowed_agent_is_not_exited_or_closed(self):
        self.record(owns_agent=False, owns_pane=False)
        self.assertEqual(self.cleanup()["actions"], [])

    def test_missing_resource_record_does_not_guess_cleanup(self):
        result = self.cli("cleanup-plan", "--request", self.info["request_path"])
        self.assertEqual(result.returncode, 2)

    def test_followup_inherits_resource_ownership(self):
        resources = self.record()
        protocol.publish(self.info["result_path"], self.response())
        new = self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        copied = protocol.read_json(Path(new["request_path"]).parent / "resources.json")
        self.assertEqual(copied, resources)

    def test_cleanup_still_available_after_project_directory_is_gone(self):
        self.record()
        self.cwd.rmdir()
        self.assertEqual(self.cleanup()["actions"][-1]["argv"][-3:], ["pane", "close", "w1:p2"])

    def test_cli_prepare_publish_verify_and_followup(self):
        first = self.cli("prepare", "--cwd", self.cwd, "--parent-depth", 0,
                         "--task-file", self.task, "--root", self.root)
        self.assertEqual(first.returncode, 0, first.stderr)
        info = json.loads(first.stdout)
        result = self.response(job_id=info["job_id"], round_id=info["round_id"])
        candidate = self.root / "candidate.json"
        candidate.write_text(json.dumps(result))
        published = self.cli("write-result", "--request", info["request_path"], "--input", candidate)
        self.assertEqual(published.returncode, 0, published.stderr)
        verified = self.cli("validate", "--request", info["request_path"], "--check-files")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        followup = self.cli("prepare", "--previous", info["request_path"],
                            "--task-file", self.task, "--root", self.root)
        self.assertEqual(followup.returncode, 0, followup.stderr)
        next_info = json.loads(followup.stdout)
        pending = self.cli("validate", "--request", next_info["request_path"])
        self.assertEqual(pending.returncode, 2, "old success must not finish the follow-up")

    def test_project_disappearance_does_not_prevent_error_reporting(self):
        self.cwd.rmdir()
        result = self.response(status="error", error="Project directory disappeared")
        candidate = self.root / "candidate.json"
        candidate.write_text(json.dumps(result))
        published = self.cli("write-result", "--request", self.info["request_path"], "--input", candidate)
        self.assertEqual(published.returncode, 0, published.stderr)
        readback = self.cli("validate", "--request", self.info["request_path"])
        self.assertEqual(readback.returncode, 0, readback.stderr)
        self.assertEqual(json.loads(readback.stdout)["status"], "error")
        checked = self.cli("validate", "--request", self.info["request_path"], "--check-files")
        self.assertEqual(checked.returncode, 3)
        followup = self.cli("prepare", "--previous", self.info["request_path"],
                            "--task-file", self.task, "--root", self.root)
        self.assertEqual(followup.returncode, 3, "new work still requires an existing cwd")

    def test_ambiguous_or_mismatched_resources_are_rejected_before_recording(self):
        invalid = ({"pane": "current"}, {"pane": "w1"}, {"pane": "w1:p2\n"},
                   {"mode": "tmux", "pane": "user-session"},
                   {"workspace": "w2", "owns_workspace": True, "mode": "isolated"},
                   {"agent": "-all"}, {"session": "bad\nsession"},
                   {"mode": "tmux", "pane": "%7", "session": "session:other"})
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(protocol.ProtocolError):
                self.record(**changes)
            self.assertFalse((Path(self.info["request_path"]).parent / "resources.json").exists())

    def test_wrong_job_resource_record_cannot_drive_cleanup_or_followup(self):
        resources = self.record()
        resources["job_id"] = "0" * 32
        (Path(self.info["request_path"]).parent / "resources.json").write_text(json.dumps(resources))
        protocol.publish(self.info["result_path"], self.response())
        with self.assertRaises(protocol.ProtocolError):
            self.cleanup()
        snapshot = set(self.root.iterdir())
        with self.assertRaises(protocol.ProtocolError):
            self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        self.assertEqual(set(self.root.iterdir()), snapshot)

    def test_corrupt_resource_record_does_not_leave_new_round(self):
        self.record()
        resources = Path(self.info["request_path"]).parent / "resources.json"
        resources.write_text("{broken")
        protocol.publish(self.info["result_path"], self.response())
        snapshot = set(self.root.iterdir())
        with self.assertRaises(ValueError):
            self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        self.assertEqual(set(self.root.iterdir()), snapshot)
        self.assertEqual(resources.read_text(), "{broken")

    def test_interrupted_preparation_rolls_back_only_new_directory(self):
        snapshot = set(self.root.iterdir())
        original_request = Path(self.info["request_path"]).read_bytes()
        for failure in (OSError("disk full"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(protocol, "contract", side_effect=failure):
                    with self.assertRaises(type(failure)):
                        self.prepare()
                self.assertEqual(set(self.root.iterdir()), snapshot)
                self.assertEqual(Path(self.info["request_path"]).read_bytes(), original_request)

    def test_overflow_numbers_and_invalid_unicode_cannot_start_followups(self):
        for suffix in (', "metric":1e999}', ', "metric":-1e999}', ', "detail":"\\ud800"}'):
            raw = json.dumps(self.response())[:-1] + suffix
            Path(self.info["result_path"]).write_text(raw)
            snapshot = set(self.root.iterdir())
            with self.subTest(suffix=suffix):
                checked = self.cli("validate", "--request", self.info["request_path"])
                self.assertEqual(checked.returncode, 3)
                with self.assertRaises(protocol.ProtocolError):
                    self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
                self.assertEqual(set(self.root.iterdir()), snapshot)

    def test_result_publication_io_failure_keeps_destination_absent(self):
        with patch.object(protocol.os, "link", side_effect=OSError("hard links unavailable")):
            with self.assertRaises(OSError):
                protocol.publish(self.info["result_path"], self.response())
        self.assertFalse(Path(self.info["result_path"]).exists())
        self.assertFalse(list(Path(self.info["request_path"]).parent.glob(".result.json.*")))


class InstallerTests(unittest.TestCase):
    def test_codex_and_all_targets_preserve_real_directories(self):
        with tempfile.TemporaryDirectory(prefix='orch-codex-install-') as temp:
            root = Path(temp)
            destinations = {name: root / (name + ' skills') for name in ('OMP', 'HERMES', 'OPENCODE', 'CODEX')}
            env = dict(os.environ, **{name+'_SKILLS_DIR':str(path) for name,path in destinations.items()})
            for _ in range(2):
                subprocess.run(['bash', str(REPO/'install.sh'), '--target', 'codex'], env=env, check=True, capture_output=True)
            self.assertFalse(any(path.exists() for name,path in destinations.items() if name!='CODEX'))
            self.assertEqual((destinations['CODEX']/'agent-codex').resolve(), REPO/'skills/agent-codex')
            preserved = destinations['CODEX']/'agent-controlled'
            preserved.unlink()
            preserved.mkdir()
            (preserved/'SKILL.md').write_text('local skill')
            subprocess.run(['bash', str(REPO/'install.sh'), '--target', 'all'], env=env, check=True, capture_output=True)
            self.assertEqual((preserved/'SKILL.md').read_text(), 'local skill')
            for path in destinations.values():
                self.assertEqual((path/'agent-codex').resolve(), REPO/'skills/agent-codex')

    def test_opencode_is_explicit_and_does_not_modify_other_hosts(self):
        with tempfile.TemporaryDirectory(prefix="orch-opencode-install-") as temp:
            root = Path(temp)
            omp, hermes, opencode = [root / name for name in ('omp', 'hermes', 'OpenCode skills')]
            env = dict(os.environ, OMP_SKILLS_DIR=str(omp), HERMES_SKILLS_DIR=str(hermes),
                       OPENCODE_SKILLS_DIR=str(opencode))
            subprocess.run(['bash', str(REPO/'install.sh'), '--target', 'opencode'],
                           env=env, check=True, capture_output=True)
            self.assertFalse(omp.exists())
            self.assertFalse(hermes.exists())
            for source in (REPO/'skills').iterdir():
                if source.is_dir():
                    self.assertEqual((opencode/source.name).resolve(), source)
            sentinel = opencode/'user-skill'
            sentinel.mkdir()
            (sentinel/'SKILL.md').write_text('user-owned')
            subprocess.run(['bash', str(REPO/'install.sh')], env=env, check=True, capture_output=True)
            self.assertEqual((sentinel/'SKILL.md').read_text(), 'user-owned')
            self.assertTrue(omp.exists() and hermes.exists())

    def test_invalid_install_target_leaves_all_destinations_absent(self):
        with tempfile.TemporaryDirectory(prefix="orch-invalid-install-") as temp:
            root = Path(temp)
            env = dict(os.environ, OMP_SKILLS_DIR=str(root/'omp'), HERMES_SKILLS_DIR=str(root/'hermes'),
                       OPENCODE_SKILLS_DIR=str(root/'opencode'))
            result = subprocess.run(['bash', str(REPO/'install.sh'), '--target', 'omo'],
                                    env=env, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(list(root.iterdir()), [])

    def test_install_repeat_preserve_directory_and_repair_broken_link(self):
        with tempfile.TemporaryDirectory(prefix="orch-install-test-") as temp:
            root = Path(temp)
            destinations = [root / "omp", root / "hermes"]
            env = dict(os.environ, OMP_SKILLS_DIR=str(destinations[0]), HERMES_SKILLS_DIR=str(destinations[1]),
                       OPENCODE_SKILLS_DIR=str(root/'opencode-not-selected'))
            names = sorted(path.name for path in (REPO / "skills").iterdir() if path.is_dir())
            for _ in range(2):
                subprocess.run(["bash", str(REPO / "install.sh")], env=env,
                               capture_output=True, check=True)
                self.assertFalse((root/'opencode-not-selected').exists())
                for dst in destinations:
                    for name in names:
                        self.assertTrue((dst / name).is_symlink())
                        self.assertEqual((dst / name).resolve(), REPO / "skills" / name)
            preserved = destinations[0] / names[0]
            preserved.unlink()
            preserved.mkdir()
            (preserved / "local.txt").write_text("keep me")
            broken = destinations[1] / names[0]
            broken.unlink()
            broken.symlink_to(root / "missing")
            subprocess.run(["bash", str(REPO / "install.sh")], env=env,
                           capture_output=True, check=True)
            self.assertEqual((preserved / "local.txt").read_text(), "keep me")
            self.assertEqual(broken.resolve(), REPO / "skills" / names[0])


if __name__ == "__main__":
    unittest.main()
