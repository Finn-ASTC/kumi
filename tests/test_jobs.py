"""Submission recovery must fail closed at crash and competing-controller boundaries."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from test_token_tools import ToolCase
import protocol
import jobs


class JobTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.index = self.root / "persistent jobs"
        self.job = self.info["job_id"]
        self.record()
        self.evidence = self.root / "transport.txt"
        self.evidence.write_text("observed transport evidence", encoding="utf-8")
        self.state = jobs.register(self.index, self.info["request_path"], now=100)

    def claim(self, owner="controller-A", now=100):
        self.state = jobs.claim(self.index, self.job, self.state["revision"], owner, 10, now=now)
        self.token = self.state["lease"]["token"]
        return self.state

    def change(self, action, payload=None, now=101):
        self.state = jobs.change(self.index, self.job, self.state["revision"], self.token,
                                 action, payload or {}, now=now)
        return self.state

    def receipt(self, status="accepted", kind="target_active", now=102):
        return self.change("receipt", {"attempt_id": self.state["attempts"][-1]["attempt_id"],
                                       "status": status, "kind": kind,
                                       "evidence_path": str(self.evidence), "note": "checked target"}, now)

    def test_prepared_is_discoverable_but_not_submitted(self):
        found = jobs.inventory(self.index, now=100)
        self.assertEqual(found["jobs"][0]["job_id"], self.job)
        view = jobs.recover(self.index, self.job, now=100)
        self.assertEqual(view["state"]["submission"], "prepared")
        self.assertEqual(view["next_action"], "claim_and_check_target")
        self.assertIsNone(view["state"]["launch"])
        self.assertIsNone(view["state"]["native"]["session_id"])
        self.assertEqual(view["result"]["status"], "missing")

    def test_crash_after_begin_remains_uncertain_after_takeover(self):
        self.claim()
        self.change("begin")
        old_token = self.token
        self.assertEqual(self.state["submission"], "uncertain")
        self.assertEqual(jobs.recover(self.index, self.job, now=112)["next_action"],
                         "reconcile_without_resending")
        self.claim("controller-B", now=112)
        with self.assertRaisesRegex(protocol.ProtocolError, "lease"):
            jobs.change(self.index, self.job, self.state["revision"], old_token, "begin", {}, now=113)
        with self.assertRaisesRegex(protocol.ProtocolError, "uncertain"):
            self.change("begin", now=113)
        self.receipt(now=113)
        self.assertEqual(self.state["submission"], "accepted")

    def test_two_controllers_cannot_claim_same_revision(self):
        revision = self.state["revision"]
        def acquire(owner):
            try:
                return jobs.claim(self.index, self.job, revision, owner, 10, now=100)
            except protocol.ProtocolError:
                return None
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(acquire, ["A", "B"]))
        self.assertEqual(sum(value is not None for value in results), 1)
        latest = jobs.recover(self.index, self.job, now=101)["state"]
        with self.assertRaisesRegex(protocol.ProtocolError, "owned"):
            jobs.claim(self.index, self.job, latest["revision"], "C", 10, now=101)

    def test_timeout_never_enables_retry_but_proven_not_sent_does(self):
        self.claim()
        self.change("begin")
        self.receipt("uncertain", "timeout")
        with self.assertRaises(protocol.ProtocolError):
            self.change("begin")
        self.receipt("rejected", "not_sent")
        first = self.state["attempts"][0]["attempt_id"]
        self.change("begin", now=103)
        self.assertNotEqual(first, self.state["attempts"][-1]["attempt_id"])
        self.assertEqual(len(self.state["attempts"]), 2)

    def test_fast_result_is_visible_without_receipt_and_never_resubmitted(self):
        self.claim()
        self.change("begin")
        protocol.publish(self.info["result_path"], self.response())
        view = jobs.recover(self.index, self.job, now=102)
        self.assertEqual(view["result"]["status"], "success")
        self.assertEqual(view["state"]["submission"], "uncertain")
        self.assertEqual(view["next_action"], "verify_response")
        self.receipt(kind="matching_result")
        with self.assertRaises(protocol.ProtocolError):
            self.change("begin")
        with self.assertRaises(protocol.ProtocolError):
            self.receipt("rejected", "not_sent")

    def test_followup_activation_has_one_successor_and_retains_history(self):
        self.claim()
        self.change("begin")
        protocol.publish(self.info["result_path"], self.response(status="blocked", blocked_reason="Which suffix?"))
        next_a = self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        next_b = self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        old_revision = self.state["revision"]
        self.change("activate", {"request_path": next_a["request_path"]})
        self.assertEqual(self.state["active_request"], next_a["request_path"])
        self.assertEqual(self.state["submission"], "prepared")
        self.assertEqual(len(self.state["rounds"]), 2)
        self.assertEqual(self.state["attempts"][0]["round_id"], self.info["round_id"])
        with self.assertRaisesRegex(protocol.ProtocolError, "revision"):
            jobs.change(self.index, self.job, old_revision, self.token, "activate",
                        {"request_path": next_b["request_path"]}, now=102)
        with self.assertRaisesRegex(protocol.ProtocolError, "previous"):
            self.change("activate", {"request_path": next_b["request_path"]})

    def test_missing_invalid_or_replaced_inputs_fail_closed(self):
        self.claim()
        self.change("begin")
        Path(self.info["result_path"]).write_text('{"wrong": true}')
        self.assertEqual(jobs.recover(self.index, self.job)["result"]["status"], "invalid")
        with self.assertRaises(protocol.ProtocolError):
            self.receipt(kind="matching_result")
        request_path = Path(self.info["request_path"])
        request = protocol.read_json(request_path)
        request["task"] = "replaced task with same IDs"
        request_path.write_text(json.dumps(request))
        with self.assertRaisesRegex(protocol.ProtocolError, "changed"):
            jobs.recover(self.index, self.job)
        self.assertEqual(jobs.inventory(self.index)["jobs"][0]["state"], "unreadable")

    def test_launch_native_and_monitor_metadata_are_explicit_and_checked(self):
        import watch
        handle = watch.init_watch([self.info["request_path"]], self.root)["watch_path"]
        self.claim()
        self.change("update", {"launch": {"argv": ["omp", "--model", "configured model"],
                                           "profile": "existing-config"},
                               "native": {"session_id": "native-1", "turn_id": None},
                               "monitor": {"owner": "controller-A", "watch_path": handle,
                                           "expires_at": 109}, "deadline": 500})
        view = jobs.recover(self.index, self.job, now=110)
        self.assertTrue(view["monitor_expired"])
        self.assertTrue(view["lease_expired"])
        self.assertEqual(view["state"]["launch"]["argv"][0], "omp")
        with self.assertRaises(protocol.ProtocolError):
            self.change("update", {"native": {"session_id": 12, "turn_id": None}})
        with self.assertRaises(protocol.ProtocolError):
            self.change("update", {"submission": "accepted"})

    def test_index_binding_prevents_parallel_registration_elsewhere(self):
        same = jobs.register(self.index, self.info["request_path"], now=101)
        self.assertEqual(same["revision"], self.state["revision"])
        with self.assertRaisesRegex(protocol.ProtocolError, "index"):
            jobs.register(self.root / "another-index", self.info["request_path"])

    def test_duplicate_target_in_same_index_is_not_submitted(self):
        self.claim()
        self.change("begin")
        sibling = self.prepare()
        resources = protocol.read_json(Path(self.info["request_path"]).parent / "resources.json")
        resources["job_id"] = sibling["job_id"]
        protocol.publish(Path(sibling["request_path"]).parent / "resources.json", resources)
        other = jobs.register(self.index, sibling["request_path"], now=100)
        other = jobs.claim(self.index, other["job_id"], other["revision"], "sibling", 10, now=100)
        with self.assertRaisesRegex(protocol.ProtocolError, "target"):
            jobs.change(self.index, other["job_id"], other["revision"], other["lease"]["token"],
                        "begin", {}, now=101)

    def test_stale_version_expired_lease_and_invalid_evidence_cannot_write(self):
        self.claim()
        revision = self.state["revision"]
        self.change("begin")
        with self.assertRaisesRegex(protocol.ProtocolError, "revision"):
            jobs.change(self.index, self.job, revision, self.token, "begin", {}, now=102)
        with self.assertRaisesRegex(protocol.ProtocolError, "lease"):
            self.change("renew", {"lease_seconds": 10}, now=111)
        with self.assertRaises(protocol.ProtocolError):
            self.change("receipt", {"attempt_id": "wrong", "status": "accepted", "kind": "target_active",
                                    "evidence_path": str(self.evidence), "note": "observed"})
        self.evidence.unlink()
        with self.assertRaises(FileNotFoundError):
            self.receipt()

    def test_cli_recovers_across_processes_and_reports_missing_index(self):
        tool = Path(jobs.__file__)
        result = subprocess.run([sys.executable, str(tool), "recover", "--index", str(self.index),
                                 "--job", self.job], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"]["active_request"], self.info["request_path"])
        missing = subprocess.run([sys.executable, str(tool), "list", "--index", str(self.root / "absent")],
                                 capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)
        self.assertFalse((self.root / "absent").exists())

    def test_committed_begin_with_lost_reply_is_recoverable_without_replay(self):
        self.claim()
        # Simulate failure AFTER atomic publication, before the CLI can return.
        with patch.object(jobs, "sync_directory", side_effect=OSError("lost commit acknowledgement")):
            with self.assertRaises(OSError):
                self.change("begin")
        view = jobs.recover(self.index, self.job, now=102)
        self.assertEqual(view["state"]["submission"], "uncertain")
        self.assertEqual(len(view["state"]["attempts"]), 1)
        self.assertNotIn("token", view["state"]["lease"])
        with self.assertRaisesRegex(protocol.ProtocolError, "revision"):
            self.change("begin")

    def test_register_crash_after_binding_recovers_only_in_same_index(self):
        initial = self.prepare()
        with patch.object(jobs, "save", side_effect=OSError("crashed before first revision")):
            with self.assertRaises(OSError):
                jobs.register(self.index, initial["request_path"])
        with self.assertRaisesRegex(protocol.ProtocolError, "index"):
            jobs.register(self.root / "wrong-index", initial["request_path"])
        state = jobs.register(self.index, initial["request_path"])
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["submission"], "prepared")

    def test_renew_release_and_close_do_not_resolve_uncertainty(self):
        self.claim()
        self.change("begin")
        self.change("renew", {"lease_seconds": 20}, now=105)
        self.assertEqual(self.state["lease"]["expires_at"], 125)
        self.change("release", now=106)
        self.claim("controller-B", now=107)
        self.assertEqual(self.state["submission"], "uncertain")
        close = {"outcome": "completed", "evidence_path": str(self.evidence), "note": "independent check"}
        with self.assertRaisesRegex(protocol.ProtocolError, "success"):
            self.change("close", close, now=108)
        protocol.publish(self.info["result_path"], self.response())
        with self.assertRaisesRegex(protocol.ProtocolError, "completion"):
            self.change("close", close, now=108)
        from control_fixtures import stopped
        self.state = stopped(self.index, self.state, self.token, self.evidence, 108)
        self.change("close", {**close, "outcome": "cancelled"}, now=108)
        self.assertEqual(jobs.recover(self.index, self.job)["next_action"], "closed")
        with self.assertRaisesRegex(protocol.ProtocolError, "closed"):
            self.claim("C", now=120)

    def test_resource_replacement_and_revision_gap_are_visible(self):
        self.claim()
        resource_path = Path(self.info["request_path"]).parent / "resources.json"
        resources = protocol.read_json(resource_path)
        resources["agent"] = "different-child"
        resource_path.write_text(json.dumps(resources))
        with self.assertRaisesRegex(protocol.ProtocolError, "changed"):
            self.change("begin")
        (self.index / self.job / "000000000001.json").unlink()
        with self.assertRaisesRegex(protocol.ProtocolError, "revision"):
            jobs.recover(self.index, self.job)

    def test_result_before_registration_and_foreign_watch_are_rejected(self):
        import watch
        initial = self.prepare()
        response = self.response(job_id=initial["job_id"], round_id=initial["round_id"])
        protocol.publish(initial["result_path"], response)
        with self.assertRaisesRegex(protocol.ProtocolError, "before submission"):
            jobs.register(self.index, initial["request_path"])
        copied = protocol.read_json(Path(self.info["request_path"]).parent / "resources.json")
        copied["job_id"] = initial["job_id"]
        protocol.publish(Path(initial["request_path"]).parent / "resources.json", copied)
        handle = watch.init_watch([initial["request_path"]], self.root)["watch_path"]
        self.claim()
        with self.assertRaisesRegex(protocol.ProtocolError, "watch"):
            self.change("update", {"monitor": {"owner": "A", "watch_path": handle, "expires_at": 110}})

    def test_invalid_times_and_lease_durations_are_rejected(self):
        for value in (True, float("nan"), float("inf"), -1):
            with self.subTest(value=value), self.assertRaises(protocol.ProtocolError):
                jobs.recover(self.index, self.job, now=value)
        for value in (True, float("nan"), 0, 86401):
            with self.subTest(value=value), self.assertRaises(protocol.ProtocolError):
                jobs.claim(self.index, self.job, self.state["revision"], "A", value, now=100)

    def test_empty_existing_history_cannot_be_reset_to_prepared(self):
        self.claim()
        self.change("begin")
        for path in (self.index / self.job).glob("*.json"):
            path.unlink()
        with self.assertRaisesRegex(protocol.ProtocolError, "empty job history"):
            jobs.register(self.index, self.info["request_path"])

    def test_cli_defaults_to_current_round_with_explicit_full_history(self):
        self.claim()
        self.change("begin")
        protocol.publish(self.info["result_path"], self.response(status="blocked", blocked_reason="Which suffix?"))
        follow = self.prepare(cwd=None, parent_depth=None, previous=self.info["request_path"])
        self.change("activate", {"request_path": follow["request_path"]})
        argv = [sys.executable, str(Path(jobs.__file__)), "recover", "--index", str(self.index), "--job", self.job]
        brief = json.loads(subprocess.run(argv, capture_output=True, text=True, check=True).stdout)["state"]
        full = json.loads(subprocess.run([*argv, "--history"], capture_output=True, text=True, check=True).stdout)["state"]
        self.assertEqual(brief["round_count"], 2)
        self.assertEqual(brief["attempt_count"], 1)
        self.assertEqual(brief["attempts"], [])  # never suggest the old attempt is the new round's attempt
        self.assertEqual(len(brief["rounds"]), 1)
        self.assertEqual(len(full["rounds"]), 2)
        self.assertEqual(len(full["attempts"]), 1)

    def test_launch_argv_preserves_empty_and_literal_arguments(self):
        self.claim()
        argv = ["runner", "--option", "", " ", "$(not-a-command)", "`literal`"]
        self.change("update", {"launch": {"argv": argv, "profile": None}})
        self.assertEqual(jobs.recover(self.index, self.job)["state"]["launch"]["argv"], argv)
