"""Retained audit timing is not an approval SLA or native action receipt."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

from test_token_tools import ToolCase
import jobs
import protocol
import reviews
import runs
import supervision_audit as audit
import watch


class AuditTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.run = runs.init_run(self.root)
        self.info = self.prepare(root=None, run=self.run['run_path'])
        self.record()
        self.handle = watch.init_watch([self.info['request_path']])['watch_path']
        self.proof = self.root / 'private-proof.json'
        self.proof.write_text('{"synthetic":true}')

    def attention(self, now=100, command='read fixture', handle=None):
        with patch.object(watch, 'observe', return_value={'state': 'working',
                          'screen': f'Command: {command}\nAllow once / Deny'}):
            result = watch.poll_watch(handle or self.handle, now=now)
        return next(e for e in result['events'] if e['kind'] == 'attention')

    def review(self, event, status='handled', revision=0, now=110, handle=None, **kwargs):
        return reviews.record_review(handle or self.handle, event['seq'], status, 'Private review note',
                                     revision, now=now, **kwargs)

    def report(self, now=200, **kwargs):
        return audit.audit(self.run['run_path'], now=now, **kwargs)

    def test_no_events_is_no_samples_not_a_pass_and_does_not_observe_or_write(self):
        before = self.snapshot()
        with patch.object(watch, 'observe', side_effect=AssertionError('no terminal read')):
            result = self.report()
        self.assertTrue(result['complete'])
        self.assertEqual(result['review_recording_verdict'], 'no_samples')
        self.assertIsNone(result['distributions']['detection_to_first_review_seconds']['p95'])
        self.assertEqual(before, self.snapshot())

    def snapshot(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()}

    def test_first_review_action_record_and_latest_resolution_are_distinct(self):
        event = self.attention()
        self.review(event, status='open', now=105)
        self.review(event, now=150, revision=1, action_evidence=str(self.proof))
        self.review(event, now=180, revision=2, resolution_evidence=str(self.proof), resolved_at=170)
        result = self.report()
        row = result['records'][0]
        self.assertEqual(row['detection_to_first_review_seconds'], 5)
        self.assertEqual(row['detection_to_action_evidence_record_seconds'], 50)
        self.assertEqual(row['detection_to_latest_resolution_seconds'], 70)
        self.assertEqual(result['review_recording_verdict'], 'within_target')
        self.assertIsNone(row['actual_action_at'])
        self.assertFalse(result['approval_response_sla_verified'])
        self.assertTrue(row['resolution_supported'])
        self.assertNotIn('Private review note', json.dumps(result))
        self.assertNotIn(str(self.proof), json.dumps(result))

    def test_handled_without_resolution_remains_unknown(self):
        event = self.attention()
        self.review(event)
        result = self.report()
        self.assertEqual(result['counts']['handled_without_resolution'], 1)
        self.assertIsNone(result['records'][0]['detection_to_latest_resolution_seconds'])
        self.assertEqual(result['records'][0]['age_without_resolution_evidence_seconds'], 100)
        self.assertEqual(result['distributions']['detection_to_latest_resolution_seconds']['samples'], 0)

    def test_waiting_user_intervals_are_declared_time_not_deducted_from_latency(self):
        event = self.attention()
        self.review(event, status='waiting_user', now=140)
        self.review(event, status='waiting_user', now=150, revision=1)
        self.review(event, now=170, revision=2)
        self.review(event, status='waiting_user', now=190, revision=3)
        row = self.report()['records'][0]
        self.assertEqual(row['declared_waiting_user_seconds'], 40)
        self.assertEqual(row['detection_to_first_review_seconds'], 40)
        self.assertEqual(row['review_target_status'], 'late')

    def test_reopen_invalidates_current_resolution_but_keeps_history_count(self):
        event = self.attention()
        self.review(event, now=120, resolved_at=119, resolution_evidence=str(self.proof))
        self.review(event, status='open', now=140, revision=1)
        row = self.report()['records'][0]
        self.assertFalse(row['resolution_supported'])
        self.assertEqual(row['historical_resolution_records'], 1)

    def test_pending_overdue_and_exact_threshold_boundary(self):
        self.attention()
        self.assertEqual(self.report(now=130)['review_recording_verdict'], 'pending')
        self.assertEqual(self.report(now=131)['counts']['overdue_unreviewed'], 1)
        event = reviews.load_event(Path(self.handle).parent, {(self.info['job_id'], self.info['round_id'])}, 2)
        self.review(event, now=130)
        self.assertEqual(self.report()['review_recording_verdict'], 'within_target')

    def test_late_review_is_missed_even_when_resolution_is_supported(self):
        event = self.attention()
        self.review(event, now=150, resolved_at=149, resolution_evidence=str(self.proof))
        result = self.report()
        self.assertEqual(result['review_recording_verdict'], 'missed')
        self.assertEqual(result['counts']['resolution_supported'], 1)

    def test_replacement_watch_and_reappeared_dialog_do_not_inherit_review(self):
        event = self.attention()
        self.review(event)
        replacement = watch.init_watch([self.info['request_path']])['watch_path']
        another = self.attention(now=120, handle=replacement)
        self.assertEqual(event['correlation_key'], another['correlation_key'])
        with patch.object(watch, 'observe', return_value={'state': 'working', 'screen': 'running'}):
            watch.poll_watch(self.handle, now=125)
        fresh = self.attention(now=130)
        self.assertNotEqual(event['issue_id'], fresh['issue_id'])
        result = self.report()
        self.assertEqual(result['counts']['attention_records'], 3)
        self.assertEqual(result['counts']['overdue_unreviewed'], 2)
        self.assertEqual(len({r['event_ref'] for r in result['records']}), 3)

    def test_summary_includes_all_samples_despite_page_limit(self):
        for index in range(5):
            event = self.attention(now=100 + index, command=f'read fixture {index}')
            self.review(event, now=101 + index * 11)
        first = self.report(limit=2)
        last = self.report(limit=2, offset=4)
        self.assertEqual(first['counts']['attention_records'], 5)
        self.assertEqual(first['counts'], last['counts'])
        self.assertEqual(first['distributions'], last['distributions'])
        self.assertEqual(first['distributions']['detection_to_first_review_seconds'],
                         {'samples': 5, 'p50': 21, 'p95': 41, 'max': 41})
        self.assertEqual(len(last['records']), 1)
        self.assertTrue(first['more_records'])
        self.assertFalse(last['more_records'])

    def test_modified_or_missing_resolution_evidence_is_incomplete_not_success(self):
        event = self.attention()
        self.review(event, now=120, resolved_at=119, resolution_evidence=str(self.proof),
                    action_evidence=str(self.proof))
        for remove in (False, True):
            if remove:
                self.proof.unlink()
            else:
                self.proof.write_text('changed')
            result = self.report()
            self.assertFalse(result['complete'])
            self.assertEqual(result['review_recording_verdict'], 'incomplete')
            self.assertFalse(result['records'][0]['resolution_supported'])
            self.assertIsNone(result['records'][0]['first_action_evidence_recorded_at'])
            self.assertEqual(len(result['records'][0]['evidence_issues']), 2)

    def test_missing_or_changed_event_capture_is_not_an_empty_healthy_report(self):
        event = self.attention()
        path = Path(event['evidence_path'])
        path.write_text('{}')
        result = self.report()
        self.assertFalse(result['complete'])
        self.assertEqual(result['counts']['candidates_in_readable_watches'], 1)
        self.assertEqual(result['counts']['attention_records'], 0)
        self.assertEqual(result['review_recording_verdict'], 'incomplete')

    def test_legacy_unpinned_evidence_is_explicit_without_claiming_native_verification(self):
        event = self.attention()
        path = Path(self.handle).parent / 'events' / f"{event['seq']:012d}.json"
        event.pop('evidence_sha256')
        path.write_text(json.dumps(event))
        self.review(event)
        result = self.report()
        self.assertTrue(result['complete'])
        self.assertEqual(result['counts']['legacy_unpinned'], 1)
        self.assertFalse(result['approval_response_sla_verified'])

    def test_bad_clock_is_incomplete_and_never_clamped_to_fast_response(self):
        event = self.attention()
        receipt = self.review(event)
        path = Path(receipt['path'])
        original = protocol.read_json(path)
        for stamp in (99, 201, -1):
            path.write_text(json.dumps({**original, 'recorded_at': stamp}))
            result = self.report()
            self.assertFalse(result['complete'])
            self.assertEqual(result['distributions']['detection_to_first_review_seconds']['samples'], 0)
        path.write_text(json.dumps(original))
        self.review(event, revision=1, now=109)
        self.assertFalse(self.report()['complete'])

    def test_corrupt_review_history_and_gapped_event_history_are_incomplete(self):
        event = self.attention()
        receipt = self.review(event)
        path = Path(receipt['path'])
        original = path.read_bytes()
        path.write_text('{broken')
        self.assertFalse(self.report()['complete'])
        path.write_bytes(original)
        (Path(self.handle).parent / 'events' / '000000000001.json').unlink()
        self.assertFalse(self.report()['complete'])

    def test_deleted_entire_event_history_is_detected_from_observer_cursor(self):
        self.attention()
        for p in (Path(self.handle).parent / 'events').glob('*.json'):
            p.unlink()
        self.assertFalse(self.report()['complete'])

    def test_run_manifest_and_orphan_watch_fail_closed(self):
        self.attention()
        registry = Path(self.run['run_path']).parent / 'watch-registry'
        for p in registry.glob('*.json'):
            p.unlink()
        self.assertFalse(self.report()['complete'])

    def test_current_and_historical_rounds_keep_separate_identity(self):
        first = self.attention()
        self.review(first)
        protocol.publish(self.info['result_path'], self.response(job_id=self.info['job_id'], round_id=self.info['round_id']))
        follow = self.prepare(root=None, cwd=None, parent_depth=None, previous=self.info['request_path'])
        new_handle = watch.init_watch([follow['request_path']])['watch_path']
        self.attention(now=140, handle=new_handle)
        result = self.report()
        self.assertEqual({r['round_id'] for r in result['records']}, {self.info['round_id'], follow['round_id']})
        self.assertEqual(result['counts']['within_target'], 1)
        self.assertEqual(result['counts']['overdue_unreviewed'], 1)

    def test_cli_and_minimal_copied_package_work_without_repository_or_tests(self):
        event = self.attention()
        self.review(event)
        package = self.root / 'minimal' / 'skills'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'skills', package,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        script = package / 'agent-orchestrator/scripts/supervision_audit.py'
        before = self.snapshot()
        result = subprocess.run([sys.executable, '-B', str(script), '--run', self.run['run_path']],
                                cwd=package, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['counts']['within_target'], 1)
        self.assertEqual(before, self.snapshot())
        bad = subprocess.run([sys.executable, '-B', str(script), '--run', self.run['run_path'], '--limit', '0'],
                             capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)

    def test_invalid_options_are_rejected(self):
        for options in ({'threshold': True}, {'threshold': float('nan')}, {'threshold': 0},
                        {'limit': True}, {'offset': -1}, {'now': -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.report(**options)

    def test_non_attention_events_do_not_inflate_approval_record_counts(self):
        with patch.object(watch, 'observe', return_value={'state': 'blocked', 'screen': 'not a dialog'}):
            watch.poll_watch(self.handle, now=100)
        result = self.report()
        self.assertEqual(result['counts']['attention_records'], 0)
        self.assertEqual(result['review_recording_verdict'], 'no_samples')

    def test_same_timing_rules_for_all_hosts_and_opencode_profiles(self):
        for host, profile in [('codex', 'default'), ('omp', 'default'), ('hermes', 'default'),
                              ('opencode', 'omo'), ('opencode', 'pure')]:
            info = self.prepare(root=None, run=self.run['run_path'])
            self.record(request=info['request_path'])
            state = jobs.register(self.run['index_path'], info['request_path'], now=90)
            state = jobs.claim(self.run['index_path'], state['job_id'], state['revision'], 'controller', 300, now=90)
            jobs.change(self.run['index_path'], state['job_id'], state['revision'], state['lease']['token'],
                        'update', {'launch': {'argv': [host], 'profile': profile}}, now=90)
            handle = watch.init_watch([info['request_path']])['watch_path']
            event = self.attention(handle=handle)
            self.review(event, handle=handle, now=135)
        result = self.report()
        self.assertEqual(result['counts']['late'], 5)
        self.assertEqual(result['distributions']['detection_to_first_review_seconds'],
                         {'samples': 5, 'p50': 35, 'p95': 35, 'max': 35})

    def test_cli_missed_pending_and_empty_do_not_exit_success(self):
        script = Path(audit.__file__)
        args = [sys.executable, '-B', str(script), '--run', self.run['run_path']]
        empty = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(empty.returncode, 1)
        self.assertEqual(json.loads(empty.stdout)['review_recording_verdict'], 'no_samples')
        event = self.attention()
        pending = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(pending.returncode, 1)
        self.assertEqual(json.loads(pending.stdout)['counts']['overdue_unreviewed'], 1)
        self.review(event, now=160)
        late = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(late.returncode, 1)
        self.assertEqual(json.loads(late.stdout)['review_recording_verdict'], 'missed')

    def test_concurrent_review_append_is_incomplete_not_a_mixed_success(self):
        event = self.attention()
        self.review(event)
        original = reviews.event_detail
        def append_then_read(*args):
            self.review(event, status='open', revision=1, now=120)
            return original(*args)
        with patch.object(reviews, 'event_detail', side_effect=append_then_read):
            self.assertFalse(self.report()['complete'])

    def test_future_event_is_not_zero_latency_and_private_errors_are_not_echoed(self):
        self.attention(now=210)
        result = self.report()
        self.assertFalse(result['complete'])
        self.assertEqual(result['counts']['attention_records'], 0)
        with patch.object(reviews, 'event_detail', side_effect=ValueError('PRIVATE_SECRET_OPERATION')):
            result = self.report(now=220)
        self.assertNotIn('PRIVATE_SECRET_OPERATION', json.dumps(result))

    def test_corrupt_watch_does_not_hide_other_watch_but_blocks_overall_pass(self):
        event = self.attention()
        self.review(event)
        other = watch.init_watch([self.info['request_path']])['watch_path']
        second = self.attention(handle=other)
        receipt = self.review(second, handle=other)
        Path(receipt['path']).write_text('{broken')
        result = self.report()
        self.assertFalse(result['complete'])
        self.assertEqual(result['counts']['attention_records'], 1)

    def test_earlier_receipt_rewrite_during_read_does_not_escape_latest_revision_check(self):
        event = self.attention()
        first = self.review(event, status='open', now=105)
        self.review(event, revision=1, now=150)
        original = reviews.event_detail
        def rewrite_then_read(*args):
            path = Path(first['path'])
            value = protocol.read_json(path)
            path.write_text(json.dumps({**value, 'recorded_at': 500}))
            return original(*args)
        with patch.object(reviews, 'event_detail', side_effect=rewrite_then_read):
            result = self.report()
        self.assertFalse(result['complete'])
        self.assertEqual(result['counts']['attention_records'], 0)
