"""Durable controller review state, independent of delivery cursors and terminal input."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from unittest.mock import patch

from test_token_tools import ToolCase, protocol


class ReviewTests(ToolCase):
    def make_watch(self):
        import watch
        self.record()
        handle = watch.init_watch([self.info["request_path"]], self.root)["watch_path"]
        with patch.object(watch, "observe", return_value={"state": "working", "screen": "Allow once / Deny"}):
            events = watch.poll_watch(handle, now=100)["events"]
        seq = next(event["seq"] for event in events if event["kind"] == "attention")
        return handle, seq

    def test_advanced_delivery_cursor_does_not_hide_unresolved_attention(self):
        import reviews
        import watch
        handle, seq = self.make_watch()
        self.assertEqual(watch.read_events(handle, after=1000)["events"], [])
        with patch.object(watch, "observe", side_effect=AssertionError("pending must not probe terminal")):
            pending = reviews.pending(handle, now=135)
        self.assertEqual(pending["counts"], dict(open=1, waiting_user=0, overdue=1))
        self.assertEqual(pending["action_required"][0]["seq"], seq)
        self.assertEqual(pending["action_required"][0]["revision"], 0)
        self.assertNotIn("screen_tail", pending["action_required"][0])
        self.assertFalse((Path(handle).parent / "reviews").exists())

    def test_waiting_user_survives_restart_and_handled_is_auditable(self):
        import reviews
        handle, seq = self.make_watch()
        event_path = Path(handle).parent / "events" / f"{seq:012d}.json"
        before = event_path.read_bytes()
        waiting = reviews.record_review(handle, seq, "waiting_user", "Question sent; waiting for a decision.", 0, now=110)
        pending = self.run_tool("watch.py", "pending", "--watch", handle)
        self.assertEqual(pending.returncode, 0, pending.stderr)
        value = json.loads(pending.stdout)
        self.assertEqual(value["action_required"], [])
        self.assertEqual(value["waiting_user"][0]["note"], waiting["review"]["note"])
        handled = reviews.record_review(handle, seq, "handled", "Inspected live UI; operation completed.", 1, now=140)
        self.assertEqual(handled["review"]["revision"], 2)
        self.assertEqual(reviews.pending(handle)["counts"]["open"], 0)
        self.assertEqual(reviews.pending(handle)["counts"]["waiting_user"], 0)
        self.assertEqual(event_path.read_bytes(), before)
        receipts = sorted((Path(handle).parent / "reviews" / f"{seq:012d}").glob("*.json"))
        self.assertEqual(len(receipts), 2)
        self.assertEqual(protocol.read_json(receipts[0])["status"], "waiting_user")

    def test_stale_writer_rejected_and_lost_reply_retry_is_idempotent(self):
        import reviews
        handle, seq = self.make_watch()
        first = reviews.record_review(handle, seq, "waiting_user", "Asked once", 0)
        retry = reviews.record_review(handle, seq, "waiting_user", "Asked once", 0)
        self.assertTrue(retry["duplicate"])
        self.assertEqual(first["review"], retry["review"])
        with self.assertRaisesRegex(ValueError, "revision"):
            reviews.record_review(handle, seq, "handled", "Stale controller", 0)
        reviews.record_review(handle, seq, "handled", "Current controller", 1)
        with self.assertRaisesRegex(ValueError, "revision"):
            reviews.record_review(handle, seq, "waiting_user", "Asked once", 0)

    def test_concurrent_writers_cannot_both_change_same_revision(self):
        import reviews
        handle, seq = self.make_watch()
        def record(index):
            try:
                reviews.record_review(handle, seq, "handled", f"Controller {index}", 0)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            accepted = list(pool.map(record, range(4)))
        self.assertEqual(sum(accepted), 1)

    def test_failed_write_does_not_reserve_revision(self):
        import reviews
        handle, seq = self.make_watch()
        with patch.object(reviews.protocol, "publish", side_effect=OSError("disk full")), self.assertRaises(OSError):
            reviews.record_review(handle, seq, "handled", "Checked", 0)
        result = reviews.record_review(handle, seq, "handled", "Checked", 0)
        self.assertEqual(result["review"]["revision"], 1)

    def test_reappearing_dialog_is_a_new_unreviewed_event(self):
        import reviews
        import watch
        handle, seq = self.make_watch()
        reviews.record_review(handle, seq, "handled", "Original dialog resolved", 0)
        with patch.object(watch, "observe", return_value={"state": "working", "screen": "running"}):
            watch.poll_watch(handle, now=101)
        with patch.object(watch, "observe", return_value={"state": "working", "screen": "Allow once / Deny"}):
            watch.poll_watch(handle, now=102)
        pending = reviews.pending(handle, now=103)
        self.assertEqual(pending["counts"]["open"], 1)
        self.assertGreater(pending["action_required"][0]["seq"], seq)

    def test_priority_and_bounded_lists_do_not_drop_events(self):
        import reviews
        import watch
        handle, seq = self.make_watch()
        with patch.object(watch, "observe", return_value={"state": "done", "screen": "done"}):
            watch.poll_watch(handle, now=101)
        limited = reviews.pending(handle, limit=1, now=102)
        self.assertEqual(limited["action_required"][0]["seq"], seq)
        self.assertTrue(limited["more_action_required"])
        reviews.record_review(handle, seq, "waiting_user", "Waiting for user", 0)
        limited = reviews.pending(handle, limit=1, now=102)
        self.assertEqual(len(limited["action_required"]), 1)
        self.assertEqual(len(limited["waiting_user"]), 1)

    def test_corrupted_or_foreign_event_and_receipt_fail_closed(self):
        import reviews
        handle, seq = self.make_watch()
        record = reviews.record_review(handle, seq, "waiting_user", "Asked", 0)
        event_path = Path(handle).parent / "events" / f"{seq:012d}.json"
        original = event_path.read_text()
        event = json.loads(original)
        for changes in ({"job_id": "0" * 32}, {"message": "Replaced event"},
                        {"kind": "observation_recovered"}):
            event_path.write_text(json.dumps({**event, **changes}))
            with self.assertRaises(ValueError):
                reviews.pending(handle)
        event_path.write_text(original)
        receipt = Path(record["path"])
        value = json.loads(receipt.read_text())
        receipt.write_text(json.dumps({**value, "status": "anything"}))
        with self.assertRaises(ValueError):
            reviews.pending(handle)

    def test_reopen_requires_current_revision_and_preserves_history(self):
        import reviews
        handle, seq = self.make_watch()
        reviews.record_review(handle, seq, "handled", "Reviewed", 0)
        reviews.record_review(handle, seq, "open", "Need to recheck live state", 1)
        self.assertEqual(reviews.pending(handle)["action_required"][0]["revision"], 2)

    def test_review_works_while_observer_owns_write_lock(self):
        import reviews
        import watch
        handle, seq = self.make_watch()
        with watch.locked(Path(handle).parent):
            reviews.record_review(handle, seq, "handled", "Independent controller receipt", 0)
        self.assertEqual(reviews.pending(handle)["counts"]["open"], 0)

    def test_invalid_input_does_not_create_review_storage(self):
        import reviews
        handle, seq = self.make_watch()
        for changes in ({"status": "approved"}, {"note": " "}, {"note": "x" * 2001},
                        {"expected_revision": True}, {"seq": -1}, {"seq": True}):
            args = dict(watch_path=handle, seq=seq, status="handled", note="Checked", expected_revision=0)
            args.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                reviews.record_review(**args)
        self.assertFalse((Path(handle).parent / "reviews").exists())
        with self.assertRaises(FileNotFoundError):
            reviews.record_review(handle, 999, "handled", "Checked", 0)

    def test_cli_review_does_not_send_input_and_preserves_unicode_note(self):
        handle, seq = self.make_watch()
        note = self.root / "note.txt"
        note.write_text("已询问用户；等待答复。\n保留原生审批。", encoding="utf-8")
        result = self.run_tool("watch.py", "review", "--watch", handle, "--seq", seq,
                               "--status", "waiting_user", "--expected-revision", 0, "--note-file", note)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["review"]["note"], note.read_text())

    def test_waiting_user_pagination_reaches_items_beyond_first_page(self):
        import reviews
        import watch
        handle, seq = self.make_watch()
        with patch.object(watch, "observe", return_value={"state": "done", "screen": "done"}):
            second = watch.poll_watch(handle, now=101)["events"][0]["seq"]
        for number in (seq, second):
            reviews.record_review(handle, number, "waiting_user", f"Question {number}", 0)
        first = reviews.pending(handle, limit=1)
        self.assertTrue(first["more_waiting_user"])
        next_page = reviews.pending(handle, limit=1, waiting_after=first["next_waiting_after"])
        self.assertEqual([e["seq"] for e in next_page["waiting_user"]], [second])
        self.assertFalse(next_page["more_waiting_user"])
        self.assertEqual(next_page["counts"]["waiting_user"], 2)

    def test_review_status_recovers_revision_even_when_handled(self):
        import reviews
        handle, seq = self.make_watch()
        reviews.record_review(handle, seq, "handled", "Verified", 0)
        result = self.run_tool("watch.py", "review-status", "--watch", handle, "--seq", seq)
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["review"]["status"], "handled")
        self.assertEqual(status["review"]["revision"], 1)
        self.assertTrue(Path(status["history_directory"]).is_dir())

    def test_misnamed_event_file_is_rejected_instead_of_duplicated(self):
        import reviews
        handle, seq = self.make_watch()
        directory = Path(handle).parent / "events"
        (directory / f"{seq}.json").write_bytes((directory / f"{seq:012d}.json").read_bytes())
        with self.assertRaisesRegex(ValueError, "filename"):
            reviews.pending(handle)

    def test_pending_rejects_invalid_limits_and_time_options(self):
        import reviews
        handle, _ = self.make_watch()
        for changes in ({"limit": True}, {"limit": 0}, {"limit": 201}, {"waiting_after": -1},
                        {"action_offset": -1}, {"action_offset": True}, {"overdue_after": True},
                        {"overdue_after": float("nan")}, {"now": float("inf")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                reviews.pending(handle, **changes)

    def test_open_items_can_be_paged_without_false_handling_or_user_questions(self):
        import reviews
        import watch
        handle, seq = self.make_watch()
        with patch.object(watch, "observe", return_value={"state": "done", "screen": "done"}):
            second = watch.poll_watch(handle, now=101)["events"][0]["seq"]
        reviews.record_review(handle, seq, "open", "Need independent investigation first", 0)
        first = reviews.pending(handle, limit=1)
        self.assertEqual(first["action_required"][0]["seq"], seq)
        self.assertTrue(first["more_action_required"])
        page = reviews.pending(handle, limit=1, action_offset=first["next_action_offset"])
        self.assertEqual([e["seq"] for e in page["action_required"]], [second])
        self.assertFalse(page["more_action_required"])
        self.assertEqual(page["counts"]["open"], 2)
