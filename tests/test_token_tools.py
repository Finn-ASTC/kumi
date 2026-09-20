"""Token-saving behavior without model calls or live user terminals."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_protocol as support

TOOL, protocol = support.TOOL, support.protocol

SCRIPTS = TOOL.parent
sys.path.insert(0, str(SCRIPTS))


class ToolCase(unittest.TestCase):
    setUp = support.ProtocolTests.setUp
    prepare = support.ProtocolTests.prepare
    response = support.ProtocolTests.response
    record = support.ProtocolTests.record
    cli = support.ProtocolTests.cli

    def run_tool(self, name, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / name), *map(str, args)],
                              capture_output=True, text=True)


class PacketTests(ToolCase):
    def test_packet_brief_cli_keeps_identity_without_echoing_task(self):
        packet = self.root / "packet.json"
        packet.write_text(json.dumps({"objective": "检查中文模块 $(false)",
            "acceptance": ["给出文件和行号"], "scope": "read-only",
            "inputs": ["src/a.py"], "known_facts": ["已有一个失败案例"],
            "constraints": ["不联网"]}))
        result = self.cli("prepare", "--cwd", self.cwd, "--parent-depth", 0,
                          "--task-packet", packet, "--root", self.root, "--brief")
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        request = protocol.load_request(info["request_path"])
        self.assertNotIn("task", info)
        self.assertIn("检查中文模块 $(false)", request["task"])
        self.assertIn("给出文件和行号", request["task"])
        self.assertEqual(info["round_id"], request["round_id"])
        prompt = Path(info["prompt_path"]).read_text()
        self.assertEqual(len(prompt.splitlines()), 1)
        self.assertEqual(json.loads(prompt.split("REQUEST=", 1)[1])["task"], request["task"])
        self.assertEqual(info["prompt_bytes"], len(prompt.encode()))

    def test_packet_rejects_missing_acceptance_and_unknown_history_field(self):
        packet = self.root / "packet.json"
        for value in ({"objective": "review", "scope": "read-only"},
                      {"objective": "review", "scope": "read-only", "acceptance": ["answer"],
                       "chat_history": "huge transcript"}):
            packet.write_text(json.dumps(value))
            before = set(self.root.iterdir())
            result = self.cli("prepare", "--cwd", self.cwd, "--parent-depth", 0,
                              "--task-packet", packet, "--root", self.root)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(set(self.root.iterdir()), before)


class EventWaitTests(ToolCase):
    def make_watch(self):
        import watch
        self.record()
        return watch.init_watch([self.info["request_path"]], self.root)["watch_path"]

    def test_queued_event_returns_immediately_without_observing(self):
        import watch
        handle = self.make_watch()
        with patch.object(watch, "observe", return_value={"state": "working", "screen": "Allow once / Deny"}):
            watch.poll_watch(handle)
        with patch.object(watch, "collect", side_effect=AssertionError("wait must not observe")), \
                patch.object(watch.time, "sleep", side_effect=AssertionError("queued events must return")):
            result = watch.wait_events(handle, after=0, timeout=20, limit=1)
        self.assertEqual(result["reason"], "events")
        self.assertTrue(result["more"])
        next_page = watch.wait_events(handle, after=result["cursor"], timeout=20)
        self.assertIn("attention", [event["kind"] for event in next_page["events"]])

    def test_absent_observer_returns_instead_of_silently_waiting(self):
        import watch
        handle = self.make_watch()
        result = watch.wait_events(handle, after=0, timeout=20)
        self.assertEqual(result["reason"], "observer_stopped")
        self.assertEqual(result["cursor"], 0)
        self.assertIsNone(result["last_checked_at"])

    def test_active_observer_timeout_is_bounded_and_does_not_take_its_lock(self):
        import watch
        handle = self.make_watch()
        with watch.locked(Path(handle).parent):
            result = self.run_tool("watch.py", "wait", "--watch", handle, "--after", 0, "--timeout", "0.03")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = json.loads(result.stdout)
        self.assertEqual(result["reason"], "timeout")
        self.assertTrue(result["observer_active"])
        self.assertEqual(result["events"], [])

    def test_wait_wakes_when_event_published_by_active_observer(self):
        from concurrent.futures import ThreadPoolExecutor
        import watch
        handle = self.make_watch()
        with watch.locked(Path(handle).parent), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(watch.wait_events, handle, 0, 1)
            with patch.object(watch, "observe", return_value={"state": "working", "screen": "Allow once / Deny"}):
                watch.sweep(Path(handle), 100)
            result = pending.result(timeout=2)
        self.assertEqual(result["reason"], "events")
        self.assertGreater(result["cursor"], 0)

    def test_invalid_wait_options_rejected(self):
        import watch
        handle = self.make_watch()
        for timeout in (-1, 61, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                watch.wait_events(handle, timeout=timeout)

    def test_observer_exit_during_wait_returns_promptly(self):
        import watch
        handle = self.make_watch()
        with patch.object(watch, "observer_active", side_effect=[True, False]):
            result = watch.wait_events(handle, timeout=20)
        self.assertEqual(result["reason"], "observer_stopped")


class UsageTests(ToolCase):
    def test_rejected_sample_does_not_reserve_attempt(self):
        import usage
        usage.record_usage(self.info["request_path"], self.sample())
        with self.assertRaises(ValueError):
            usage.record_usage(self.info["request_path"], self.sample(attempt_id="next", purpose="retry"))
        usage.record_usage(self.info["request_path"], self.sample(sample_id="call-2", attempt_id="next"))
        self.assertEqual(usage.summarize(self.info["request_path"])["samples"], 2)

    def test_cross_round_duplicate_rejected_before_publication_in_either_order(self):
        import usage
        protocol.publish(self.info["result_path"], self.response())
        followup = self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        usage.record_usage(followup["request_path"], self.sample())
        with self.assertRaises(ValueError):
            usage.record_usage(self.info["request_path"], self.sample())
        usage.record_usage(self.info["request_path"], self.sample(sample_id="call-2"))
        with self.assertRaises(ValueError):
            usage.record_usage(followup["request_path"], self.sample(sample_id="call-2"))
        self.assertEqual(usage.summarize(followup["request_path"])["samples"], 2)
        self.assertEqual(usage.summarize(self.info["request_path"])["samples"], 1)

    def test_failed_sample_publication_does_not_reserve_attempt(self):
        import usage
        publish = usage.protocol.publish
        def fail_sample(path, value):
            if "sample_id" in value:
                raise OSError("injected sample write failure")
            return publish(path, value)
        with patch.object(usage.protocol, "publish", side_effect=fail_sample), self.assertRaises(OSError):
            usage.record_usage(self.info["request_path"], self.sample(purpose="retry"))
        usage.record_usage(self.info["request_path"], self.sample())
        self.assertEqual(usage.summarize(self.info["request_path"])["attempts"]["task"], 1)

    def test_concurrent_conflicting_attempts_publish_only_one_purpose(self):
        from concurrent.futures import ThreadPoolExecutor
        import usage
        def record(index):
            try:
                usage.record_usage(self.info["request_path"], self.sample(
                    sample_id=f"call-{index}", purpose="task" if index % 2 else "retry"))
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(record, range(16)))
        summary = usage.summarize(self.info["request_path"])
        self.assertEqual(sum(accepted), 8)
        self.assertEqual(summary["samples"], 8)
        self.assertEqual(sum(summary["attempts"].values()), 1)

    def test_legacy_samples_remain_readable_and_deduplicated(self):
        import usage
        legacy = Path(self.info["request_path"]).parent / "usage"
        legacy.mkdir()
        entry = {**self.sample(), **{key: self.request[key] for key in protocol.IDENTITY_FIELDS}}
        protocol.publish(legacy / "legacy.json", entry)
        self.assertTrue(usage.record_usage(self.info["request_path"], self.sample())["duplicate"])
        usage.record_usage(self.info["request_path"], self.sample(sample_id="new-call"))
        self.assertEqual(usage.summarize(self.info["request_path"])["samples"], 2)

    def test_sibling_rounds_share_uniqueness_but_summary_uses_only_requested_chain(self):
        import usage
        protocol.publish(self.info["result_path"], self.response())
        a = self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        b = self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        usage.record_usage(a["request_path"], self.sample())
        with self.assertRaises(ValueError):
            usage.record_usage(b["request_path"], self.sample())
        usage.record_usage(b["request_path"], self.sample(sample_id="other-call"))
        self.assertEqual(usage.summarize(a["request_path"])["samples"], 1)
        self.assertEqual(usage.summarize(b["request_path"])["samples"], 1)

    def sample(self, **changes):
        value = dict(sample_id="call-1", source="native-cli", measurement="delta",
                     attempt_id="attempt-1", purpose="task", provider="example", model="model-a",
                     input_tokens=100, cached_input_tokens=60, output_tokens=20)
        value.update(changes)
        return value

    def test_usage_deduplication_conflicts_and_cached_subset(self):
        import usage
        usage.record_usage(self.info["request_path"], self.sample())
        duplicate = usage.record_usage(self.info["request_path"], self.sample())
        self.assertTrue(duplicate["duplicate"])
        summary = usage.summarize(self.info["request_path"])
        self.assertEqual(summary["totals"], dict(input_tokens=100, cached_input_tokens=60, output_tokens=20))
        self.assertEqual(summary["attempts"], dict(task=1, retry=0, report_repair=0))
        for change in ({"output_tokens": 21}, {"sample_id": "bad", "cached_input_tokens": 101},
                       {"sample_id": "bad", "input_tokens": True},
                       {"sample_id": "bad", "measurement": "cumulative"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                usage.record_usage(self.info["request_path"], self.sample(**change))

    def test_usage_followups_unknown_counters_and_attempts(self):
        import usage
        usage.record_usage(self.info["request_path"], self.sample())
        protocol.publish(self.info["result_path"], self.response())
        followup = self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        for index in range(2):
            usage.record_usage(followup["request_path"], self.sample(sample_id=f"call-{index+2}",
                attempt_id="repair-1", purpose="report_repair", input_tokens=None,
                cached_input_tokens=None, output_tokens=10))
        summary = usage.summarize(followup["request_path"])
        self.assertIsNone(summary["totals"]["input_tokens"])
        self.assertEqual(summary["known_subtotals"]["input_tokens"], 100)
        self.assertEqual(summary["totals"]["output_tokens"], 40)
        self.assertEqual(summary["attempts"]["report_repair"], 1)
        self.assertEqual(summary["rounds"], 2)

    def test_missing_round_usage_stays_unknown_and_cross_job_chain_rejected(self):
        import usage
        summary = usage.summarize(self.info["request_path"])
        self.assertIsNone(summary["totals"]["output_tokens"])
        self.assertEqual(summary["rounds_without_usage"], [self.info["round_id"]])
        other = self.prepare()
        request = protocol.read_json(self.info["request_path"])
        request["previous_request"] = other["request_path"]
        Path(self.info["request_path"]).write_text(json.dumps(request))
        with self.assertRaises(ValueError):
            usage.summarize(self.info["request_path"])

    def test_usage_cli_uses_normalized_native_counts(self):
        candidate = self.root / "usage.json"
        candidate.write_text(json.dumps(self.sample()))
        result = self.run_tool("usage.py", "record", "--request", self.info["request_path"],
                               "--input", candidate)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = self.run_tool("usage.py", "summary", "--request", self.info["request_path"])
        self.assertEqual(json.loads(summary.stdout)["totals"]["input_tokens"], 100)

    def test_retry_attempt_is_counted_once_across_multiple_model_calls(self):
        import usage
        for index in range(3):
            usage.record_usage(self.info["request_path"], self.sample(sample_id=f"retry-call-{index}",
                attempt_id="retry-1", purpose="retry"))
        summary = usage.summarize(self.info["request_path"])
        self.assertEqual(summary["attempts"]["retry"], 1)
        self.assertEqual(summary["totals"]["input_tokens"], 300)

    def test_conflicting_attempt_purpose_does_not_poison_existing_ledger(self):
        import usage
        usage.record_usage(self.info["request_path"], self.sample())
        with self.assertRaises(ValueError):
            usage.record_usage(self.info["request_path"], self.sample(sample_id="second-call", purpose="retry"))
        self.assertEqual(usage.summarize(self.info["request_path"])["samples"], 1)


class WatchTests(ToolCase):
    def test_restart_after_evidence_or_event_publication_keeps_progress(self):
        import watch
        for stage in ("events", "state"):
            with self.subTest(stage=stage):
                if not (Path(self.info["request_path"]).parent / "resources.json").exists():
                    self.record()
                handle = watch.init_watch([self.info["request_path"]], self.root)["watch_path"]
                publish = watch.protocol.publish
                def fail_event(path, value):
                    if Path(path).parent.name == "events":
                        raise OSError("injected after evidence")
                    return publish(path, value)
                with patch.object(watch.protocol, "publish", side_effect=fail_event) if stage == "events" else patch.object(watch, "atomic_save", side_effect=OSError("injected after events")):
                    with self.assertRaises(OSError):
                        self.poll(handle)
                before = {str(p): p.read_bytes() for d in ("events", "evidence")
                          for p in (Path(handle).parent / d).glob("*.json")}
                recovered = self.poll(handle, now=15)
                self.assertGreater(recovered["cursor"], 1)
                self.assertTrue(all(Path(p).read_bytes() == data for p, data in before.items()))
                self.assertTrue(all(Path(e["evidence_path"]).exists()
                                    for e in watch.read_events(handle)["events"]))
                self.assertEqual(self.poll(handle, now=30)["events"], [])

    def test_historical_dialog_with_changing_progress_has_bounded_attention(self):
        handle = self.make_watch()
        events = []
        for now in range(0, 600, 15):
            events.extend(self.poll(handle, now=now, screen=(
                f"Previous dialog: Allow once / Deny\nNow running normally: file {now}"))["events"])
        self.assertLessEqual(sum(e["kind"] == "attention" for e in events), 1)
        self.assertGreater(sum(e["kind"] == "review_due" for e in events), 0)

    def test_dialog_progress_outside_command_does_not_repeat_but_new_command_does(self):
        handle = self.make_watch()
        self.poll(handle, state="blocked", screen="Command: read a.py\nAllow once / Deny\nElapsed 1s")
        same = self.poll(handle, now=15, state="blocked", screen="Command: read a.py\nAllow once / Deny\nElapsed 2s")
        self.assertNotIn("attention", [e["kind"] for e in same["events"]])
        changed = self.poll(handle, now=16, state="blocked", screen="Command: write a.py\nAllow once / Deny\nElapsed 3s")
        self.assertIn("attention", [e["kind"] for e in changed["events"]])

    def test_codex_native_run_prompt_is_visible_without_blocked_hook(self):
        handle = self.make_watch()
        screen = "Would you like to run the following command?\n\n$ read a.py\n\n1. Yes, proceed (y)\n2. No (esc)"
        first = self.poll(handle, screen=screen)
        self.assertIn("attention", [e["kind"] for e in first["events"]])
        second = self.poll(handle, now=15, screen=screen.replace("read a.py", "write b.py"))
        self.assertIn("attention", [e["kind"] for e in second["events"]])

    def make_watch(self):
        import watch
        self.record()
        return watch.init_watch([self.info["request_path"]], self.root)["watch_path"]

    def poll(self, handle, state="working", screen="running", now=0):
        import watch
        with patch.object(watch, "observe", return_value={"state": state, "screen": screen}):
            return watch.poll_watch(handle, now=now)

    def test_unchanged_work_is_quiet_and_changed_ui_gets_periodic_review(self):
        handle = self.make_watch()
        self.poll(handle)
        self.assertEqual(self.poll(handle, now=15)["events"], [])
        self.assertEqual(self.poll(handle, screen="processing file 2", now=16)["events"], [])
        result = self.poll(handle, screen="processing file 3", now=31)
        self.assertIn("review_due", [event["kind"] for event in result["events"]])

    def test_approval_without_blocked_hook_is_reported_and_deduplicated(self):
        handle = self.make_watch()
        self.poll(handle)
        screen = "Command: read src/a.py\nAllow once / Deny"
        result = self.poll(handle, screen=screen, now=1)
        self.assertIn("attention", [event["kind"] for event in result["events"]])
        self.assertEqual(self.poll(handle, screen=screen, now=16)["events"], [])
        self.poll(handle, screen="working again", now=20)
        result = self.poll(handle, screen=screen, now=21)
        self.assertIn("attention", [event["kind"] for event in result["events"]])

    def test_ten_minutes_of_identical_screens_do_not_replay_every_poll(self):
        handle = self.make_watch()
        events = []
        for now in range(0, 600, 15):
            events.extend(self.poll(handle, now=now)["events"])
        self.assertEqual([event["kind"] for event in events], ["state_changed", "stalled"])

    def test_new_command_in_same_approval_dialog_is_not_suppressed(self):
        handle = self.make_watch()
        self.poll(handle, state="blocked", screen="Command: read a.py\nAllow once / Deny")
        result = self.poll(handle, state="blocked", screen="Command: write a.py\nAllow once / Deny", now=15)
        self.assertIn("attention", [event["kind"] for event in result["events"]])

    def test_result_identity_checked_and_cursor_survives_restart(self):
        import watch
        handle = self.make_watch()
        self.poll(handle)
        protocol.publish(self.info["result_path"], self.response(round_id="f"*32))
        result = self.poll(handle, now=1)
        self.assertIn("invalid_result", [event["kind"] for event in result["events"]])
        cursor = result["cursor"]
        self.assertEqual(watch.read_events(handle, cursor)["events"], [])
        self.assertEqual(self.poll(handle, now=15)["events"], [])

    def test_result_does_not_stop_post_publication_ui_observation(self):
        handle = self.make_watch()
        protocol.publish(self.info["result_path"], self.response())
        first = self.poll(handle)
        self.assertIn("result", [event["kind"] for event in first["events"]])
        later = self.poll(handle, screen="Permission required: command", now=1)
        self.assertIn("attention", [event["kind"] for event in later["events"]])

    def test_missing_resources_and_tmux_selector_are_not_guessed(self):
        import watch
        with self.assertRaises(FileNotFoundError):
            watch.init_watch([self.info["request_path"]], self.root)
        self.record(mode="tmux", pane="%7", owns_session=True, tmux_socket=str(self.root / "socket"))
        resources_path = Path(self.info["request_path"]).parent / "resources.json"
        resources = protocol.read_json(resources_path)
        del resources["tmux_selector"]
        resources_path.write_text(json.dumps(resources))
        with self.assertRaises(ValueError):
            watch.init_watch([self.info["request_path"]], self.root)

    def test_transport_errors_do_not_become_task_completion(self):
        import watch
        handle = self.make_watch()
        with patch.object(watch, "observe", side_effect=OSError("socket unavailable")):
            result = watch.poll_watch(handle, now=0)
        self.assertEqual([event["kind"] for event in result["events"]], ["observation_error"])

    def test_all_targets_observed_and_results_not_confused_between_rounds(self):
        import watch
        self.record()
        other = self.prepare()
        protocol.record(argparse.Namespace(request=other["request_path"], mode="insider",
            session="user-session", agent="other", pane="w2:p1", workspace="w2",
            parent_pane="w1:p1", owns_agent=True, owns_pane=True, owns_workspace=True, owns_session=False))
        handle = watch.init_watch([self.info["request_path"], other["request_path"]], self.root)["watch_path"]
        def observation(target):
            return {"state": "working", "screen": "Allow once / Deny"
                    if target["round_id"] == other["round_id"] else "normal output"}
        with patch.object(watch, "observe", side_effect=observation):
            result = watch.poll_watch(handle, now=0)
        alerts = [event for event in result["events"] if event["kind"] == "attention"]
        self.assertEqual(result["checked_targets"], 2)
        self.assertEqual([event["round_id"] for event in alerts], [other["round_id"]])

    def test_deadline_and_stall_emit_once_without_cancelling(self):
        import watch
        self.record()
        handle = watch.init_watch([self.info["request_path"]], self.root, deadline=50)["watch_path"]
        self.poll(handle)
        self.assertIn("deadline", [e["kind"] for e in self.poll(handle, now=60)["events"]])
        self.assertIn("stalled", [e["kind"] for e in self.poll(handle, now=130)["events"]])
        self.assertEqual(self.poll(handle, now=150)["events"], [])

    def test_two_observers_cannot_write_the_same_watch(self):
        import watch
        handle = self.make_watch()
        with watch.locked(Path(handle).parent), self.assertRaises(ValueError):
            self.poll(handle)

    def test_herdr_cli_dedup_evidence_and_bounded_run(self):
        import watch
        handle = self.make_watch()
        binary = self.root / "herdr"
        fixture = support.REPO / "tests/fixtures/observer_peer.py"
        binary.write_text(f"#!{sys.executable}\n" + fixture.read_text())
        binary.chmod(0o700)
        source = self.root / "peer.json"
        source.write_text(json.dumps({"state": "working", "screen": "Allow once / Deny"}))
        environment = {"PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                       "ORCH_OBSERVER_FIXTURE": str(source)}
        with patch.dict(os.environ, environment):
            first = self.run_tool("watch.py", "poll", "--watch", handle)
            self.assertEqual(first.returncode, 0, first.stderr)
            events = json.loads(first.stdout)["events"]
            self.assertIn("attention", [event["kind"] for event in events])
            self.assertTrue(all(Path(event["evidence_path"]).exists() for event in events))
            second = self.run_tool("watch.py", "poll", "--watch", handle)
            self.assertEqual(json.loads(second.stdout)["events"], [])
            run = self.run_tool("watch.py", "run", "--watch", handle,
                                "--duration", "0.08", "--interval", "0.02")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertFalse(json.loads(run.stdout)["targets_cancelled"])
        self.assertGreater(len(source.with_suffix(".calls").read_text().splitlines()), 4)

    def test_tmux_observation_preserves_explicit_socket_and_exact_identity(self):
        import watch
        self.record(mode="tmux", pane="%7", owns_session=True, tmux_socket=str(self.root / "socket"))
        handle = watch.init_watch([self.info["request_path"]], self.root)["watch_path"]
        with self.assertRaises(ValueError):
            watch.init_watch([self.info["request_path"]], self.root, ["-L", "wrong"])
        target = protocol.read_json(handle)["targets"][0]
        calls = []
        def read(argv):
            calls.append(argv)
            return "user-session\t%7\t0\n" if "display-message" in argv else "working"
        with patch.object(watch, "run_read", side_effect=read):
            self.assertEqual(watch.observe(target)["state"], "unknown")
        self.assertEqual(calls[0][:3], ["tmux", "-S", str(self.root / "socket")])
        self.assertEqual(calls[1][-4:], ["-t", "%7", "-S", "0"])
        with patch.object(watch, "run_read", return_value="wrong-session\t%7\t0\n"), self.assertRaises(ValueError):
            watch.observe(target)
