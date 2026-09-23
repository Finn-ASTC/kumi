"""Per-round reviews preserve transition facts and never accept a successor by accident."""

import copy
import json
from pathlib import Path

import test_completion as fixtures
from test_token_tools import ToolCase
import jobs
import protocol


class RoundAcceptanceTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.record()
        self.index = self.root / "jobs"
        self.job = self.info["job_id"]
        self.proof = self.root / "proof.txt"
        self.proof.write_text("Independent review of exact round")
        self.state = jobs.register(self.index, self.info["request_path"], now=100)
        self.state = jobs.claim(self.index, self.job, 1, "controller", 300, now=100)
        self.token = self.state["lease"]["token"]
        self.change(
            "update", {"native": {"session_id": "native-root", "turn_id": "turn-1"}}
        )

    change = fixtures.CompletionTests.change
    acceptance = fixtures.CompletionTests.acceptance
    publish = fixtures.CompletionTests.publish
    host = fixtures.CompletionTests.host
    verified_snapshot = fixtures.CompletionTests.verified_snapshot

    def follow(self):
        return self.prepare(
            cwd=None, parent_depth=None, previous=self.state["active_request"]
        )

    def status(self, round_id=None):
        return jobs.round_status(
            self.index, self.job, round_id or self.info["round_id"]
        )

    def legacy_activate(self, follow):
        # Reproduce an immutable pre-DH-01 history which switched before review.
        state = copy.deepcopy(self.state)
        record = jobs.request_record(follow["request_path"])
        state.update(
            active_request=record["request_path"],
            round_id=record["round_id"],
            resources=jobs.resources_for(record["request_path"]),
            submission="prepared",
            completion={},
        )
        state["rounds"].append(record)
        state["native"]["turn_id"] = None
        state["monitor"] = {"owner": None, "watch_path": None, "expires_at": None}
        self.state = jobs.save(self.index, state, "activate", 102)

    def test_unreviewed_success_cannot_activate_and_changes_nothing(self):
        self.publish()
        follow = self.follow()
        before = self.state["revision"]
        with self.assertRaisesRegex(ValueError, "acceptance"):
            self.change("activate", {"request_path": follow["request_path"]})
        self.assertEqual(jobs.load(self.index, self.job)["revision"], before)

    def test_accepted_round_transition_survives_later_backfill_rejection(self):
        self.publish()
        self.change("accept", self.acceptance())
        follow = self.follow()
        self.change("activate", {"request_path": follow["request_path"]}, now=102)
        transition = self.status()["transition"]
        self.assertEqual(transition["acceptance_status"], "accepted")
        current = copy.deepcopy(self.state)
        old_files = {p: p.read_bytes() for p in (self.index / self.job).glob("*.json")}
        self.change(
            "accept",
            self.acceptance(round_id=self.info["round_id"], verdict="rejected"),
            now=105,
        )
        status = self.status()
        self.assertEqual(status["acceptance"]["status"], "rejected")
        self.assertEqual(status["acceptance_record"]["recorded_at"], 105)
        self.assertTrue(status["acceptance_record"]["backfilled"])
        self.assertEqual(status["transition"], transition)
        self.assertFalse(status["is_current_round"])
        for key in (
            "active_request",
            "round_id",
            "completion",
            "native",
            "resources",
            "monitor",
            "submission",
        ):
            self.assertEqual(self.state[key], current[key], key)
        self.assertEqual(old_files, {p: p.read_bytes() for p in old_files})
        self.assertFalse(
            jobs.recover(self.index, self.job, now=106)["completion"][
                "accepted_by_verifier"
            ]
        )
        self.assertNotIn("token", json.dumps(status))

    def test_rejected_success_allows_rework_but_not_completed_closure(self):
        self.publish()
        self.change("accept", self.acceptance(verdict="rejected"))
        with self.assertRaisesRegex(ValueError, "completion"):
            self.change(
                "close",
                {
                    "outcome": "completed",
                    "evidence_path": str(self.proof),
                    "note": "no",
                },
            )
        self.change("activate", {"request_path": self.follow()["request_path"]})
        self.assertEqual(self.status()["transition"]["acceptance_status"], "rejected")

    def test_blocked_continuation_needs_no_success_acceptance(self):
        self.publish(status="blocked", blocked_reason="Need decision")
        self.change("activate", {"request_path": self.follow()["request_path"]})
        self.assertEqual(
            self.status()["transition"]["acceptance_status"], "not_applicable"
        )
        with self.assertRaisesRegex(ValueError, "success"):
            self.change("accept", self.acceptance(round_id=self.info["round_id"]))

    def test_error_continuation_needs_no_success_acceptance(self):
        self.publish(status="error", error="Tool failed")
        self.change("activate", {"request_path": self.follow()["request_path"]})
        self.assertEqual(self.status()["transition"]["response_status"], "error")

    def test_changed_rejection_evidence_blocks_switch(self):
        self.publish()
        self.change("accept", self.acceptance(verdict="rejected"))
        self.proof.write_text("Changed review")
        with self.assertRaisesRegex(ValueError, "acceptance"):
            self.change("activate", {"request_path": self.follow()["request_path"]})
        self.assertEqual(self.status()["acceptance"]["status"], "invalid")

    def test_legacy_backfill_records_now_and_keeps_transition_unknown(self):
        self.publish()
        self.legacy_activate(self.follow())
        self.assertEqual(self.status()["acceptance"]["status"], "missing")
        self.change("accept", self.acceptance(round_id=self.info["round_id"]), now=106)
        status = self.status()
        self.assertEqual(status["acceptance"]["status"], "accepted")
        self.assertEqual(status["transition"]["acceptance_status"], "unknown_legacy")
        self.assertEqual(status["acceptance_record"]["recorded_at"], 106)

    def test_prepared_unactivated_and_foreign_rounds_cannot_receive_reviews(self):
        self.publish()
        for info in (self.follow(), self.prepare()):
            with (
                self.subTest(round=info["round_id"]),
                self.assertRaisesRegex(ValueError, "indexed"),
            ):
                self.change("accept", self.acceptance(round_id=info["round_id"]))
            with self.assertRaises(ValueError):
                self.status(info["round_id"])

    def test_historical_delivery_uses_its_own_attempt(self):
        self.publish(files_created=["answer.txt"])
        attempt = self.verified_snapshot()
        follow = self.follow()
        self.legacy_activate(follow)
        protocol.publish(
            follow["result_path"],
            self.response(round_id=follow["round_id"], files_created=["answer.txt"]),
        )
        wrong = self.verified_snapshot(follow)
        with self.assertRaisesRegex(ValueError, "identity"):
            self.change(
                "accept",
                self.acceptance(
                    kind="delivery", attempt_path=wrong, round_id=self.info["round_id"]
                ),
            )
        self.change(
            "accept",
            self.acceptance(
                kind="delivery", attempt_path=attempt, round_id=self.info["round_id"]
            ),
        )
        self.assertEqual(self.status()["acceptance"]["status"], "accepted")
        (Path(attempt).parent / "evidence/0001/stdout.log").write_text("tampered")
        self.assertEqual(self.status()["acceptance"]["status"], "invalid")

    def test_history_query_detects_changed_old_contract(self):
        self.publish()
        self.legacy_activate(self.follow())
        path = Path(self.info["request_path"])
        data = protocol.read_json(path)
        data["task"] = "changed contract"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "changed"):
            self.status()
        with self.assertRaises(ValueError):
            self.change("accept", self.acceptance(round_id=self.info["round_id"]))

    def test_current_result_change_invalidates_review_not_transition_history(self):
        self.publish()
        self.change("accept", self.acceptance())
        self.change("activate", {"request_path": self.follow()["request_path"]})
        path = Path(self.info["result_path"])
        data = protocol.read_json(path)
        data["output"] = "replacement"
        path.write_text(json.dumps(data))
        status = self.status()
        self.assertEqual(status["acceptance"]["status"], "invalid")
        self.assertEqual(status["transition"]["acceptance_status"], "accepted")
        with self.assertRaisesRegex(ValueError, "historical result changed"):
            self.change(
                "accept", self.acceptance(round_id=self.info["round_id"]), now=104
            )

    def test_multiple_successors_keep_each_review_and_transition_separate(self):
        self.publish()
        self.change("accept", self.acceptance(verdict="rejected"))
        second = self.follow()
        self.change("activate", {"request_path": second["request_path"]}, now=102)
        protocol.publish(
            second["result_path"], self.response(round_id=second["round_id"])
        )
        self.change("accept", self.acceptance(), now=103)
        third = self.follow()
        self.change("activate", {"request_path": third["request_path"]}, now=104)
        self.change("accept", self.acceptance(round_id=self.info["round_id"]), now=105)
        old = self.status()
        middle = self.status(second["round_id"])
        active = self.status(third["round_id"])
        self.assertEqual(old["transition"]["acceptance_status"], "rejected")
        self.assertEqual(old["acceptance"]["status"], "accepted")
        self.assertEqual(middle["transition"]["acceptance_status"], "accepted")
        self.assertEqual(middle["acceptance"]["status"], "accepted")
        self.assertFalse(middle["acceptance_record"]["backfilled"])
        self.assertEqual(active["acceptance"]["status"], "missing")

    def test_failed_delivery_rejection_allows_rework_but_changed_attempt_blocks_it(
        self,
    ):
        self.publish(files_created=["answer.txt"])
        attempt = self.verified_snapshot(exit_code=1)
        self.change(
            "accept",
            self.acceptance(kind="delivery", attempt_path=attempt, verdict="rejected"),
        )
        self.assertEqual(self.status()["acceptance"]["status"], "rejected")
        self.change("activate", {"request_path": self.follow()["request_path"]})
        (Path(attempt).parent / "evidence/0001/stdout.log").write_text("changed")
        self.assertEqual(self.status()["acceptance"]["status"], "invalid")

    def test_closed_current_review_changes_no_completed_closure_evidence(self):
        self.publish()
        self.change("accept", self.acceptance())
        self.change("host", self.host())
        self.change(
            "close",
            {
                "outcome": "completed",
                "evidence_path": str(self.proof),
                "note": "Verified",
            },
            now=102,
        )
        closure = copy.deepcopy(self.state["closed"])
        self.state = jobs.claim(
            self.index,
            self.job,
            self.state["revision"],
            "auditor",
            now=103,
            acceptance_only=True,
        )
        self.token = self.state["lease"]["token"]
        self.change("accept", self.acceptance(verdict="rejected"), now=104)
        self.assertEqual(self.state["closed"], closure)
        self.assertTrue(closure["completion"]["accepted_by_verifier"])
        self.assertEqual(self.status()["acceptance"]["status"], "rejected")
        self.assertTrue(self.status()["acceptance_record"]["backfilled"])
        self.assertFalse(
            jobs.recover(self.index, self.job, now=105)["completion"][
                "ready_to_complete"
            ]
        )

    def test_acceptance_only_lease_cannot_drive_an_open_job(self):
        self.change("release", {}, now=102)
        self.state = jobs.claim(
            self.index,
            self.job,
            self.state["revision"],
            "auditor",
            now=103,
            acceptance_only=True,
        )
        self.token = self.state["lease"]["token"]
        for action, payload in (
            ("begin", {}),
            ("update", {"deadline": 200}),
            (
                "close",
                {
                    "outcome": "cancelled",
                    "evidence_path": str(self.proof),
                    "note": "stop",
                },
            ),
        ):
            with self.assertRaisesRegex(ValueError, "acceptance-only"):
                self.change(action, payload, now=104)

    def test_backfill_requires_current_revision_and_live_lease(self):
        self.publish()
        self.legacy_activate(self.follow())
        for rev, token, now in (
            (self.state["revision"] - 1, self.token, 103),
            (self.state["revision"], "wrong", 103),
            (self.state["revision"], self.token, 500),
        ):
            with self.assertRaises(ValueError):
                jobs.change(
                    self.index,
                    self.job,
                    rev,
                    token,
                    "accept",
                    self.acceptance(round_id=self.info["round_id"]),
                    now=now,
                )

    def test_closed_job_audit_lease_only_updates_reviews_and_preserves_closure(self):
        self.publish()
        self.legacy_activate(self.follow())
        from control_fixtures import stopped
        self.state = stopped(self.index, self.state, self.token, self.proof, 103)
        self.change(
            "close",
            {
                "outcome": "cancelled",
                "evidence_path": str(self.proof),
                "note": "Stopped",
            },
            now=103,
        )
        closure = copy.deepcopy(self.state["closed"])
        with self.assertRaisesRegex(ValueError, "closed"):
            jobs.claim(
                self.index, self.job, self.state["revision"], "reviewer", now=104
            )
        self.state = jobs.claim(
            self.index,
            self.job,
            self.state["revision"],
            "reviewer",
            now=104,
            acceptance_only=True,
        )
        self.token = self.state["lease"]["token"]
        self.change("accept", self.acceptance(round_id=self.info["round_id"]), now=105)
        self.assertEqual(self.state["closed"], closure)
        self.assertEqual(self.status()["acceptance"]["status"], "accepted")
        self.assertEqual(
            jobs.recover(self.index, self.job, now=106)["next_action"], "closed"
        )
        for action, payload in (
            ("begin", {}),
            ("update", {"deadline": 200}),
            ("activate", {"request_path": self.info["request_path"]}),
            ("host", self.host()),
        ):
            with self.assertRaises(ValueError):
                self.change(action, payload, now=106)
        self.change("renew", {"lease_seconds": 30}, now=106)
        self.change("release", {}, now=107)
        self.assertIsNone(self.state["lease"])

    def test_round_status_cli_is_read_only_and_accept_cli_can_select_old_round(self):
        import time

        self.publish()
        self.legacy_activate(self.follow())
        # Current real-time lease for subprocess checks.
        self.state = jobs.claim(
            self.index, self.job, self.state["revision"], "cli", now=time.time()
        )
        self.token = self.state["lease"]["token"]
        payload = self.root / "acceptance.json"
        payload.write_text(json.dumps(self.acceptance()))
        result = self.run_tool(
            "jobs.py",
            "accept",
            "--index",
            self.index,
            "--job",
            self.job,
            "--expect-revision",
            self.state["revision"],
            "--token",
            self.token,
            "--round-id",
            self.info["round_id"],
            "--input",
            payload,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("historical_acceptance", json.loads(result.stdout))
        before = {p: p.read_bytes() for p in (self.index / self.job).glob("*.json")}
        result = self.run_tool(
            "jobs.py",
            "round-status",
            "--index",
            self.index,
            "--job",
            self.job,
            "--round-id",
            self.info["round_id"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["acceptance"]["status"], "accepted")
        self.assertNotIn(self.token, result.stdout)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_closed_audit_claim_cli_and_live_lease_exclusion(self):
        from control_fixtures import stopped
        self.state = stopped(self.index, self.state, self.token, self.proof, 101)
        self.change(
            "close",
            {
                "outcome": "cancelled",
                "evidence_path": str(self.proof),
                "note": "Stopped",
            },
        )
        result = self.run_tool(
            "jobs.py",
            "claim",
            "--index",
            self.index,
            "--job",
            self.job,
            "--expect-revision",
            self.state["revision"],
            "--owner",
            "auditor",
            "--acceptance-only",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout)
        self.assertEqual(state["lease"]["scope"], "acceptance")
        with self.assertRaisesRegex(ValueError, "live lease"):
            jobs.claim(
                self.index, self.job, state["revision"], "another", acceptance_only=True
            )
