"""Opt-in real tmux transport checks with a deterministic peer, never a model agent."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from test_protocol import REPO, TOOL, protocol

sys.path.insert(0, str(TOOL.parent))
import watch
import jobs


@unittest.skipUnless(os.environ.get("ORCH_RUN_TMUX_TESTS") == "1", "set ORCH_RUN_TMUX_TESTS=1 to run tmux")
@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TmuxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orch-terminal-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.socket = self.root / "tmux.sock"
        self.addCleanup(self.close_sessions)
        self.cwd = self.root / "project with spaces"
        self.cwd.mkdir()
        self.peer = self.root / "fake agent.py"
        fixture = REPO / "tests/fixtures/terminal_peer.py"
        self.peer.write_text(f"#!{sys.executable}\n" + fixture.read_text())
        self.peer.chmod(0o700)
        self.ready = self.root / "ready"
        self.received = self.root / "received.jsonl"
        self.peer.with_suffix(".json").write_text(json.dumps({
            "tool": str(TOOL), "ready": str(self.ready), "received": str(self.received)}))
        self.tmux("new-session", "-d", "-s", "sentinel", "/usr/bin/env", sys.executable,
                  "-c", "import time; time.sleep(60)")
        # Two command arguments force direct exec; the path intentionally contains spaces.
        self.tmux("new-session", "-d", "-s", "child", "-c", str(self.cwd),
                  "-x", "200", "-y", "50", "/usr/bin/env", str(self.peer))
        self.wait_for(self.ready.exists, "fake target never became ready")
        self.pane = self.tmux("list-panes", "-t", "=child", "-F", "#{pane_id}").stdout.strip()

    def tmux(self, *args, check=True):
        result = subprocess.run(["tmux", "-S", str(self.socket), "-f", "/dev/null", *args],
                                capture_output=True, text=True, timeout=5)
        if check and result.returncode:
            self.fail(f"tmux {args[0]} failed: {result.stderr.strip()}")
        return result

    def close_sessions(self):
        for name in ("child", "sentinel"):
            self.tmux("kill-session", "-t", "=" + name, check=False)

    def wait_for(self, predicate, message):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(message)

    def prepare(self, task, previous=None):
        task_file = self.root / "task.txt"
        task_file.write_text(task, encoding="utf-8", newline="")
        info = protocol.prepare(argparse.Namespace(cwd=None if previous else str(self.cwd),
            parent_depth=None if previous else 0, max_depth=None, previous=previous,
            task_file=str(task_file), root=str(self.root)))
        if previous is None:
            protocol.record(argparse.Namespace(request=info["request_path"], mode="tmux",
                session="child", agent="peer", pane=self.pane, workspace=None,
                owns_agent=True, owns_pane=True, owns_workspace=False, owns_session=True,
                tmux_socket=str(self.socket)))
        return info

    def exchange(self, info):
        self.assertFalse(Path(info["result_path"]).exists())
        buffer = "orch-" + info["round_id"]
        self.tmux("load-buffer", "-b", buffer, info["prompt_path"])
        self.tmux("paste-buffer", "-d", "-b", buffer, "-t", self.pane)
        self.tmux("send-keys", "-t", self.pane, "Enter")
        self.wait_for(lambda: Path(info["result_path"]).exists(), "target did not publish a response")
        return protocol.validate_result(protocol.load_request(info["request_path"]),
                                        protocol.read_json(info["result_path"]), check_files=True)

    def test_long_multiline_task_arrives_literally_in_one_round(self):
        marker = self.root / "must-not-exist"
        task = f"中文任务\n`touch {marker}` $(touch {marker}) 'quoted' \r\n" + "测试内容" * 1000
        info = self.prepare(task)
        response = self.exchange(info)
        self.assertEqual(response["output"], task)
        self.assertEqual(len(self.received.read_text().splitlines()), 1)
        self.assertFalse(marker.exists())

    def test_publication_rewrite_survives_real_terminal_exit_and_watch_restart(self):
        info = self.prepare('Known original response')
        response = self.exchange(info)
        handle = watch.init_watch([info['request_path']], self.root)['watch_path']
        original = next(e for e in watch.poll_watch(handle)['events'] if e['kind'] == 'result')
        pinned = protocol.read_json(original['evidence_path'])['publication']
        response['output'] = 'Deliberate fixture rewrite'
        Path(info['result_path']).write_text(json.dumps(response))
        self.tmux('send-keys', '-t', self.pane, '-l', '--', '/exit')
        self.tmux('send-keys', '-t', self.pane, 'Enter')
        self.wait_for(lambda: self.tmux('has-session', '-t', '=child', check=False).returncode != 0,
                      'fixture terminal did not exit')
        replacement = watch.init_watch([info['request_path']], self.root)['watch_path']
        events = watch.poll_watch(replacement)['events']
        self.assertIn('observation_error', [e['kind'] for e in events])
        changed = next(e for e in events if e['kind'] == 'result_changed')
        self.assertEqual(protocol.read_json(changed['evidence_path'])['publication']['path'], pinned['path'])
        self.assertEqual(json.loads(protocol.read_json(pinned['path'])['result_text'])['output'],
                         'Known original response')
        self.assertEqual(len(self.received.read_text().splitlines()), 1)
        self.assertEqual(self.tmux('has-session', '-t', '=sentinel', check=False).returncode, 0)

    def test_submission_crash_after_delivery_recovers_without_duplicate_input(self):
        info = self.prepare("ASK: choose output format")
        index = self.root / "durable-index"
        state = jobs.register(index, info["request_path"])
        state = jobs.claim(index, state["job_id"], state["revision"], "controller-A", 1)
        stale_token = state["lease"]["token"]
        controller = self.root / "crashing_controller.py"
        controller.write_text('''import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import jobs
config = json.loads(Path(sys.argv[2]).read_text())
state = config["state"]
jobs.change(config["index"], state["job_id"], state["revision"], state["lease"]["token"], "begin", {})
base = ["tmux", "-S", config["socket"]]
subprocess.run(base + ["load-buffer", "-b", "crash-test", config["prompt"]], check=True)
subprocess.run(base + ["paste-buffer", "-d", "-b", "crash-test", "-t", config["pane"]], check=True)
subprocess.run(base + ["send-keys", "-t", config["pane"], "Enter"], check=True)
os._exit(17)  # no acceptance receipt or finally block
''')
        config = self.root / "controller.json"
        config.write_text(json.dumps({"state": state, "index": str(index), "socket": str(self.socket),
                                      "prompt": info["prompt_path"], "pane": self.pane}))
        crashed = subprocess.run([sys.executable, str(controller), str(TOOL.parent), str(config)],
                                 capture_output=True, text=True, timeout=5)
        self.assertEqual(crashed.returncode, 17, crashed.stderr)
        self.wait_for(lambda: Path(info["result_path"]).exists(), "delivered request never produced result")
        view = jobs.recover(index, state["job_id"])
        self.assertEqual(view["state"]["submission"], "uncertain")
        self.assertEqual(view["result"]["status"], "blocked")
        self.wait_for(lambda: time.time() >= state["lease"]["expires_at"], "lease failed to expire")
        state = jobs.claim(index, state["job_id"], view["state"]["revision"], "controller-B", 30)
        with self.assertRaises(ValueError):
            jobs.change(index, state["job_id"], state["revision"], stale_token, "begin", {})
        with self.assertRaises(ValueError):
            jobs.change(index, state["job_id"], state["revision"], state["lease"]["token"], "begin", {})
        state = jobs.change(index, state["job_id"], state["revision"], state["lease"]["token"], "receipt",
                            {"attempt_id": view["state"]["attempts"][-1]["attempt_id"], "status": "accepted",
                             "kind": "matching_result", "evidence_path": info["result_path"],
                             "note": "Recovered matching blocked response after controller crash."})
        follow = self.prepare("Use JSON output", previous=info["request_path"])
        state = jobs.change(index, state["job_id"], state["revision"], state["lease"]["token"], "activate",
                            {"request_path": follow["request_path"]})
        state = jobs.change(index, state["job_id"], state["revision"], state["lease"]["token"], "begin", {})
        self.assertEqual(self.exchange(follow)["status"], "success")
        state = jobs.change(index, state["job_id"], state["revision"], state["lease"]["token"], "receipt",
                            {"attempt_id": state["attempts"][-1]["attempt_id"], "status": "accepted",
                             "kind": "matching_result", "evidence_path": follow["result_path"],
                             "note": "Matched new round success."})
        deliveries = [json.loads(line)["round_id"] for line in self.received.read_text().splitlines()]
        self.assertEqual(deliveries, [info["round_id"], follow["round_id"]])
        self.assertEqual(len(state["rounds"]), 2)
        self.assertEqual(len(state["attempts"]), 2)

    def test_run_recovery_keeps_old_permission_while_sibling_completes_and_advances(self):
        import reviews
        import runs
        run = runs.init_run(self.root)
        sibling_peer = self.root / "sibling peer.py"
        sibling_peer.write_text(self.peer.read_text())
        sibling_peer.chmod(0o700)
        ready, received = self.root / "sibling-ready", self.root / "sibling-received.jsonl"
        sibling_peer.with_suffix(".json").write_text(json.dumps({"tool": str(TOOL), "ready": str(ready),
                                                               "received": str(received)}))
        self.tmux("new-session", "-d", "-s", "sibling", "-c", str(self.cwd),
                  "/usr/bin/env", str(sibling_peer))
        self.addCleanup(self.tmux, "kill-session", "-t", "=sibling", check=False)
        self.wait_for(ready.exists, "sibling did not become ready")
        sibling_pane = self.tmux("list-panes", "-t", "=sibling", "-F", "#{pane_id}").stdout.strip()

        def prepare_task(task, previous=None):
            file = self.root / "managed-task.txt"
            file.write_text(task)
            return protocol.prepare(argparse.Namespace(cwd=None if previous else str(self.cwd),
                parent_depth=None if previous else 0, max_depth=None, previous=previous,
                root=None, run=run["run_path"], task_file=str(file)))

        def send(info, pane):
            self.tmux("load-buffer", "-b", info["round_id"], info["prompt_path"])
            self.tmux("paste-buffer", "-d", "-b", info["round_id"], "-t", pane)
            self.tmux("send-keys", "-t", pane, "Enter")

        first = prepare_task("PAUSE: require a fixture permission")
        sibling = prepare_task("Complete independent sibling task")
        states = {}
        for info, pane, session in ((first, self.pane, "child"), (sibling, sibling_pane, "sibling")):
            protocol.record(argparse.Namespace(request=info["request_path"], mode="tmux", session=session,
                agent="peer", pane=pane, workspace=None, owns_agent=True, owns_pane=True,
                owns_workspace=False, owns_session=True, tmux_socket=str(self.socket)))
            state = jobs.register(run["index_path"], info["request_path"])
            state = jobs.claim(run["index_path"], state["job_id"], state["revision"], "controller", 30)
            states[info["job_id"]] = jobs.change(run["index_path"], state["job_id"], state["revision"],
                                                state["lease"]["token"], "begin", {})
            send(info, pane)
        self.wait_for(lambda: Path(sibling["result_path"]).exists(), "sibling did not finish independently")
        self.assertFalse(Path(first["result_path"]).exists())
        old = watch.init_watch([first["request_path"], sibling["request_path"]])["watch_path"]
        events = watch.poll_watch(old)["events"]
        attention = next(e for e in events if e["job_id"] == first["job_id"] and e["kind"] == "attention")
        reviews.record_review(old, attention["seq"], "waiting_user", "Asked once: fixture permission", 0)
        # The controller produces its own artifact while the first peer awaits input.
        (self.cwd / "controller-acceptance.txt").write_text("Check both round identities and delivery counts.")
        follow = prepare_task("Verify the sibling result", previous=sibling["request_path"])
        state = states[sibling["job_id"]]
        state = jobs.change(run["index_path"], state["job_id"], state["revision"], state["lease"]["token"],
                            "activate", {"request_path": follow["request_path"]})
        state = jobs.change(run["index_path"], state["job_id"], state["revision"], state["lease"]["token"], "begin", {})
        send(follow, sibling_pane)
        self.wait_for(lambda: Path(follow["result_path"]).exists(), "sibling follow-up did not finish")
        new = watch.init_watch([first["request_path"], follow["request_path"]])["watch_path"]
        next_events = watch.poll_watch(new)["events"]
        current = next(e for e in next_events if e["job_id"] == first["job_id"] and e["kind"] == "attention")
        self.assertEqual(attention["correlation_key"], current["correlation_key"])
        resumed = subprocess.run([sys.executable, str(TOOL.parent / "runs.py"), "recover", "--run", run["run_path"]],
                                 capture_output=True, text=True, timeout=5)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        snapshot = json.loads(resumed.stdout)
        waiting = next(e for e in snapshot["waiting_user"] if e["job_id"] == first["job_id"])
        opened = next(e for e in snapshot["action_required"] if e["kind"] == "attention")
        self.assertNotEqual(waiting["event_ref"], opened["event_ref"])
        self.assertEqual(waiting["note"], "Asked once: fixture permission")
        self.assertEqual(opened["related_reviews"][0]["note"], waiting["note"])
        self.assertEqual(opened["status"], "open")
        self.assertTrue(all(w["observer_active"] is False for w in snapshot["watches"]))
        self.tmux("send-keys", "-t", self.pane, "-l", "--", "ALLOW")
        self.tmux("send-keys", "-t", self.pane, "Enter")
        self.wait_for(lambda: Path(first["result_path"]).exists(), "fixture permission did not resume the same round")
        for handle, event, revision in ((old, attention, 1), (new, current, 0)):
            reviews.record_review(handle, event["seq"], "handled", "Verified fixture UI resolved and matching result", revision)
        self.assertEqual(runs.recover(run["run_path"])["counts"]["waiting_user"], 0)
        self.assertEqual(len(self.received.read_text().splitlines()), 1)
        self.assertEqual(len(received.read_text().splitlines()), 2)
        self.assertEqual(protocol.read_json(first["result_path"])["round_id"], first["round_id"])
        self.assertEqual(self.tmux("has-session", "-t", "=sentinel", check=False).returncode, 0)

    def test_repaint_correlation_and_audited_resolution_in_real_terminal(self):
        import reviews
        info = self.prepare("PAUSE: F04 supervision fixture")
        handle = watch.init_watch([info["request_path"]], self.root)["watch_path"]
        self.tmux("load-buffer", "-b", info["round_id"], info["prompt_path"])
        self.tmux("paste-buffer", "-d", "-b", info["round_id"], "-t", self.pane)
        self.tmux("send-keys", "-t", self.pane, "Enter")
        self.wait_for(lambda: "Allow once / Deny" in self.tmux("capture-pane", "-p", "-t", self.pane).stdout,
                      "fixture dialog missing")
        event = next(e for e in watch.poll_watch(handle, now=100)["events"] if e["kind"] == "attention")
        reviews.record_review(handle, event["seq"], "waiting_user", "Fixture decision recorded once", 0, now=101)
        all_events = []
        for tick in range(1, 41):
            self.tmux("send-keys", "-t", self.pane, "-l", "--", "REPAINT")
            self.tmux("send-keys", "-t", self.pane, "Enter")
            self.wait_for(lambda: f"Elapsed {tick}s" in self.tmux("capture-pane", "-p", "-t", self.pane).stdout,
                          "fixture failed to repaint")
            all_events.extend(watch.poll_watch(handle, now=100 + tick * 15)["events"])
        self.assertFalse(any(e["kind"] == "attention" for e in all_events))
        self.assertEqual(sum(e["kind"] == "review_due" for e in all_events), 4)
        pending = reviews.pending(handle, now=701)
        self.assertEqual(pending["waiting_user"][0]["note"], "Fixture decision recorded once")
        self.assertEqual(len([e for e in pending["action_required"] if e["kind"] == "review_due"]), 1)
        self.assertFalse(Path(info["result_path"]).exists())
        action = self.root / "fixture-input.json"
        self.tmux("send-keys", "-t", self.pane, "-l", "--", "ALLOW")
        self.tmux("send-keys", "-t", self.pane, "Enter")
        action.write_text(json.dumps({"input": "ALLOW", "scope": "deterministic test peer", "sent_at": time.time()}))
        self.wait_for(lambda: Path(info["result_path"]).exists(), "fixture did not resume")
        protocol.validate_result(protocol.load_request(info["request_path"]), protocol.read_json(info["result_path"]))
        self.assertNotIn("Allow once / Deny", self.tmux("capture-pane", "-p", "-t", self.pane).stdout)
        receipt = reviews.record_review(handle, event["seq"], "handled", "Fixture resolution verified", 1,
            action_evidence=str(action), resolution_evidence=info["result_path"], resolved_at=time.time())["review"]
        self.assertIsNotNone(receipt["resolved_at"])
        self.assertEqual(len(self.received.read_text().splitlines()), 1)
        for item in pending["action_required"]:
            reviews.record_review(handle, item["seq"], "handled", "Final live fixture state verified", item["revision"],
                                  resolution_evidence=info["result_path"], resolved_at=time.time())
        self.assertEqual(reviews.pending(handle)["counts"]["open"], 0)
        self.assertEqual(self.tmux("has-session", "-t", "=sentinel", check=False).returncode, 0)

    def test_prose_then_two_permissions_record_only_confirmed_occurrence(self):
        import reviews
        info = self.prepare('PERMISSIONS: review each fixture operation')
        handle = watch.init_watch([info['request_path']], self.root)['watch_path']
        self.tmux('load-buffer', '-b', info['round_id'], info['prompt_path'])
        self.tmux('paste-buffer', '-d', '-b', info['round_id'], '-t', self.pane)
        self.tmux('send-keys', '-t', self.pane, 'Enter')

        def screen():
            return self.tmux('capture-pane', '-p', '-t', self.pane).stdout

        def send(text):
            self.tmux('send-keys', '-t', self.pane, '-l', '--', text)
            self.tmux('send-keys', '-t', self.pane, 'Enter')

        self.wait_for(lambda: 'Fixture prose ready' in screen(), 'fixture prose missing')
        events = watch.poll_watch(handle)['events']
        self.assertFalse(any(e['kind'] == 'attention' for e in events))
        # No hook in this real tmux peer: keep unknown-state review explicit.
        for event in events:
            reviews.record_review(handle, event['seq'], 'handled', 'Read live deterministic peer prose', 0)
        send('NEXT_PERMISSION')
        self.wait_for(lambda: '$ fixture-operation a' in screen(), 'fixture a missing')
        first = next(e for e in watch.poll_watch(handle)['events'] if e['kind'] == 'attention')
        action, confirmation, note = (self.root / name for name in ('action.json', 'live.json', 'note.txt'))
        target = protocol.read_json(handle)['targets'][0]
        send('ALLOW')  # An input to our deterministic peer, never to a model host.
        action.write_text(json.dumps({'target': target, 'operation': 'a', 'input': 'ALLOW',
                                      'sent_at': time.time(), 'basis': 'deterministic fixture test'}))
        self.assertEqual(reviews.review_status(handle, first['seq'])['review']['status'], 'open')
        self.wait_for(lambda: '$ fixture-operation b' in screen(), 'fixture b missing')
        live = watch.observe(target)
        confirmed_at = time.time()
        self.assertIn('Fixture operation a completed', live['screen'])
        self.assertNotIn('$ fixture-operation a', live['screen'])
        confirmation.write_text(json.dumps({'target': target, 'observed_at': confirmed_at, **live}))
        note.write_text('Verified a completed; the current b prompt needs its own decision.')
        # Persist the confirmed outcome as the next bookkeeping operation, via a
        # separate controller process; do not scan/batch the next dialog first.
        reviewed = subprocess.run([sys.executable, str(TOOL.parent / 'watch.py'), 'review',
            '--watch', handle, '--seq', str(first['seq']), '--status', 'handled',
            '--expected-revision', '0', '--note-file', str(note), '--action-evidence', str(action),
            '--resolution-evidence', str(confirmation), '--resolved-at', str(confirmed_at)],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        self.assertEqual(json.loads(reviewed.stdout)['review']['resolved_at'], confirmed_at)
        second = next(e for e in watch.poll_watch(handle)['events'] if e['kind'] == 'attention')
        pending = reviews.pending(handle)['action_required']
        self.assertEqual([e['seq'] for e in pending], [second['seq']])
        self.assertEqual(pending[0]['revision'], 0)
        self.assertNotEqual(first['correlation_key'], second['correlation_key'])
        self.assertFalse(Path(info['result_path']).exists())
        send('ALLOW')
        self.wait_for(lambda: Path(info['result_path']).exists(), 'fixture did not finish')
        protocol.validate_result(protocol.load_request(info['request_path']), protocol.read_json(info['result_path']))
        self.assertEqual(len(self.received.read_text().splitlines()), 1)
        self.assertEqual(self.tmux('has-session', '-t', '=sentinel', check=False).returncode, 0)

    def test_observer_reads_private_socket_and_current_round_without_input(self):
        info = self.prepare("Observer transport check")
        handle = watch.init_watch([info["request_path"]], self.root,
                                  ["-S", str(self.socket)])["watch_path"]
        first = watch.poll_watch(handle)
        self.assertEqual(first["checked_targets"], 1)
        self.assertFalse(any(event["kind"] == "observation_error" for event in first["events"]))
        self.exchange(info)
        result = watch.poll_watch(handle)
        self.assertIn("result", [event["kind"] for event in result["events"]])
        self.assertEqual(len(self.received.read_text().splitlines()), 1)
        self.assertEqual(self.tmux("has-session", "-t", "=sentinel", check=False).returncode, 0)

    def test_review_receipts_survive_cli_restart_without_resubmitting_to_tmux(self):
        import reviews
        info = self.prepare("ASK: select output format")
        handle = watch.init_watch([info["request_path"]], self.root)["watch_path"]
        self.exchange(info)
        events = watch.poll_watch(handle)["events"]
        seq = next(event["seq"] for event in events if event["kind"] == "result")
        delivered_cursor = watch.read_events(handle)["cursor"]
        self.assertEqual(watch.read_events(handle, delivered_cursor)["events"], [])
        note = self.root / "review-note.txt"
        note.write_text("Asked for the output format; no answer received yet.")
        command = [sys.executable, str(TOOL.parent / "watch.py")]
        observer = subprocess.Popen(command + ["run", "--watch", handle, "--interval", "0.1",
                                               "--duration", "3"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True)
        try:
            self.wait_for(lambda: watch.observer_active(Path(handle).parent), "observer never acquired lock")
            result = subprocess.run(command + ["review", "--watch", handle, "--seq", str(seq),
                "--status", "waiting_user", "--expected-revision", "0", "--note-file", str(note)],
                capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            resumed = subprocess.run(command + ["pending", "--watch", handle],
                                     capture_output=True, text=True, timeout=3)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual([item["seq"] for item in json.loads(resumed.stdout)["waiting_user"]], [seq])
            # Receipt updates never answer the blocked round or send a second prompt.
            self.assertEqual(len(self.received.read_text().splitlines()), 1)
            self.assertEqual(protocol.read_json(info["result_path"])["status"], "blocked")
            self.assertEqual(reviews.review_status(handle, seq)["review"]["revision"], 1)
            self.assertEqual(self.tmux("has-session", "-t", "=sentinel", check=False).returncode, 0)
        finally:
            observer.terminate()
            observer.communicate(timeout=3)

    def test_blocked_followup_revision_and_owned_session_cleanup(self):
        old_results = []
        previous = None
        for task, status in (("ASK: select format", "blocked"), ("Use JSON", "success"),
                             ("Revise with an explanation", "success")):
            info = self.prepare(task, previous=previous)
            response = self.exchange(info)
            self.assertEqual(response["status"], status)
            for path, snapshot in old_results:
                self.assertEqual(path.read_bytes(), snapshot)
            old_results.append((Path(info["result_path"]), Path(info["result_path"]).read_bytes()))
            previous = info["request_path"]
        self.assertEqual(len(self.received.read_text().splitlines()), 3)
        plan = protocol.cleanup_plan(argparse.Namespace(request=previous))
        self.assertFalse(plan["executes_commands"])
        self.assertEqual(plan["actions"][-1]["argv"], ["tmux", "-S", str(self.socket), "kill-session", "-t", "=child"])
        self.tmux("send-keys", "-t", self.pane, "-l", "--", "/exit")
        self.tmux("send-keys", "-t", self.pane, "Enter")
        self.wait_for(lambda: self.tmux("has-session", "-t", "=child", check=False).returncode != 0,
                      "target/session did not exit")
        # Graceful exit already removed the owned session; no broader teardown is needed.
        self.assertEqual(self.tmux("has-session", "-t", "=sentinel", check=False).returncode, 0)

    def test_cleanup_plan_targets_recorded_socket_with_same_named_other_session(self):
        info = self.prepare("cleanup isolation")
        other = ["tmux", "-S", str(self.root / "other.sock"), "-f", "/dev/null"]
        subprocess.run(other + ["new-session", "-d", "-s", "child", "/usr/bin/env",
                               sys.executable, "-c", "import time; time.sleep(60)"], check=True)
        self.addCleanup(subprocess.run, other + ["kill-session", "-t", "=child"],
                        capture_output=True, timeout=5)
        plan = protocol.cleanup_plan(argparse.Namespace(request=info["request_path"]))
        subprocess.run(plan["actions"][-1]["argv"], check=True, timeout=5)
        self.assertNotEqual(self.tmux("has-session", "-t", "=child", check=False).returncode, 0)
        self.assertEqual(self.tmux("has-session", "-t", "=sentinel", check=False).returncode, 0)
        self.assertEqual(subprocess.run(other + ["has-session", "-t", "=child"],
                                        capture_output=True, timeout=5).returncode, 0)


if __name__ == "__main__":
    unittest.main()
