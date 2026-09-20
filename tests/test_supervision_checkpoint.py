"""Work budgets require live coverage, not a monitor declaration or a done UI."""
import contextlib
import io
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import test_e2e_runner as fixture
import jobs
import protocol
import reviews
import watch
try:
    import supervision
except ModuleNotFoundError:
    supervision = None


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(supervision, 'supervision checkpoint is missing')
        self.fixture = fixture.RunnerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.start()
        self.runner = self.fixture.session
        self.target = self.runner.target('author')
        self.path = self.runner.data['run_path']
        self.handle = self.runner.watch_init()['watch_path']
        self.now = time.time()
        self.monitor()
        with patch.object(watch, 'observe', return_value={'state':'working','screen':'Compiling'}):
            watch.poll_watch(self.handle, now=self.now)
        self.runtime()
        active = patch.object(watch, 'observer_active', return_value=True)
        active.start()
        self.addCleanup(active.stop)

    def monitor(self, seconds=300):
        self.runner.monitor('author', {'monitor':{'owner':'test-controller','watch_path':self.handle,
            'expires_at':self.now+seconds}, 'evidence_path':str(self.fixture.proof), 'note':'Test receipt'})

    def runtime(self, seconds=300):
        config = protocol.read_json(self.handle)
        watch.atomic_save(Path(self.handle).with_name('observer.json'), {
            'version':1, 'watch_id':config['watch_id'], 'instance_id':'a'*32, 'pid':123,
            'started_at':self.now-1, 'expires_at':self.now+seconds, 'stopped_at':None, 'reason':None})

    def check(self, **kw):
        return supervision.check(self.path, now=self.now, **kw)

    def test_healthy_coverage_bounds_independent_work_and_sends_nothing(self):
        with patch.object(watch, 'observe', side_effect=AssertionError('checkpoint must not probe')):
            out = self.check(work_seconds=60)
        self.assertTrue(out['can_work'])
        self.assertLessEqual(out['work_budget_seconds'], 30)
        self.assertFalse(out['executes_commands'])

    def test_actual_observer_expiry_overrides_long_monitor_receipt(self):
        self.runtime(seconds=20)
        out = self.check()
        self.assertFalse(out['can_work'])
        self.assertIn('observer_expiring', out['coverage'][0]['issues'])
        self.assertEqual(out['work_budget_seconds'], 0)

    def test_default_handoff_margin_covers_more_than_one_short_tool_wait(self):
        self.runtime(seconds=55)
        out = self.check()
        self.assertFalse(out['can_work'])
        self.assertIn('observer_expiring', out['coverage'][0]['issues'])
        self.assertEqual(out['coverage'][0]['renew_by'], self.now - 5)

    def test_budget_ends_at_explicit_renewal_boundary(self):
        self.runtime(seconds=70)
        out = self.check(renew_before=60)
        self.assertEqual(out['work_budget_seconds'], 10)
        self.assertEqual(out['coverage'][0]['renew_by'], self.now + 10)

    def test_live_lock_cannot_hide_stale_or_failed_target_reads(self):
        for extra in ({'last_successful_check_at':self.now-75}, {'error':'transport failed'}):
            with self.subTest(extra=extra):
                p=Path(self.handle).with_name('state.json');state=protocol.read_json(p)
                state['targets'][self.target['round_id']].update(last_successful_check_at=self.now,error=None)
                state['targets'][self.target['round_id']].update(extra)
                watch.atomic_save(p,state)
                self.assertFalse(self.check()['can_work'])

    def test_declared_owner_and_expiry_are_required_even_when_observer_is_alive(self):
        for change in ({'owner':None},{'expires_at':None},{'expires_at':self.now-1}):
            self.monitor()
            state=jobs.load(Path(self.runner.data['index_path']),self.target['job_id'])
            self.runner.mutate('author','update',{'monitor':{**state['monitor'],**change}})
            self.assertFalse(self.check()['can_work'])

    def test_changed_resources_cannot_borrow_a_fresh_pre_submission_heartbeat(self):
        path=Path(self.target['request_path']).with_name('resources.json')
        data=protocol.read_json(path)
        data.update(pane='wQ:p1',workspace='wQ',tab='wQ:t1')
        path.write_text(json.dumps(data))
        self.assertFalse(self.check()['can_work'])

    def test_same_watch_can_be_restarted_without_losing_waiting_records(self):
        with patch.object(watch,'observe',return_value={'state':'working','screen':'Command: inspect\nAllow once / Deny'}):
            event=next(e for e in watch.poll_watch(self.handle,now=self.now)['events'] if e['kind']=='attention')
        reviews.record_review(self.handle,event['seq'],'waiting_user','Already asked',0,now=self.now)
        self.runtime(seconds=-1)
        self.assertFalse(self.check()['can_work'])
        self.runtime(seconds=300)
        self.monitor()
        self.assertTrue(self.check()['can_work'])
        self.assertEqual(self.check()['counts']['waiting_user'],1)

    def test_stopped_or_legacy_observer_never_silently_grants_a_work_budget(self):
        with patch.object(watch, 'observer_active', return_value=False):
            self.assertIn('observer_stopped',self.check()['coverage'][0]['issues'])
        Path(self.handle).with_name('observer.json').unlink()
        self.assertIn('observer_lifetime_unknown', self.check()['coverage'][0]['issues'])

    def test_new_round_cannot_borrow_old_watch_coverage(self):
        self.fixture.publish('blocked')
        self.runner.follow('author',self.fixture.spec['targets'][0]['task_packet'],'Writer yielded')
        out=self.check()
        self.assertFalse(out['can_work'])
        self.assertIn('monitor_missing',out['coverage'][0]['issues'])

    def test_pending_dialog_blocks_work_but_waiting_user_preserves_parallel_work(self):
        with patch.object(watch,'observe',return_value={'state':'working','screen':'Command: inspect\nAllow once / Deny'}):
            event=next(e for e in watch.poll_watch(self.handle,now=self.now)['events'] if e['kind']=='attention')
        out=self.check()
        self.assertFalse(out['can_work'])
        self.assertEqual(out['action_required'][0]['event_ref'],f"{protocol.read_json(self.handle)['watch_id']}:{event['seq']}")
        reviews.record_review(self.handle,event['seq'],'waiting_user','Already asked; continue sibling work',0,now=self.now)
        out=self.check()
        self.assertTrue(out['can_work'])
        self.assertEqual(out['counts']['waiting_user'],1)
        self.assertEqual(out['waiting_user'][0]['note'],'Already asked; continue sibling work')

    def test_success_and_done_still_require_supervision_until_explicit_close(self):
        self.fixture.publish()
        with patch.object(watch,'observer_active',return_value=False):
            out=self.check()
        self.assertEqual(len(out['coverage']),1)
        self.assertFalse(out['can_work'])

    def test_unknown_or_dead_ui_requires_manual_supervision_despite_successful_reads(self):
        for state in ('unknown','dead'):
            with patch.object(watch,'observe',return_value={'state':state,'screen':'unrecognized host'}):
                watch.poll_watch(self.handle,now=self.now)
            out=self.check()
            self.assertFalse(out['can_work'])
            self.assertIn('observation_'+state,out['coverage'][0]['issues'])

    def test_no_hook_input_requires_fresh_nonempty_ui_review_without_granting_work(self):
        with patch.object(watch,'observe',return_value={'state':'unknown','screen':''}):
            watch.poll_watch(self.handle,now=self.now)
            with patch.object(self.runner,'execute') as execute, self.assertRaisesRegex(ValueError,'reconciliation'):
                self.runner.submit('author',self.fixture.ready())
            execute.assert_not_called()
        with patch.object(watch,'observe',return_value={'state':'unknown','screen':'Known host input prompt'}), \
                patch.object(self.runner,'execute',side_effect=self.fixture.fake_execute):
            self.runner.submit('author',self.fixture.ready())
        self.assertFalse(self.check()['can_work'])

    def test_partial_recovery_and_future_heartbeat_are_not_healthy(self):
        p=Path(self.handle).with_name('state.json');state=protocol.read_json(p)
        state['targets'][self.target['round_id']]['last_successful_check_at']=self.now+10
        watch.atomic_save(p,state)
        self.assertFalse(self.check()['can_work'])
        p.write_text('broken')
        out=self.check()
        self.assertFalse(out['complete'])
        self.assertFalse(out['can_work'])

    def test_run_observer_persists_expiry_and_exit_even_on_failure(self):
        for failure in (False,True):
            with self.subTest(failure=failure):
                response={'cursor':1,'events':[{'kind':'fixture'}]}
                with patch.object(watch,'sweep',side_effect=RuntimeError('read failed') if failure else None,
                                  return_value=response), contextlib.redirect_stdout(io.StringIO()):
                    if failure:
                        with self.assertRaises(RuntimeError):watch.run_observer(self.handle,1,20,True)
                    else:watch.run_observer(self.handle,1,20,True)
                state=protocol.read_json(Path(self.handle).with_name('observer.json'))
                self.assertIsNotNone(state['stopped_at'])
                self.assertEqual(state['reason'],'failed' if failure else 'stop_on_event')
                self.assertAlmostEqual(state['expires_at']-state['started_at'],20)


if __name__ == '__main__':
    unittest.main()
