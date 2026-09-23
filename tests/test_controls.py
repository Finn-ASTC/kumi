"""Exact intervention identity, uncertainty, cancellation and takeover regressions."""
import json
import os
from pathlib import Path
from unittest.mock import patch

from test_token_tools import ToolCase
import controls
import jobs
import protocol
import reviews
import watch


class ControlTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.record()
        self.index = self.root / 'jobs'
        self.job = self.info['job_id']
        self.state = jobs.register(self.index, self.info['request_path'], now=100)
        self.state = jobs.claim(self.index, self.job, self.state['revision'], 'controller', 100, now=100)
        self.token = self.state['lease']['token']
        self.handle = watch.init_watch([self.info['request_path']], self.root)['watch_path']
        self.screen = 'Command: write /fixture/a\nAllow once / Deny'
        self.event = self.poll(self.screen, 101)
        self.proof = self.root / 'decision.json'
        self.proof.write_text('{"decision":"deny this fixture operation"}')

    def poll(self, screen, now):
        with patch.object(watch, 'observe', return_value={'state':'blocked', 'screen':screen}):
            events = watch.poll_watch(self.handle, now=now)['events']
        return next((e for e in events if e['kind'] == 'attention'), None)

    def inspect(self, now=102, event=True, screen=None):
        with patch.object(watch, 'observe', return_value={'state':'blocked', 'screen':screen or self.screen}):
            return controls.inspect(self.index, self.job, watch_path=self.handle if event else None,
                                    seq=self.event['seq'] if event else None, now=now)

    def change(self, action, payload, now=103):
        self.state = jobs.change(self.index, self.job, self.state['revision'], self.token,
                                 action, payload, now=now)
        return self.state

    def begin(self, kind='deny', now=103, inspection=None):
        inspected = inspection or self.inspect(now=now-1, event=kind == 'deny')
        with patch.object(watch, 'observe', return_value={'state':'blocked', 'screen':self.screen}):
            return self.change('control-begin', {'kind':kind, 'inspection_path':inspected['inspection_path'],
                'method':{'kind':'keys', 'description':'Fixture exact deny control; not a universal Esc mapping'},
                'evidence_path':str(self.proof), 'note':'Authorized fixture scope'}, now)

    def receipt(self, status='confirmed', outcome='denied', now=105, **overrides):
        return self.change('control-receipt', {'control_id':self.state['control']['control_id'],
            'status':status, 'outcome':outcome, 'observed_at':now-1,
            'checks':{'operation':'clear','scope':'clear'} if outcome == 'denied' else
                {k:'clear' for k in controls.STOP_CHECKS},
            'evidence_path':str(self.proof), 'note':'Independently observed exact fixture outcome', **overrides}, now)

    def test_detail_returns_full_pinned_screen_and_actual_options(self):
        long_screen = 'Command: write a\n' + ('argument ' * 300) + '\nAllow once / Deny'
        event = self.poll(long_screen, 102)
        detail = reviews.event_detail(self.handle, event['seq'])
        self.assertEqual(detail['screen'], long_screen)
        self.assertEqual(detail['integrity'], 'pinned')
        self.assertGreater(len(detail['screen']), len(event['screen_tail']))
        self.assertEqual(detail['capture']['completeness'], 'unknown')
        self.assertFalse(detail['executes_commands'])

    def test_detail_rejects_replaced_evidence(self):
        path = Path(self.event['evidence_path'])
        data = protocol.read_json(path)
        data['screen'] = 'different operation'
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'evidence.*changed'):
            reviews.event_detail(self.handle, self.event['seq'])

    def test_intent_is_uncertain_and_same_incident_cannot_be_sent_twice(self):
        inspected = self.inspect()
        self.begin(inspection=inspected)
        self.assertEqual(self.state['control']['status'], 'uncertain')
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            self.begin(now=104, inspection=inspected)
        self.receipt()
        with self.assertRaisesRegex(ValueError, 'already|resolved'):
            self.begin(now=106, inspection=inspected)
        self.assertEqual([e['seq'] for e in reviews.pending(self.handle, now=106)['action_required']
                          if e['kind'] == 'attention'], [self.event['seq']])

    def test_stale_wrong_dialog_and_reappeared_incident_reject_before_intent(self):
        inspected = self.inspect()
        with self.assertRaisesRegex(ValueError, 'stale|expired'):
            self.begin(now=140, inspection=inspected)
        with self.assertRaisesRegex(ValueError, 'dialog'):
            self.inspect(screen='Command: write b\nAllow once / Deny')
        self.poll('Working', 104)
        self.poll(self.screen, 105)
        with self.assertRaisesRegex(ValueError, 'incident'):
            self.begin(now=106, inspection=inspected)

    def test_expired_owner_cannot_record_and_takeover_preserves_uncertainty(self):
        self.begin()
        old_token = self.token
        self.state = jobs.claim(self.index, self.job, self.state['revision'], 'successor', 100, now=201)
        self.token = self.state['lease']['token']
        self.assertEqual(self.state['control']['status'], 'uncertain')
        self.assertEqual(jobs.recover(self.index, self.job, now=202)['next_action'], 'reconcile_control')
        with self.assertRaisesRegex(ValueError, 'lease'):
            jobs.change(self.index, self.job, self.state['revision'], old_token, 'control-receipt', {}, now=202)
        self.receipt(now=203)
        self.assertEqual(self.state['control']['receipt']['owner'], 'successor')

    def test_uncertain_receipt_neither_resolves_dialog_nor_allows_repeat(self):
        self.begin()
        self.receipt(status='uncertain', outcome=None)
        self.assertEqual(self.state['control']['status'], 'uncertain')
        self.assertEqual([e['seq'] for e in reviews.pending(self.handle, now=106)['action_required']
                          if e['kind'] == 'attention'], [self.event['seq']])
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            self.change('begin', {}, now=106)

    def test_confirmation_checks_outcome_time_and_exact_control_id(self):
        self.begin()
        for override in ({'outcome':'stopped'}, {'observed_at':102}, {'control_id':'f'*32},
                         {'checks':{'operation':'unknown','scope':'clear'}}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.receipt(**override)
        self.receipt()
        with self.assertRaisesRegex(ValueError, 'resolved'):
            self.receipt(now=106)

    def test_known_not_sent_allows_fresh_inspection_retry(self):
        self.begin()
        old = self.state['control']['control_id']
        self.receipt(status='not_sent', outcome=None)
        self.begin(now=106)
        self.assertNotEqual(self.state['control']['control_id'], old)

    def test_cancel_requires_all_writers_checked_without_fabricating_result(self):
        self.begin('cancel')
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            self.change('close', {'outcome':'cancelled','evidence_path':str(self.proof),'note':'Sent stop'}, now=104)
        self.receipt(outcome='stopped')
        self.assertEqual(jobs.recover(self.index, self.job, now=105)['next_action'], 'close_cancelled')
        with self.assertRaisesRegex(ValueError, 'cancel'):
            self.change('begin', {}, now=106)
        with self.assertRaisesRegex(ValueError, 'cancel'):
            self.change('close', {'outcome':'failed','evidence_path':str(self.proof),'note':'Incorrect outcome'}, now=106)
        self.change('close', {'outcome':'cancelled','evidence_path':str(self.proof),'note':'Writers settled'}, now=106)
        self.assertEqual(self.state['closed']['control_id'], self.state['control']['control_id'])
        self.assertFalse(Path(self.info['result_path']).exists())

    def test_stop_confirmation_missing_background_check_is_rejected(self):
        self.begin('cancel')
        with self.assertRaisesRegex(ValueError, 'checks'):
            self.receipt(outcome='stopped', checks={'foreground':'clear'})
        self.assertIsNone(self.state['closed'])

    def test_inspection_target_native_or_resources_cannot_be_substituted(self):
        inspected = self.inspect()
        self.change('update', {'native':{'session_id':'new-session','turn_id':None}})
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.begin(now=104, inspection=inspected)

    def test_pending_control_blocks_identity_changes_but_allows_monitor_handoff(self):
        self.begin()
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            self.change('update', {'native':{'session_id':'different','turn_id':None}}, now=104)
        self.change('update', {'monitor':{'owner':'replacement','watch_path':self.handle,'expires_at':190}}, now=104)
        self.assertEqual(self.state['control']['status'], 'uncertain')

    def test_inspection_cli_and_detail_cli_return_readable_evidence(self):
        detail = self.run_tool('watch.py', 'detail', '--watch', self.handle, '--seq', self.event['seq'])
        self.assertEqual(detail.returncode, 0, detail.stderr)
        self.assertEqual(json.loads(detail.stdout)['screen'], self.screen)

    def test_legacy_event_is_readable_but_cannot_drive_new_deny(self):
        path = Path(self.handle).parent/'events'/f'{self.event["seq"]:012d}.json'
        event = protocol.read_json(path)
        event.pop('evidence_sha256')
        path.write_text(json.dumps(event))
        self.assertEqual(reviews.event_detail(self.handle, self.event['seq'])['integrity'], 'legacy_unpinned')
        with self.assertRaisesRegex(ValueError, 'pinned'):
            self.inspect()

    def test_cancel_confirmation_can_be_refreshed_after_expiry_without_new_input(self):
        self.begin('cancel')
        self.receipt(outcome='stopped')
        close = {'outcome':'cancelled','evidence_path':str(self.proof),'note':'Confirmed stopped'}
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.change('close', close, now=140)
        self.receipt(outcome='stopped', now=141)
        self.change('close', close, now=142)
        self.assertEqual(self.state['closed']['stop_status'], 'confirmed')

    def test_dead_transport_can_be_manually_reconciled_without_fabricating_success(self):
        with patch.object(watch, 'observe', side_effect=OSError('server unavailable')):
            inspected = controls.inspect(self.index, self.job, now=102)
            payload = {'kind':'cancel','inspection_path':inspected['inspection_path'],
                'method':{'kind':'keys','description':'cannot send'},
                'evidence_path':str(self.proof),'note':'Fixture target exited'}
            with self.assertRaisesRegex(ValueError, 'manual'):
                self.change('control-begin', payload)
            payload['method'] = {'kind':'manual','description':'Inspect owned process handles and side effects'}
            self.change('control-begin', payload)
        self.receipt(outcome='stopped')
        self.assertFalse(Path(self.info['result_path']).exists())

    def test_replaced_monitor_and_changed_screen_cannot_borrow_inspection(self):
        inspected = self.inspect()
        with patch.object(watch, 'observe', return_value={'state':'blocked','screen':'new UI'}), \
                self.assertRaisesRegex(ValueError, 'dialog|changed'):
            self.change('control-begin', {'kind':'deny','inspection_path':inspected['inspection_path'],
                'method':{'kind':'keys','description':'fixture'},'evidence_path':str(self.proof),'note':'fixture'})
        new = watch.init_watch([self.info['request_path']], self.root)['watch_path']
        self.change('update', {'monitor':{'owner':'new','watch_path':new,'expires_at':190}})
        with self.assertRaisesRegex(ValueError, 'replaced monitor'):
            self.begin(now=104, inspection=inspected)

    def test_allocated_job_cannot_close_cancelled_with_only_a_note(self):
        with self.assertRaisesRegex(ValueError, 'confirmed stop'):
            self.change('close', {'outcome':'cancelled','evidence_path':str(self.proof),'note':'not yet verified'})

    def test_bound_review_only_resolves_original_event_even_when_next_dialog_is_open(self):
        self.begin()
        with self.assertRaisesRegex(ValueError, 'confirmed'):
            controls.review(self.index, self.job, self.state['control']['control_id'], 0, 'Too soon', now=104)
        self.receipt()
        second = self.poll('Command: write b\nAllow once / Deny', 106)
        reviewed = controls.review(self.index, self.job, self.state['control']['control_id'], 0,
                                   'Exact a denied; b stays open', now=107)
        self.assertEqual(reviewed['review']['resolved_at'], 104)
        remaining = [e['seq'] for e in reviews.pending(self.handle, now=108)['action_required']
                     if e['kind'] == 'attention']
        self.assertEqual(remaining, [second['seq']])
        self.change('renew', {'lease_seconds':100}, now=108)
        self.assertTrue(controls.review(self.index, self.job, self.state['control']['control_id'], 0,
                                       'Exact a denied; b stays open', now=108)['duplicate'])

    def test_bound_review_rejects_changed_resolution_evidence(self):
        self.begin()
        self.receipt()
        self.proof.write_text('rewritten evidence')
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            controls.review(self.index, self.job, self.state['control']['control_id'], 0, 'Changed', now=106)

    def test_invalidated_dialog_can_reconcile_then_cancel_without_claiming_denial(self):
        self.begin()
        self.receipt(outcome='invalidated', checks={'operation':'clear','scope':'clear'})
        self.assertEqual(self.state['control']['receipt']['outcome'], 'invalidated')
        self.begin('cancel', now=107)
        self.receipt(outcome='stopped', now=109)
        self.assertEqual(len(self.state['controls']), 2)

    def test_new_submission_cannot_reuse_earlier_stop_confirmation(self):
        self.begin('interrupt')
        self.receipt(outcome='stopped')
        self.change('begin', {}, now=106)
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.change('close', {'outcome':'cancelled','evidence_path':str(self.proof),'note':'Old stop'}, now=107)

    def test_partial_capture_is_explicit_and_cannot_drive_deny(self):
        with patch.object(watch, 'observe', return_value={'state':'blocked','screen':self.screen,
                'capture':{'completeness':'unknown','locally_truncated':True}}), \
                self.assertRaisesRegex(ValueError, 'truncated'):
            controls.inspect(self.index, self.job, watch_path=self.handle, seq=self.event['seq'], now=102)

    def test_cli_begin_rejects_expired_lease_without_writing_intent(self):
        # A dead transport is deliberately used: only manual reconciliation is permitted.
        with patch.object(watch, 'observe', side_effect=OSError('offline')):
            inspected = controls.inspect(self.index, self.job)
        payload = self.root/'intent.json'
        payload.write_text(json.dumps({'kind':'cancel','inspection_path':inspected['inspection_path'],
            'method':{'kind':'manual','description':'Fixture offline process reconciliation'},
            'evidence_path':str(self.proof),'note':'Fixture only'}))
        # Historical deterministic lease is expired at real CLI time; rejection must not write.
        result = self.run_tool('jobs.py','control-begin','--index',self.index,'--job',self.job,
            '--expect-revision',self.state['revision'],'--token',self.token,'--input',payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('lease', result.stderr)
        self.assertNotIn('control', jobs.load(self.index, self.job))

    def test_another_open_job_cannot_control_the_same_target(self):
        other = self.prepare()
        original = self.info
        try:
            self.info = other
            self.record()
        finally:
            self.info = original
        jobs.register(self.index, other['request_path'], now=102)
        with self.assertRaisesRegex(ValueError, 'another open job'):
            self.begin()

    def test_unallocated_unstarted_job_can_cancel_without_a_transport(self):
        other = self.prepare()
        state = jobs.register(self.index, other['request_path'], now=102)
        state = jobs.claim(self.index, other['job_id'], state['revision'], 'controller', 100, now=102)
        state = jobs.change(self.index, other['job_id'], state['revision'], state['lease']['token'], 'close',
            {'outcome':'cancelled','evidence_path':str(self.proof),'note':'No resource or submission'}, now=103)
        self.assertEqual(state['closed']['stop_status'], 'not_started')

    def test_event_details_cannot_cross_to_a_successor_round(self):
        inspected = self.inspect()
        protocol.publish(self.info['result_path'], self.response(status='blocked', blocked_reason='Fixture question'))
        next_round = self.prepare(cwd=None, parent_depth=None, previous=self.info['request_path'])
        self.change('activate', {'request_path':next_round['request_path']})
        with self.assertRaisesRegex(ValueError, 'identity|old round'):
            self.begin(now=104, inspection=inspected)

    def test_telemetry_outside_exact_dialog_can_redraw_without_changing_operation(self):
        inspected = self.inspect()
        with patch.object(watch, 'observe', return_value={
                'state':'blocked','screen':'Elapsed 4s\n' + self.screen + '\nElapsed 4s'}):
            self.change('control-begin', {'kind':'deny','inspection_path':inspected['inspection_path'],
                'method':{'kind':'keys','description':'Fixture option'},
                'evidence_path':str(self.proof),'note':'Same full captured operation and options'})
        self.assertEqual(self.state['control']['status'], 'uncertain')

    def test_review_evidence_rejects_fifo_and_final_symlink_without_blocking(self):
        fifo = self.root/'evidence.fifo'
        os.mkfifo(fifo)
        link = self.root/'evidence-link'
        link.symlink_to(self.proof)
        for path in (fifo, link):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                reviews.evidence_ref(str(path))

    def test_delayed_receipt_preserves_actual_resolution_time(self):
        self.begin()
        self.receipt(now=150, observed_at=104)
        receipt = self.state['control']['receipt']
        self.assertEqual(receipt['observed_at'], 104)
        self.assertEqual(receipt['recorded_at'], 150)
