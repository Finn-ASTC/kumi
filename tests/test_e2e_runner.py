"""The live runner composes real bookkeeping; only external terminal I/O is faked."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_token_tools import SCRIPTS
import protocol
import jobs
import runs
import watch

try:
    import e2e_runner as runner
except ModuleNotFoundError:
    runner = None


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(runner, 'reusable E2E runner is missing')
        host_lookup = patch.object(runner.shutil,'which',side_effect=lambda kind:'/fixture/bin/'+kind)
        host_lookup.start()
        self.addCleanup(host_lookup.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='orch-runner-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seed = self.root / 'seed'
        self.seed.mkdir()
        (self.seed / 'value.txt').write_text('before')
        self.proof = self.root / 'inspection.json'
        self.proof.write_text('{"reviewed":"fixture only"}')
        packet = {'objective':'Implement exact decimal totals', 'scope':'Owned project only',
                  'acceptance':['Independent verifier passes']}
        self.spec = {'version':1, 'targets':[{'name':'author', 'label':'实现金额汇总', 'kind':'omp',
            'seed':str(self.seed), 'task_packet':packet,
            'scope':{'include':['.'], 'exclude':['__pycache__']},
            'verification_plan':{'commands':[{'argv':[sys.executable,'-c',
                "from pathlib import Path; assert Path('value.txt').read_text() == 'after'"],
                'timeout_seconds':3}], 'artifacts':[], 'dependencies':{}, 'environment':{}}}]}
        self.path = runner.initialize(self.root, self.spec)['runner_path']
        self.session = runner.Runner(self.path)
        self.session.__enter__()
        self.addCleanup(self.session.__exit__, None, None, None)

    def preflight(self):
        return {'host_version':'fixture-only', 'evidence_path':str(self.proof), 'note':'Synthetic readiness',
                'checks':{'model_and_approvals_preserved':True, 'runtime_dependencies_verified':True}}

    def fake_execute(self, argv, **kwargs):
        proof = self.session.evidence('transport', {'argv':argv})
        if 'workspace' in argv and 'create' in argv:
            value = {'workspace':{'workspace_id':'wZ'}, 'tab':{'tab_id':'wZ:tA'},
                     'root_pane':{'pane_id':'wZ:pB'}}
        elif 'tab' in argv and 'create' in argv:
            value = {'tab':{'tab_id':'w1:tA'}, 'root_pane':{'pane_id':'w1:pB'}}
        else:
            value = {}
        return {'exit_code':0, 'stdout':json.dumps({'result':value}), 'evidence_path':proof}

    def start(self, **kwargs):
        with patch.object(self.session, 'execute', side_effect=self.fake_execute):
            return self.session.start('author', self.preflight(), session='fixture', **kwargs)

    def ready(self):
        target = self.session.target('author')
        return {'request_path':target['request_path'], 'resources':protocol.read_json(
            Path(target['request_path']).parent/'resources.json'), 'ready':True,
            'observed_at':time.time(), 'evidence_path':str(self.proof), 'note':'Checked exact UI and input readiness'}

    def enable_monitor(self):
        handle = self.session.watch_init()['watch_path']
        now = time.time()
        self.session.monitor('author', {'monitor':{'owner':'fixture-controller','watch_path':handle,
            'expires_at':now+300}, 'evidence_path':str(self.proof),'note':'Fixture observer explicitly modelled'})
        with patch.object(watch,'observe',return_value={'state':'working','screen':'Fixture ready'}):
            watch.poll_watch(handle)
        watch.atomic_save(Path(handle).with_name('observer.json'), {
            'version':1,'watch_id':protocol.read_json(handle)['watch_id'], 'instance_id':'a'*32,'pid':123,
            'started_at':now,'expires_at':now+300,'stopped_at':None,'reason':None})
        active = patch.object(watch,'observer_active',return_value=True)
        active.start()
        self.addCleanup(active.stop)

    def publish(self, status='success'):
        target = self.session.target('author')
        request = protocol.load_request(target['request_path'])
        response = {k:request[k] for k in protocol.IDENTITY_FIELDS}
        response.update(status=status, output='fixture response', error=None,
            blocked_reason='Which format?' if status=='blocked' else None, completed_at=protocol.utc_now(),
            files_created=[], files_modified=['value.txt'] if status=='success' else [],
            files_generated=[], files_deleted=[])
        protocol.publish(request['result_path'], response)

    def test_initialization_copies_seed_and_captures_baseline_before_any_launch(self):
        target = self.session.target('author')
        self.assertEqual((Path(target['cwd'])/'value.txt').read_text(), 'before')
        self.assertNotEqual(Path(target['cwd']), self.seed)
        self.assertTrue(Path(target['baseline']).is_file())
        recovery = self.session.recover()
        self.assertEqual(recovery['run']['jobs'][0]['submission'], 'prepared')
        self.assertEqual(target['phase'], 'prepared')

    def test_unknown_spec_and_overlapping_target_names_fail_before_allocating(self):
        before = set(self.root.iterdir())
        for spec in ({**self.spec,'autoapprove':True},
                     {**self.spec,'targets':self.spec['targets']*2}):
            with self.assertRaises(ValueError):
                runner.initialize(self.root, spec)
        self.assertEqual(set(self.root.iterdir()), before)

    def test_workspace_and_tab_creation_use_task_label_and_no_focus(self):
        calls = []
        def execute(argv, **kwargs):
            calls.append(argv)
            return self.fake_execute(argv, **kwargs)
        with patch.object(self.session, 'execute', side_effect=execute):
            self.session.start('author', self.preflight(), session='fixture', parent_pane='w1:p1')
        creation = next(a for a in calls if 'workspace' in a and 'create' in a)
        self.assertIn('--no-focus', creation)
        self.assertEqual(creation[creation.index('--label')+1], '实现金额汇总')
        self.assertTrue(any('rename' in a and '实现金额汇总' in a for a in calls))
        record = self.ready()['resources']
        self.assertFalse(record['owns_session'])
        self.assertTrue(record['owns_workspace'])

    def test_start_failure_cannot_silently_allocate_again(self):
        with patch.object(self.session, 'execute', side_effect=OSError('lost creation reply')):
            with self.assertRaises(OSError):
                self.session.start('author', self.preflight(), session='fixture')
        with patch.object(self.session, 'execute') as execute, self.assertRaisesRegex(ValueError,'reconcile'):
            self.session.start('author', self.preflight(), session='fixture')
        execute.assert_not_called()
        self.assertEqual(self.session.target('author')['phase'], 'start_uncertain')

    def test_send_commits_uncertainty_before_io_and_never_retries_timeout(self):
        self.start()
        self.enable_monitor()
        sends = []
        def timeout(argv, **kwargs):
            target = self.session.target('author')
            state = jobs.load(Path(self.session.data['index_path']), target['job_id'])
            self.assertEqual(state['submission'], 'uncertain')
            sends.append(argv)
            return {'exit_code':None, 'stdout':'', 'evidence_path':self.session.evidence('timeout', {})}
        with patch.object(watch, 'observe', return_value={'state':'idle','screen':'ready'}), \
                patch.object(self.session, 'execute', side_effect=timeout):
            self.session.submit('author', self.ready())
            with self.assertRaisesRegex(ValueError, 'uncertain|accepted'):
                self.session.submit('author', self.ready())
        self.assertEqual(len(sends), 1)
        self.assertEqual(self.session.recover()['run']['jobs'][0]['submission'], 'uncertain')

    def test_submit_requires_live_supervision_before_input(self):
        self.start()
        with patch.object(watch, 'observe', return_value={'state':'idle','screen':'ready'}), \
                patch.object(self.session, 'execute', side_effect=self.fake_execute) as execute:
            with self.assertRaisesRegex(ValueError, 'supervision'):
                self.session.submit('author', self.ready())
            execute.assert_not_called()
        state = jobs.load(Path(self.session.data['index_path']), self.session.target('author')['job_id'])
        self.assertEqual(state['submission'], 'prepared')
        self.assertEqual(state['attempts'], [])

    def test_permission_or_stale_readiness_never_sends(self):
        self.start()
        for stale in (False, True):
            receipt = self.ready()
            if stale:
                receipt['observed_at'] -= 60
            with patch.object(watch, 'observe', return_value={
                    'state':'idle', 'screen':'Command: inspect\nAllow once / Deny'}), \
                    patch.object(self.session,'execute') as execute, self.assertRaises(ValueError):
                self.session.submit('author', receipt)
            execute.assert_not_called()

    def test_followup_keeps_job_and_old_waiting_user(self):
        self.start()
        handle = self.session.watch_init()['watch_path']
        with patch.object(watch,'observe',return_value={'state':'working','screen':'Allow once / Deny'}):
            event = next(e for e in self.session.poll()['events'] if e['kind']=='attention')
        import reviews
        reviews.record_review(handle,event['seq'],'waiting_user','Question already asked',0)
        self.publish('blocked')
        before = self.session.target('author').copy()
        self.session.follow('author', self.spec['targets'][0]['task_packet'], 'Writer yielded for next round')
        after = self.session.target('author')
        self.assertEqual(after['job_id'],before['job_id'])
        self.assertNotEqual(after['request_path'],before['request_path'])
        self.assertTrue(Path(before['request_path']).with_name('result.json').exists())
        self.assertEqual(self.session.recover()['run']['counts']['waiting_user'],1)

    def test_verification_fails_then_new_round_passes_and_host_gate_remains(self):
        self.start()
        self.publish()
        rejected = self.session.verify('author','Writer yielded')
        self.assertFalse(rejected['passed'])
        with self.assertRaisesRegex(ValueError,'completion|acceptance'):
            self.session.close('author',str(self.proof),'Done')
        self.session.follow('author',self.spec['targets'][0]['task_packet'],'Writer yielded')
        (Path(self.session.target('author')['cwd'])/'value.txt').write_text('after')
        self.publish()
        accepted = self.session.verify('author','Writer yielded')
        self.assertTrue(accepted['passed'])
        self.assertNotEqual(accepted['attempt_path'],rejected['attempt_path'])
        with self.assertRaisesRegex(ValueError,'host'):
            self.session.close('author',str(self.proof),'Done')

    def test_host_preflight_is_required_before_resource_allocation(self):
        receipt = self.preflight()
        receipt['checks']['runtime_dependencies_verified'] = False
        with patch.object(self.session,'execute') as execute, self.assertRaises(ValueError):
            self.session.start('author',receipt,session='fixture')
        execute.assert_not_called()

    def test_second_controller_cannot_enter_same_runner(self):
        with self.assertRaises(ValueError):
            with runner.Runner(self.path):
                self.fail('second controller obtained runner lock')

    def test_tab_creation_uses_explicit_parent_and_records_tab_ownership(self):
        calls = []
        def execute(argv, **kwargs):
            calls.append(argv)
            return self.fake_execute(argv, **kwargs)
        with patch.object(self.session,'execute',side_effect=execute):
            self.session.start('author',self.preflight(),session='fixture',layout='tab',
                               parent_pane='w1:p1',parent_tab='w1:t1')
        creation = next(a for a in calls if 'tab' in a and 'create' in a)
        self.assertEqual(creation[creation.index('--workspace')+1],'w1')
        self.assertIn('--no-focus',creation)
        resources = self.ready()['resources']
        self.assertTrue(resources['owns_tab'])
        self.assertFalse(resources['owns_workspace'])

    def test_native_profiles_and_hermes_environment_are_not_silently_changed(self):
        target = self.session.target('author')
        with patch.object(runner.shutil,'which',side_effect=lambda kind:'/usr/bin/'+kind):
            for kind in ('omp','codex','opencode'):
                target['kind'] = kind
                argv, env = self.session.launch(target,self.preflight())
                self.assertEqual(argv[0],'/usr/bin/'+kind)
                self.assertEqual(env,{})
                self.assertNotIn('--pure',argv)
                self.assertFalse(any('approval' in a for a in argv))
            target['opencode_mode'] = 'pure'
            self.assertEqual(self.session.launch(target,self.preflight())[0],['/usr/bin/opencode','--pure'])
            target['opencode_mode'] = None
            target['kind'] = 'hermes'
            home = self.session.root/'profiles'/'worker'
            (home/'bin').mkdir(parents=True)
            (home/'config.yaml').write_text('# fake configuration')
            scanner = home/'bin/tirith'
            scanner.write_text('# fixture scanner')
            scanner.chmod(0o700)
            payload = self.preflight()
            payload.update(hermes_home=str(home),scanner_sha256=runner.delivery.file_record(scanner)['sha256'])
            payload['checks'].update(private_home_verified=True,automatic_maintenance_disabled=True)
            calls=[]
            def execute(argv, **kwargs):
                calls.append((argv,kwargs))
                if 'config' in argv:
                    return {'exit_code':0,'stdout':str(home/'config.yaml'),'evidence_path':str(self.proof)}
                return self.fake_execute(argv,**kwargs)
            with patch.object(self.session,'execute',side_effect=execute):
                self.session.start('author',payload,session='fixture')
            create = next(a for a,k in calls if 'create' in a)
            self.assertIn('HERMES_HOME='+str(home),create)
            self.assertEqual(calls[0][1]['env'],{'HERMES_HOME':str(home)})

    def test_round_activation_crash_can_reconcile_without_preparing_or_sending_again(self):
        self.start()
        self.publish('blocked')
        save = self.session.save
        def crash_after_activation():
            target = self.session.target('author')
            if 'candidate_round' not in target and target['round_id'] != original_round:
                raise OSError('simulated controller crash')
            save()
        original_round = self.session.target('author')['round_id']
        with patch.object(self.session,'save',side_effect=crash_after_activation), self.assertRaises(OSError):
            self.session.follow('author',self.spec['targets'][0]['task_packet'],'Writer yielded')
        self.session.data = protocol.read_json(self.path)
        with patch.object(protocol,'prepare',side_effect=AssertionError('must not prepare again')):
            self.session.reconcile_round('author')
        target = self.session.target('author')
        state = jobs.load(Path(self.session.data['index_path']),target['job_id'])
        self.assertEqual(state['active_request'],target['request_path'])
        self.assertEqual(len(state['rounds']),2)
        self.assertNotIn('candidate_round',target)

    def test_startup_reconciliation_rejects_a_foreign_session_before_recording(self):
        with patch.object(self.session,'execute',side_effect=OSError('lost reply')):
            with self.assertRaises(OSError):
                self.session.start('author',self.preflight(),session='expected-session')
        payload = {'resources':{'mode':'isolated','session':'foreign-session',
            'agent':self.session.target('author')['agent'],'pane':'wZ:pB','workspace':'wZ',
            'owns_agent':True,'owns_pane':True,'owns_workspace':True,'owns_session':False},
            'evidence_path':str(self.proof),'note':'Synthetic wrong-session proof'}
        with self.assertRaisesRegex(ValueError,'startup intent'):
            self.session.bind('author',payload)
        self.assertFalse(Path(self.session.target('author')['request_path']).with_name('resources.json').exists())

    def test_inspect_keeps_readiness_false_and_records_exact_target(self):
        self.start()
        with patch.object(watch,'observe',return_value={'state':'idle','screen':'ready'}):
            inspected = self.session.inspect('author')
        self.assertFalse(inspected['ready'])
        self.assertEqual(inspected['resources'],self.ready()['resources'])
        self.assertTrue(Path(inspected['evidence_path']).is_file())

    def test_explicit_claim_preserves_uncertain_attempt_without_input(self):
        self.start()
        self.enable_monitor()
        with patch.object(watch,'observe',return_value={'state':'idle','screen':'ready'}), \
                patch.object(self.session,'execute',side_effect=self.fake_execute):
            self.session.submit('author',self.ready())
        target = self.session.target('author')
        released = self.session.mutate('author','release',{})
        jobs.claim(self.session.data['index_path'],target['job_id'],released['revision'],
                   'former-controller',1,now=time.time()-2)
        with patch.object(self.session,'execute',side_effect=AssertionError('claim must not send')):
            acquired = self.session.claim('author',{'evidence_path':str(self.proof),
                'note':'Former fixture controller is stopped'})
        self.assertEqual(acquired['submission'],'uncertain')
        self.assertFalse(acquired['submitted'])

    def test_invalid_parent_ids_are_rejected_before_allocating(self):
        with patch.object(self.session,'execute') as execute, self.assertRaises(ValueError):
            self.session.start('author',self.preflight(),session='fixture',parent_pane='focused')
        execute.assert_not_called()

    def test_startup_reconciliation_adopts_live_tmux_without_relaunch(self):
        with patch.object(self.session,'execute',side_effect=OSError('lost reply')):
            with self.assertRaises(OSError):
                self.session.start('author',self.preflight(),transport='tmux')
        target = self.session.target('author')
        resources = {'mode':'tmux','session':target['agent'],'agent':target['agent'],'pane':'%7',
            'tmux_selector':['-S',str(self.session.root/'tmux.sock')],
            'owns_agent':True,'owns_pane':True,'owns_workspace':False,'owns_session':True}
        with patch.object(watch,'observe',return_value={'state':'unknown','screen':'ready'}), \
                patch.object(self.session,'execute',side_effect=AssertionError('must not relaunch')):
            self.session.bind('author',{'resources':resources,'evidence_path':str(self.proof),
                                       'note':'Exact owned live fixture reconciled'})
        recorded = self.ready()['resources']
        self.assertEqual(recorded['tmux_selector'],resources['tmux_selector'])
        self.assertEqual(self.session.target('author')['phase'],'started')

    def test_startup_reconciliation_keeps_existing_record_unchanged(self):
        def execute(argv, **kwargs):
            if 'start' in argv:
                raise OSError('lost host start reply')
            return self.fake_execute(argv,**kwargs)
        with patch.object(self.session,'execute',side_effect=execute), self.assertRaises(OSError):
            self.session.start('author',self.preflight(),session='fixture')
        recorded = self.ready()['resources']
        with patch.object(watch,'observe',return_value={'state':'idle','screen':'ready'}):
            self.session.bind('author',{'resources':recorded,'evidence_path':str(self.proof),
                                       'note':'Existing allocation and live host reconciled'})
        self.assertEqual(self.ready()['resources'],recorded)

    def test_long_task_name_still_has_valid_internal_agent_identity(self):
        self.spec['targets'][0]['name'] = 'a'*40
        manifest = runner.initialize(self.root,self.spec)['runner_path']
        with runner.Runner(manifest) as controller:
            with patch.object(controller,'execute',side_effect=self.fake_execute):
                controller.start('a'*40,self.preflight(),session='fixture')
            self.assertLessEqual(len(controller.target('a'*40)['agent']),32)

    def test_seed_cannot_contain_test_parent(self):
        nested = self.seed/'test-root'
        nested.mkdir()
        # Prevent actual recursive copying even before the guard exists.
        with patch.object(runner.shutil,'copytree',side_effect=AssertionError('recursive copy attempted')):
            with self.assertRaisesRegex(ValueError,'seed'):
                runner.initialize(nested,self.spec)
        self.assertEqual(list(nested.iterdir()),[])

    def test_monitor_is_explicit_expires_and_must_cover_current_round(self):
        self.start()
        handle = self.session.watch_init()
        target = self.session.target('author')
        def current():
            return jobs.load(Path(self.session.data['index_path']),target['job_id'])
        self.assertIsNone(current()['monitor']['owner'])
        payload = {'monitor':{'owner':'fixture-controller','watch_path':handle['watch_path'],
            'expires_at':time.time()+60},'evidence_path':str(self.proof),
            'note':'Managed polling handle inspected'}
        with patch.object(self.session,'execute',side_effect=AssertionError('receipt must not launch')):
            self.session.monitor('author',payload)
        self.assertEqual(current()['monitor']['owner'],'fixture-controller')
        self.publish('blocked')
        self.session.follow('author',self.spec['targets'][0]['task_packet'],'Writer yielded')
        self.assertIsNone(current()['monitor']['owner'])
        with self.assertRaisesRegex(ValueError,'watch'):
            self.session.monitor('author',payload)

    def test_declared_external_changes_are_reported_without_rewriting_original_snapshot(self):
        self.spec['observe_paths'] = [str(self.seed)]
        manifest = runner.initialize(self.root,self.spec)['runner_path']
        with runner.Runner(manifest) as controller:
            before = controller.data['before']
            (self.seed/'value.txt').write_text('changed by external fixture')
            report = controller.recover()['side_effects']
            self.assertEqual(report['changed_paths'],[str(self.seed)])
            self.assertEqual(report['before'],before)
            self.assertNotEqual(report['before'],report['after'])


if __name__ == '__main__':
    unittest.main()
