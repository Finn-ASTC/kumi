"""F04: redraw budgets, conservative dialog identity and independent timing."""
import argparse
import threading
import time
from pathlib import Path
from unittest.mock import patch

from test_token_tools import ToolCase
import protocol
import reviews
import watch


class SupervisionTests(ToolCase):
    def make_watch(self):
        self.record()
        return watch.init_watch([self.info['request_path']], self.root)['watch_path']

    def poll(self, handle, screen, now=0, state='working'):
        with patch.object(watch, 'observe', return_value={'state': state, 'screen': screen}):
            return watch.poll_watch(handle, now=now)['events']

    def test_ten_minute_redraw_budget_and_single_pending_group(self):
        handle = self.make_watch()
        events = []
        for now in range(0, 601, 15):
            events += self.poll(handle, f'Processing file {now}', now)
        reminders = [e for e in events if e['kind'] == 'review_due']
        self.assertEqual([e['observed_at'] for e in reminders], [30, 90, 210, 450])
        pending = reviews.pending(handle, now=601)['action_required']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['occurrences'], 4)
        self.assertEqual(pending[0]['detected_at'], 30)
        reviews.record_review(handle, pending[0]['seq'], 'handled', 'Latest evidence inspected', 0, now=602)
        self.assertEqual(reviews.pending(handle)['action_required'], [])
        self.poll(handle, 'Processing file 750', 750)
        self.assertEqual(len(reviews.pending(handle)['action_required']), 1)

    def test_known_dialog_excludes_outside_telemetry_preserves_full_operation(self):
        handle = self.make_watch()
        command = 'Command: python - <<PY\nprint(12)\n' + '\n'.join(f'# line {i}' for i in range(12)) + '\nPY'
        first = self.poll(handle, 'Elapsed 1s\n' + command + '\nAllow once / Deny\nElapsed 1s')
        same = self.poll(handle, 'Elapsed 2s\n' + command + '\nAllow once / Deny\nElapsed 2s', 15)
        self.assertEqual(sum(e['kind'] == 'attention' for e in first), 1)
        self.assertNotIn('attention', [e['kind'] for e in same])
        changed = self.poll(handle, 'Elapsed 3s\n' + command.replace('print(12)', 'print(13)') + '\nAllow once / Deny', 16)
        self.assertIn('attention', [e['kind'] for e in changed])

    def test_codex_prompt_options_and_reappearance_get_fresh_incident(self):
        handle = self.make_watch()
        screen = 'Would you like to run the following command?\n\n$ read a.py\n\n1. Yes, proceed (y)\n2. No (esc)'
        first = next(e for e in self.poll(handle, screen) if e['kind'] == 'attention')
        changed = next(e for e in self.poll(handle, screen.replace('2. No (esc)', '2. Cancel (esc)'), 1) if e['kind'] == 'attention')
        self.poll(handle, 'running', 2)
        again = next(e for e in self.poll(handle, screen, 3) if e['kind'] == 'attention')
        self.assertEqual(first['correlation_key'], again['correlation_key'])
        self.assertNotEqual(first['issue_id'], again['issue_id'])
        self.assertNotEqual(changed['correlation_key'], again['correlation_key'])

    def test_error_gap_redetects_dialog_and_state_change_resets_fallback(self):
        handle = self.make_watch()
        self.poll(handle, 'Command: read 1\nAllow once / Deny')
        with patch.object(watch, 'observe', side_effect=OSError('offline')):
            watch.poll_watch(handle, now=1)
        after = self.poll(handle, 'Command: read 1\nAllow once / Deny', 2)
        self.assertIn('attention', [e['kind'] for e in after])

    def test_group_preserves_question_note_but_new_reminder_stays_open(self):
        handle = self.make_watch()
        self.poll(handle, 'unknown UI 0')
        event = next(e for e in self.poll(handle, 'unknown UI 1', 30) if e['kind'] == 'review_due')
        reviews.record_review(handle, event['seq'], 'waiting_user', 'Asked once about unknown UI', 0, now=31)
        self.poll(handle, 'unknown UI 2', 90)
        pending = reviews.pending(handle, now=91)
        self.assertEqual(len(pending['waiting_user']), 1)
        self.assertEqual(len(pending['action_required']), 1)
        item = pending['action_required'][0]
        self.assertEqual(item['status'], 'open')
        self.assertEqual(item['related_reviews'][0]['note'], 'Asked once about unknown UI')

    def test_review_action_and_resolution_are_separate_and_retry_pinned(self):
        handle = self.make_watch()
        event = next(e for e in self.poll(handle, 'Allow once / Deny', 100) if e['kind'] == 'attention')
        action = self.root / 'action.txt'
        action.write_text('Sent authorized response; result not yet verified')
        receipt = reviews.record_review(handle, event['seq'], 'handled', 'Input sent', 0, now=110,
                                        action_evidence=str(action))['review']
        self.assertIsNone(receipt['resolved_at'])
        self.assertIsNone(receipt['resolution_evidence'])
        self.assertTrue(receipt['action_evidence']['sha256'])
        action.write_text('changed evidence')
        with self.assertRaisesRegex(ValueError, 'revision'):
            reviews.record_review(handle, event['seq'], 'handled', 'Input sent', 0, now=110,
                                  action_evidence=str(action))
        resolved = self.root / 'resolved.json'
        resolved.write_text('{"state":"working"}')
        receipt = reviews.record_review(handle, event['seq'], 'handled', 'Verified live target', 1, now=120,
                                        resolution_evidence=str(resolved), resolved_at=119)['review']
        self.assertEqual(receipt['resolved_at'], 119)
        with self.assertRaises(ValueError):
            reviews.record_review(handle, event['seq'], 'handled', 'No evidence', 2, now=125, resolved_at=124)

    def test_review_cli_pins_outcome_evidence_and_requires_resolution_pair(self):
        handle = self.make_watch()
        event = next(e for e in self.poll(handle, 'Allow once / Deny', 100) if e['kind'] == 'attention')
        note, evidence = self.root / 'note.txt', self.root / 'action.log'
        note.write_text('Verified current UI')
        evidence.write_text('Fixture resumed')
        result = self.run_tool('watch.py', 'review', '--watch', handle, '--seq', event['seq'],
                               '--status', 'handled', '--expected-revision', 0, '--note-file', note,
                               '--action-evidence', evidence, '--resolution-evidence', evidence,
                               '--resolved-at', 120)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = reviews.review_status(handle, event['seq'])['review']
        self.assertEqual(receipt['resolution_evidence']['path'], str(evidence))
        bad = self.run_tool('watch.py', 'review', '--watch', handle, '--seq', event['seq'],
                            '--status', 'handled', '--expected-revision', 1, '--note-file', note,
                            '--resolved-at', 125)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(reviews.review_status(handle, event['seq'])['review']['revision'], 1)

    def test_later_handled_reminder_does_not_remove_earlier_explicit_question(self):
        handle = self.make_watch()
        self.poll(handle, 'tick 0')
        first = next(e for e in self.poll(handle, 'tick 30', 30) if e['kind'] == 'review_due')
        reviews.record_review(handle, first['seq'], 'waiting_user', 'Question still unanswered', 0, now=31)
        self.poll(handle, 'tick 90', 90)
        last = next(e for e in self.poll(handle, 'tick 210', 210) if e['kind'] == 'review_due')
        self.assertEqual(reviews.pending(handle)['action_required'][0]['occurrences'], 2)
        reviews.record_review(handle, last['seq'], 'handled', 'Latest redraw checked', 0, now=211)
        pending = reviews.pending(handle)
        self.assertEqual(pending['action_required'], [])
        self.assertEqual(pending['waiting_user'][0]['seq'], first['seq'])
        self.assertEqual(len(watch.read_events(handle)['events']), 4)

    def test_fast_target_evidence_published_while_slow_read_is_in_flight(self):
        self.record()
        other = self.prepare()
        protocol.record(argparse.Namespace(request=other['request_path'], mode='insider',
            session='user-session', agent='other', pane='w2:p1', workspace='w2',
            parent_pane='w1:p1', owns_agent=True, owns_pane=True, owns_workspace=True, owns_session=False))
        handle = watch.init_watch([self.info['request_path'], other['request_path']], self.root)['watch_path']
        release = threading.Event()
        def observe(target):
            if target['round_id'] == self.info['round_id']:
                release.wait(2)
            return {'state': 'working', 'screen': 'Allow once / Deny'}
        output, errors = [], []
        def sweep():
            try:
                output.append(watch.poll_watch(handle))
            except Exception as exc:
                errors.append(exc)
        with patch.object(watch, 'observe', side_effect=observe):
            worker = threading.Thread(target=sweep)
            worker.start()
            try:
                deadline = time.monotonic() + 1
                observed = []
                while time.monotonic() < deadline:
                    observed = watch.read_events(handle)['events']
                    if observed:
                        break
                    time.sleep(.01)
                self.assertTrue(observed, 'fast target hidden behind slow read')
                self.assertTrue(all(e['round_id'] == other['round_id'] for e in observed))
            finally:
                release.set()
                worker.join(3)
        self.assertEqual(errors, [])
        self.assertFalse(worker.is_alive())
        state = protocol.read_json(Path(handle).parent / 'state.json')['targets']
        fast, slow = state[other['round_id']], state[self.info['round_id']]
        self.assertLess(fast['last_checked_at'], slow['last_checked_at'])
        self.assertGreater(slow['read_duration_seconds'], fast['read_duration_seconds'])
