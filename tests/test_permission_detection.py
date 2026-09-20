"""Regression screens and controller review ordering from native supervision."""

import json
from pathlib import Path

import test_supervision
import reviews
import watch


# Reuse helpers without inheriting (and rerunning) the source test suite.
from test_token_tools import ToolCase


class PermissionDetectionTests(ToolCase):
    make_watch = test_supervision.SupervisionTests.make_watch
    poll = test_supervision.SupervisionTests.poll

    def test_stale_blocked_without_prompt_requests_review_without_claiming_dialog(self):
        handle = self.make_watch()
        prompt = 'Would you like to run the following command?\n$ probe\n1. Yes, proceed (y)\n2. No (esc)'
        self.assertIn('attention', [e['kind'] for e in self.poll(handle, prompt, 0, state='blocked')])
        screen = ('✔ You approved codex to run probe this time\n'
                  '• Waiting for background terminal (1s)\n› Ask Codex to do anything')
        events = self.poll(handle, screen, 1, state='blocked')
        self.assertNotIn('attention', [e['kind'] for e in events])
        review = next(e for e in events if e['kind'] == 'review_due')
        self.assertEqual(review['confidence'], 'unconfirmed_blocked')
        self.assertNotIn('attention', [e['kind'] for e in self.poll(handle, screen.replace('1s', '2s'), 2, state='blocked')])
        self.assertIn('review_due', [e['kind'] for e in self.poll(handle, screen.replace('1s', '31s'), 31, state='blocked')])
        self.assertIn('attention', [e['kind'] for e in self.poll(handle, prompt.replace('probe', 'other'), 32, state='blocked')])

    def test_unknown_blocked_still_has_immediate_review_and_input_remains_blocked(self):
        handle = self.make_watch()
        events = self.poll(handle, 'Vendor-specific unresolved question', 0, state='blocked')
        self.assertIn('state_changed', [e['kind'] for e in events])
        self.assertIn('review_due', [e['kind'] for e in events])
        self.assertTrue(reviews.pending(handle, now=1)['action_required'])
        self.assertIsNotNone(watch.dialog_key('Vendor-specific unresolved question', 'blocked'))

    def test_blind_interval_requires_fresh_unconfirmed_blocked_review(self):
        config = {'review_interval': 30, 'stall_after': 120, 'deadline': None}
        old = {}
        sample = {'state': 'blocked', 'screen': 'Waiting for background terminal'}
        first = watch.signals(sample, old, 0, config)
        self.assertIn('review_due', [kind for kind, _ in first])
        issue = old['fallback_issue']
        self.assertEqual(watch.signals(sample, old, 1, config), [])
        watch.signals({'error': 'read failed'}, old, 2, config)
        recovered = watch.signals(sample, old, 3, config)
        self.assertIn('review_due', [kind for kind, _ in recovered])
        self.assertNotIn('attention', [kind for kind, _ in recovered])
        self.assertNotEqual(issue, old['fallback_issue'])

    def test_sanitized_native_screens(self):
        fixture = Path(__file__).parent / 'fixtures' / 'permission-screens.json'
        for sample in json.loads(fixture.read_text())['samples']:
            with self.subTest(sample=sample['name']):
                self.assertEqual(watch.dialog_key(sample['screen'], sample['state']) is not None,
                                 sample['attention'])

    def test_prose_and_history_do_not_create_dialog_alerts(self):
        for text in (
            'We should approve or deny each request after review.',
            '任务：需要授权时先记录，禁止自动批准。',
            'Previous dialog: Allow once / Deny\nNow running normally',
            'The docs say "Do you want to proceed?" when confirmation is needed.',
            '• To log in, use the sign in command.',
            '| action | approve | deny |',
            'Command: print("deny")\nTool output: permission required',
        ):
            with self.subTest(text=text):
                self.assertIsNone(watch.dialog_key(text, 'working'))

    def test_partial_and_unknown_prompt_shapes_still_surface(self):
        for text in (
            'Would you like to run the following command?\n$ read a.py',
            '$ read a.py\n› 1. Yes, proceed (y)',
            'Allow once / Deny',
            'Permission required: read a.py',
            'Do you want to proceed?',
            'Trust this folder?',
            'Sign in to continue',
            '是否允许读取 a.py？',
            '需要授权：读取 a.py',
            '允许一次 / 拒绝',
        ):
            for state in ('working', 'unknown'):
                with self.subTest(text=text, state=state):
                    self.assertIsNotNone(watch.dialog_key(text, state))
        self.assertIsNotNone(watch.dialog_key('unrecognized blocking UI', 'blocked'))

    def test_unknown_layout_keeps_changed_screen_fallback(self):
        handle = self.make_watch()
        self.poll(handle, 'Vendor question loading', 0, state='unknown')
        events = self.poll(handle, 'Choose a vendor-specific action', 30, state='unknown')
        self.assertIn('review_due', [event['kind'] for event in events])
        self.assertNotIn('attention', [event['kind'] for event in events])

    def test_complete_frame_ignores_approval_words_in_surrounding_prose(self):
        frame = ('Would you like to run the following command?\n$ write a.py\n'
                 '› 1. Yes, proceed (y)\n2. No (esc)')
        first = watch.dialog_key('任务：沙箱拒绝则请求批准。\n' + frame + '\nWorking 1s', 'working')
        second = watch.dialog_key('New task prose: approve or deny.\n' + frame + '\nWorking 2s', 'working')
        self.assertEqual(first, second)
        self.assertNotEqual(first, watch.dialog_key(frame.replace('a.py', 'b.py'), 'working'))

    def test_record_a_resolution_immediately_without_handling_next_operation(self):
        handle = self.make_watch()
        first = next(e for e in self.poll(handle, 'Command: write a\nAllow once / Deny', 100)
                     if e['kind'] == 'attention')
        action, confirmation = self.root / 'action.json', self.root / 'confirmation.json'
        action.write_text(json.dumps({'operation': 'a', 'sent_at': 121}))
        # Sent input does not remove the old pending incident.
        self.assertEqual(reviews.pending(handle, now=121)['counts']['open'], 1)
        self.poll(handle, 'a ran; working', 122)
        confirmation.write_text(json.dumps({'operation': 'a', 'observed_at': 122,
                                             'screen': 'a ran; working'}))
        second = next(e for e in self.poll(handle, 'Command: write b\nAllow once / Deny', 123)
                      if e['kind'] == 'attention')
        receipt = reviews.record_review(handle, first['seq'], 'handled', 'a verified live', 0, now=124,
            action_evidence=str(action), resolution_evidence=str(confirmation), resolved_at=122)['review']
        self.assertEqual(receipt['resolved_at'], 122)
        pending = reviews.pending(handle, now=124)['action_required']
        self.assertEqual([e['seq'] for e in pending], [second['seq']])
        self.assertEqual(pending[0]['revision'], 0)
        self.assertEqual(pending[0]['status'], 'open')
        self.poll(handle, 'working', 125)
        again = next(e for e in self.poll(handle, 'Command: write a\nAllow once / Deny', 126)
                     if e['kind'] == 'attention')
        self.assertNotEqual(first['issue_id'], again['issue_id'])
        self.assertEqual(reviews.review_status(handle, again['seq'])['review']['revision'], 0)
