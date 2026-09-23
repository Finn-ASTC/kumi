"""Completion needs independently accepted output and a current scoped host check."""

import json
from pathlib import Path
import sys

from test_token_tools import ToolCase
import protocol
import jobs
import delivery
import runs


class CompletionTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.record()
        self.index = self.root / 'jobs'
        self.job = self.info['job_id']
        self.proof = self.root / 'proof.txt'
        self.proof.write_text('Independent answer review and exact host observation')
        self.state = jobs.register(self.index, self.info['request_path'], now=100)
        self.state = jobs.claim(self.index, self.job, 1, 'controller', 300, now=100)
        self.token = self.state['lease']['token']
        self.change('update', {'native': {'session_id': 'native-root', 'turn_id': 'turn-1'}})

    def change(self, action, payload, now=101):
        self.state = jobs.change(self.index, self.job, self.state['revision'], self.token,
                                 action, payload, now=now)
        return self.state

    def publish(self, **kwargs):
        protocol.publish(self.info['result_path'], self.response(**kwargs))

    def acceptance(self, **kwargs):
        return {'kind': 'answer', 'verdict': 'accepted', 'evidence_path': str(self.proof),
                'note': 'Reviewed the requested answer', **kwargs}

    def host(self, **kwargs):
        return {'host': 'hermes', 'host_version': 'fixture-1', 'status': 'settled',
                'disposition': 'reusable', 'observed_at': 101, 'valid_until': 120,
                'checks': {k: 'clear' for k in ('foreground', 'background', 'native_children',
                                               'interaction', 'side_effects')},
                'children': [], 'side_effect_paths': [], 'evidence_path': str(self.proof),
                'note': 'Exact owned target and declared observation scope checked', **kwargs}

    def close(self, now=102):
        return self.change('close', {'outcome': 'completed', 'evidence_path': str(self.proof),
                                     'note': 'Completion conditions checked'}, now)

    def view(self, now=102):
        return jobs.recover(self.index, self.job, now=now)['completion']

    def verified_snapshot(self, info=None, exit_code=0):
        info = info or self.info
        (self.cwd / 'answer.txt').write_text('42\n')
        snapshot = delivery.capture(info['request_path'], ['answer.txt'], [], 'Writer handed off', root=self.root)
        plan = {'commands': [{'argv': [sys.executable, '-c', f'import sys; sys.exit({exit_code})'],
                              'timeout_seconds': 5}], 'artifacts': [], 'dependencies': {}, 'environment': {}}
        return delivery.verify(snapshot['snapshot_path'], plan, self.root)['attempt_path']

    def test_success_without_acceptance_and_host_check_cannot_close(self):
        self.publish()
        with self.assertRaisesRegex(ValueError, 'completion'):
            self.close()

    def test_answer_acceptance_needs_no_snapshot_and_survives_cli_recovery(self):
        self.publish()
        self.change('accept', self.acceptance())
        self.assertFalse(self.view()['host_settled'])
        self.change('host', self.host())
        self.assertTrue(self.view()['ready_to_complete'])
        self.close()
        result = self.run_tool('jobs.py', 'recover', '--index', self.index, '--job', self.job)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout)['state']
        self.assertTrue(state['closed']['completion']['accepted_by_verifier'])
        self.assertTrue(state['closed']['completion']['host_settled'])

    def test_unknown_host_and_rejected_answer_remain_incomplete(self):
        self.publish()
        self.change('accept', self.acceptance(verdict='rejected'))
        self.change('host', self.host(status='unknown', disposition=None))
        view = self.view()
        self.assertTrue(view['response_published'])
        self.assertFalse(view['accepted_by_verifier'])
        self.assertFalse(view['host_settled'])

    def test_answer_receipt_cannot_bypass_declared_file_delivery(self):
        self.publish(files_created=['answer.txt'])
        with self.assertRaisesRegex(ValueError, 'delivery'):
            self.change('accept', self.acceptance())

    def test_failed_delivery_cannot_be_accepted(self):
        self.publish()
        attempt = self.verified_snapshot(exit_code=1)
        with self.assertRaisesRegex(ValueError, 'passed'):
            self.change('accept', self.acceptance(kind='delivery', attempt_path=attempt))

    def test_delivery_bound_to_result_and_readback_detects_changed_logs(self):
        self.publish(files_created=['answer.txt'])
        attempt = self.verified_snapshot()
        self.change('accept', self.acceptance(kind='delivery', attempt_path=attempt))
        self.change('host', self.host())
        self.assertTrue(self.view()['ready_to_complete'])
        (Path(attempt).parent / 'evidence/0001/stdout.log').write_text('rewritten evidence')
        self.assertFalse(self.view()['accepted_by_verifier'])
        with self.assertRaisesRegex(ValueError, 'completion'):
            self.close()

    def test_other_round_delivery_is_not_accepted(self):
        self.publish()
        other = self.prepare()
        protocol.publish(other['result_path'], self.response(job_id=other['job_id'], round_id=other['round_id']))
        attempt = self.verified_snapshot(other)
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.change('accept', self.acceptance(kind='delivery', attempt_path=attempt))

    def test_expired_host_observation_blocks_close(self):
        self.publish()
        self.change('accept', self.acceptance())
        self.change('host', self.host())
        self.assertFalse(self.view(now=120)['host_settled'])
        with self.assertRaisesRegex(ValueError, 'completion'):
            self.close(now=120)

    def test_changed_native_binding_invalidates_previous_host_check(self):
        self.publish()
        self.change('accept', self.acceptance())
        self.change('host', self.host())
        self.change('update', {'native': {'session_id': 'different-session', 'turn_id': 'turn-1'}})
        self.assertFalse(self.view()['host_settled'])

    def test_settled_cannot_hide_unknown_checks_or_active_native_child(self):
        self.publish()
        unknown = self.host()
        unknown['checks']['background'] = 'unknown'
        for payload in (unknown, self.host(children=[{'session_id': 'child', 'parent_session_id': 'native-root',
                                                       'status': 'active'}])):
            with self.assertRaisesRegex(ValueError, 'settled'):
                self.change('host', payload)

    def test_host_observation_cannot_come_from_future_or_without_native_identity(self):
        self.publish()
        with self.assertRaisesRegex(ValueError, 'time'):
            self.change('host', self.host(observed_at=102))
        self.change('update', {'native': {'session_id': None, 'turn_id': None}})
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.change('host', self.host())

    def test_native_lineage_must_reach_the_recorded_root(self):
        self.publish()
        for children in ([{'session_id': 'child', 'parent_session_id': 'unrelated-root', 'status': 'settled'}],
                         [{'session_id': 'a', 'parent_session_id': 'b', 'status': 'settled'},
                          {'session_id': 'b', 'parent_session_id': 'a', 'status': 'settled'}]):
            with self.assertRaisesRegex(ValueError, 'lineage'):
                self.change('host', self.host(children=children))

    def test_check_completion_cli_reports_missing_conditions_without_lease(self):
        self.publish()
        result = self.run_tool('jobs.py', 'check-completion', '--index', self.index, '--job', self.job)
        self.assertEqual(result.returncode, 1, result.stderr)
        view = json.loads(result.stdout)
        self.assertTrue(view['response_published'])
        self.assertFalse(view['ready_to_complete'])
        self.assertTrue(view['reasons'])

    def test_changed_evidence_or_result_revokes_acceptance(self):
        self.publish()
        self.change('accept', self.acceptance())
        self.change('host', self.host())
        self.proof.write_text('different controller evidence')
        self.assertFalse(self.view()['ready_to_complete'])
        self.change('accept', self.acceptance())
        path = Path(self.info['result_path'])
        value = json.loads(path.read_text())
        value['output'] = 'replacement answer'
        path.write_text(json.dumps(value))
        self.assertFalse(self.view()['accepted_by_verifier'])

    def test_new_activity_revokes_previously_settled_host(self):
        self.publish()
        self.change('accept', self.acceptance())
        self.change('host', self.host())
        self.change('host', self.host(status='waiting', disposition=None), now=102)
        self.assertFalse(self.view()['host_settled'])
        with self.assertRaisesRegex(ValueError, 'completion'):
            self.close()

    def test_followup_does_not_inherit_acceptance_or_host_settling(self):
        self.publish()
        self.change('accept', self.acceptance())
        self.change('host', self.host())
        follow = self.prepare(cwd=None, parent_depth=None, previous=self.info['request_path'])
        self.change('activate', {'request_path': follow['request_path']})
        view = self.view()
        self.assertFalse(view['response_published'])
        self.assertFalse(view['accepted_by_verifier'])
        self.assertFalse(view['host_settled'])

    def test_legacy_state_loads_with_unknown_completion(self):
        # Legacy on-disk v1 histories did not carry lifecycle facts.
        path = sorted((self.index / self.job).glob('*.json'))[-1]
        value = json.loads(path.read_text())
        value.pop('completion', None)
        path.write_text(json.dumps(value))
        self.assertFalse(self.view()['ready_to_complete'])

    def test_failed_and_cancelled_outcomes_do_not_claim_verification(self):
        from control_fixtures import stopped
        self.state = stopped(self.index, self.state, self.token, self.proof, 101)
        self.change('close', {'outcome': 'cancelled', 'evidence_path': str(self.proof), 'note': 'No input sent'})
        self.assertFalse(self.view()['accepted_by_verifier'])

    def test_run_recovery_includes_completion_conditions(self):
        info = self.prepare(root=None, temporary=True)
        import shutil
        self.addCleanup(shutil.rmtree, Path(info['run_path']).parent)
        jobs.register(info['index_path'], info['request_path'], now=100)
        protocol.publish(info['result_path'], self.response(job_id=info['job_id'], round_id=info['round_id']))
        job = runs.recover(info['run_path'], now=102)['jobs'][0]
        self.assertTrue(job['completion']['response_published'])
        self.assertFalse(job['completion']['ready_to_complete'])
