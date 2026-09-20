"""Real private tmux allocation survives controller death without relaunching."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import e2e_runner as runner
from e2e_runner import jobs, protocol


# Run in a separate controller process. The fixture starts for real; only the
# controller's continuation is killed at the chosen persistence boundary.
FAULT_WORKER = r'''
import json, os, sys
from pathlib import Path
from e2e_runner import Runner, protocol
path, point, proof = sys.argv[1:]
with Runner(path) as r:
    execute, record = r.execute, r.record
    def interrupted_execute(argv, **kwargs):
        receipt = execute(argv, **kwargs)
        if 'new-session' in argv and receipt['exit_code'] == 0:
            protocol.publish(r.root/'actual-start-receipt.json', receipt)
            if point == 'reply':
                os._exit(91)
        return receipt
    def interrupted_record(name, resources):
        record(name, resources)
        if point == 'record':
            os._exit(92)
    r.execute, r.record = interrupted_execute, interrupted_record
    r.start('author', {'host_version':'fixture-only', 'evidence_path':proof,
        'note':'Explicit deterministic fixture; no model or credentials',
        'checks':{'model_and_approvals_preserved':True,
                  'runtime_dependencies_verified':True}}, transport='tmux')
raise AssertionError('fault boundary was not reached')
'''


@unittest.skipUnless(os.environ.get('ORCH_RUN_TMUX_TESTS') == '1', 'opt-in private tmux socket')
@unittest.skipUnless(shutil.which('tmux'), 'tmux is not installed')
class StartupRecoveryTests(unittest.TestCase):
    def exercise_fault(self, point: str) -> None:
        """Recover in a fresh controller and retain exact live pane/process identity."""
        with tempfile.TemporaryDirectory(prefix='orch-start-') as directory:
            root = Path(directory)
            seed = root/'seed'
            seed.mkdir()
            (seed/'value.txt').write_text('unchanged')
            proof = root/'preflight.json'
            proof.write_text('{"fixture":true}')
            preflight = {'host_version':'fixture-only', 'evidence_path':str(proof),
                         'note':'Deterministic fixture', 'checks':{
                             'model_and_approvals_preserved':True,
                             'runtime_dependencies_verified':True}}
            spec = {'version':1, 'targets':[{
                'name':'author', 'label':'验证启动中断恢复', 'kind':'omp', 'seed':str(seed),
                'task_packet':{'objective':'Never submitted in this startup test',
                               'scope':'Owned fixture', 'acceptance':['No duplicate launch']},
                'scope':{'include':['.'], 'exclude':[]},
                'verification_plan':{'commands':[{'argv':[sys.executable,'-c','pass'],
                                                 'timeout_seconds':2}],
                                     'artifacts':[], 'dependencies':{}, 'environment':{}}}]}
            manifest = Path(runner.initialize(root,spec,fixture=True)['runner_path'])
            lab = manifest.parent
            initial = protocol.read_json(manifest)['targets']['author']
            cli = ['tmux','-S',str(lab/'tmux.sock'),'-f','/dev/null']

            def tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess:
                return subprocess.run(cli+list(args),capture_output=True,text=True,check=check,timeout=5)

            try:
                tmux('new-session','-d','-s','sentinel',sys.executable,'-c','import time; time.sleep(60)')
                crashed = subprocess.run([sys.executable,'-c',FAULT_WORKER,str(manifest),point,str(proof)],
                    cwd=Path(__file__).parent,capture_output=True,text=True,timeout=15)
                self.assertEqual(crashed.returncode,91 if point=='reply' else 92,crashed.stderr)
                ready = lab/'native/author.ready'
                deadline = time.monotonic()+5
                while not ready.exists() and time.monotonic()<deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                identity = tmux('list-panes','-t','='+initial['agent'],
                                '-F','#{pane_id} #{pane_pid} #{pane_dead}').stdout.strip()
                pane, _, dead = identity.split()
                self.assertEqual(dead,'0')
                saved_path = Path(initial['request_path']).with_name('resources.json')
                saved = saved_path.read_bytes() if saved_path.exists() else None
                self.assertEqual(saved is not None,point=='record')
                resources = protocol.parse_json(saved) if saved else {
                    'mode':'tmux','session':initial['agent'],'agent':initial['agent'],
                    'pane':pane,'tmux_selector':['-S',str(lab/'tmux.sock')],
                    'owns_agent':True,'owns_pane':True,'owns_workspace':False,'owns_session':True}
                recovery_proof = root/'recovery-proof.json'
                recovery_proof.write_text(json.dumps({'fixture':True,'identity':identity,
                                                       'controller_exit':crashed.returncode}))
                with runner.Runner(manifest) as recovered:
                    self.assertEqual(recovered.target('author')['phase'],'start_uncertain')
                    with patch.object(recovered,'execute',side_effect=AssertionError('must not launch')):
                        with self.assertRaisesRegex(ValueError,'startup may have landed'):
                            recovered.start('author',preflight,transport='tmux')
                        recovered.bind('author',{'resources':resources,'evidence_path':str(recovery_proof),
                                               'note':'Exact owned fixture/process inspected after controller death'})
                    self.assertEqual(recovered.target('author')['phase'],'started')
                    state = jobs.load(Path(recovered.data['index_path']),initial['job_id'])
                    self.assertEqual(state['submission'],'prepared')
                    self.assertFalse(Path(initial['result_path']).exists())
                if saved is not None:
                    self.assertEqual(saved_path.read_bytes(),saved)
                self.assertEqual(tmux('list-panes','-t','='+initial['agent'],
                    '-F','#{pane_id} #{pane_pid} #{pane_dead}').stdout.strip(),identity)
                self.assertEqual(set(tmux('list-sessions','-F','#{session_name}').stdout.splitlines()),
                                 {'sentinel',initial['agent']})
                starts = [protocol.read_json(p) for p in (lab/'evidence').glob('command-*/started.json')]
                self.assertEqual(sum('new-session' in c['argv'] for c in starts),1)
                self.assertFalse((lab/'native/author.received.jsonl').exists())
                tmux('send-keys','-t',pane,'-l','/exit')
                tmux('send-keys','-t',pane,'Enter')
                deadline = time.monotonic()+5
                while tmux('has-session','-t','='+initial['agent'],check=False).returncode==0:
                    self.assertLess(time.monotonic(),deadline,'fixture failed to exit')
                    time.sleep(0.01)
                self.assertEqual(tmux('has-session','-t','=sentinel',check=False).returncode,0)
            finally:
                for name in (initial['agent'],'sentinel'):
                    tmux('kill-session','-t','='+name,check=False)

    def test_controller_dies_after_live_allocation_before_receipt_is_consumed(self) -> None:
        self.exercise_fault('reply')

    def test_controller_dies_after_resources_are_saved_before_started_phase(self) -> None:
        self.exercise_fault('record')


if __name__ == '__main__':
    unittest.main()
