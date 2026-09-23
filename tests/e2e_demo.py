#!/usr/bin/env python3
"""Retained, deterministic small-project E2E via the public runner CLI and real tmux."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import e2e_runner
from e2e_runner import protocol
import reviews
import watch


def run_demo(parent: Path) -> dict:
    root = Path(tempfile.mkdtemp(prefix='runner-demo-',dir=parent))
    (root/'commands').mkdir()
    seed = root/'seed'; seed.mkdir()
    initial = 'def cents(value):\n    return int(float(value) * 100)\n'
    (seed/'money.py').write_text(initial)
    manifest = None
    owned = []
    cli = []
    observers = []

    def write(label, value):
        path = root/(label+'-'+uuid.uuid4().hex+'.json')
        protocol.publish(path,value)
        return str(path)

    def call(action, target=None, payload=None, extra=(), allowed=(0,)):
        argv = [sys.executable,str(Path(__file__).with_name('e2e_runner.py')),action]
        if manifest is not None:
            argv += ['--runner',manifest]
        if target:
            argv += ['--target',target]
        if payload is not None:
            argv += ['--input',write(action,payload)]
        result = subprocess.run(argv+list(extra),capture_output=True,text=True,timeout=15)
        path = root/'commands'/(str(time.time_ns())+'-'+action+'.json')
        protocol.publish(path,{'argv':argv+list(extra),'exit':result.returncode,
                              'stdout':result.stdout,'stderr':result.stderr})
        if result.returncode not in allowed:
            raise RuntimeError(f'runner {action}: {result.stderr}; {path}')
        return json.loads(result.stdout)

    def state():
        return protocol.read_json(manifest)

    def stop_observers():
        for process,log in observers:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            log.close()

    def watch_init():
        handle = call('watch-init')
        log = (root/('observer-'+uuid.uuid4().hex+'.log')).open('w')
        process = subprocess.Popen([sys.executable,str(e2e_runner.SCRIPTS/'watch.py'),'run',
            '--watch',handle['watch_path'],'--interval','0.1','--duration','300'],stdout=log,stderr=log)
        observers.append((process,log))
        deadline=time.monotonic()+5
        while True:
            health=protocol.read_json(Path(handle['watch_path']).with_name('state.json'))
            if len(health['targets'])==len(state()['targets']) and all(
                    t.get('last_successful_check_at') for t in health['targets'].values()):
                break
            if process.poll() is not None or time.monotonic()>deadline:
                raise RuntimeError('observer did not establish coverage before submission')
            time.sleep(0.02)
        runtime=protocol.read_json(Path(handle['watch_path']).with_name('observer.json'))
        proof = write('observer-owner',{'controller':'demo CLI driver','pid':process.pid,
            'watch_path':handle['watch_path'],'expires_at':runtime['expires_at']})
        for name in state()['targets']:
            call('monitor',name,{'monitor':{'owner':'e2e-demo-controller',
                'watch_path':handle['watch_path'],'expires_at':runtime['expires_at']},
                'evidence_path':proof,'note':'Managed observer started and every target checked before input'})
        for old,_ in observers[:-1]:
            if old.poll() is None:
                old.terminate()
                old.wait(timeout=5)
        return handle

    def tmux(*args, check=True):
        result = subprocess.run(cli+list(args),capture_output=True,text=True,timeout=5)
        if check and result.returncode:
            raise RuntimeError(result.stderr)
        return result

    def wait(predicate, message):
        end = time.monotonic()+5
        while time.monotonic()<end:
            if predicate():
                return
            time.sleep(0.02)
        raise RuntimeError(message)

    def packet(marker):
        return {'objective':marker,'scope':'Deterministic fixture in owned project',
                'acceptance':['Independent money oracle passes']}

    def submit(name):
        inspected = call('inspect',name)
        inspected.update(ready=True,note='Fixture-only raw input loop inspected')
        return call('submit',name,inspected)

    def receive(name):
        target = state()['targets'][name]
        result_path = Path(target['request_path']).with_name('result.json')
        wait(result_path.exists,'missing fixture response')
        proof = write('matching-result',{'result_path':str(result_path),'fixture':True})
        call('receipt',name,{'status':'accepted','kind':'matching_result',
                            'evidence_path':proof,'note':'Matched exact current round response'})
        return protocol.read_json(result_path)

    def resolve_pending(note):
        # This explicit fixture driver knows every event in its own simulated run.
        # The reusable runner has no approval or automatic review command.
        recovered = call('recover')['run']
        for event in recovered['action_required'] + recovered['waiting_user']:
            reviews.record_review(event['watch_path'],event['seq'],'handled',note,event['revision'])

    oracle = "from money import cents; assert [cents(v) for v in ['0.29','-0.29','1.10']] == [29,-29,110]; print('3 independent decimal cases passed')"
    verify_oracle = "import json; from pathlib import Path; assert json.loads(Path('cases.json').read_text()) == [['0.29',29],['-0.29',-29],['1.10',110]]"
    def plan(code):
        return {'commands':[{'argv':[sys.executable,'-c',code],'timeout_seconds':5}],
                'artifacts':[],'dependencies':{'python':sys.version.split()[0]},'environment':{}}
    spec = {'version':1,'observe_paths':[str(seed)],'targets':[
        {'name':'author','label':'实现金额汇总与返修','kind':'omp','seed':str(seed),
         'task_packet':packet('ASK_OUTPUT_FORMAT'),'scope':{'include':['.'],'exclude':['__pycache__']},
         'verification_plan':plan(oracle)},
        {'name':'verifier','label':'独立验证金额夹具','kind':'omp','seed':str(seed),
         'task_packet':packet('PREPARE_ORACLE'),'scope':{'include':['.'],'exclude':['__pycache__']},
         'verification_plan':plan(verify_oracle)}]}
    try:
        info = call('init',payload=spec,extra=('--root',str(root),'--fixture'))
        manifest = info['runner_path']; lab = Path(manifest).parent
        cli = ['tmux','-S',str(lab/'tmux.sock'),'-f','/dev/null']
        tmux('new-session','-d','-s','sentinel',sys.executable,'-c','import time; time.sleep(60)')
        owned.append('sentinel')
        for name in ('author','verifier'):
            target = state()['targets'][name]
            owned.append(target['agent'])  # Own named allocation intent even if a reply is lost.
            proof = write('preflight',{'fixture':True,'models_launched':0})
            call('start',name,{'host_version':'fixture-only','note':'Deterministic peer; no native model',
                'evidence_path':proof,'checks':{'model_and_approvals_preserved':True,
                'runtime_dependencies_verified':True}},extra=('--transport','tmux'))
        watch_init()
        wait(lambda: all((lab/'native'/(n+'.ready')).exists() for n in ('author','verifier')),
             'fixture terminal never became ready')
        labels = {}
        for name in ('author','verifier'):
            labels[name] = tmux('list-windows','-t','='+state()['targets'][name]['agent'],
                               '-F','#{window_name}').stdout.strip()
        assert labels == {'author':'实现金额汇总与返修 · omp','verifier':'独立验证金额夹具 · omp'},labels
        submit('author'); submit('verifier')
        # Actual controller-side negative control, while fixture work is outstanding.
        negative = subprocess.run([sys.executable,'-B','-c',oracle],cwd=seed,capture_output=True,text=True)
        assert negative.returncode != 0
        protocol.publish(lab/'controller-work.json',{'negative_control_exit':negative.returncode,
            'task':'Independently exercised known-bad seed against frozen oracle','fixture':True})
        assert receive('author')['status']=='blocked'
        wait(lambda: any(e['kind']=='attention' for e in call('recover')['run']['action_required']),
             'fixture permission did not surface')
        attention = next(e for e in call('recover')['run']['action_required'] if e['kind']=='attention')
        reviews.record_review(attention['watch_path'],attention['seq'],'waiting_user',
                              'Fixture decision saved before controller restart',attention['revision'])
        # Every call is already a new CLI/controller process. Preserve its durable question.
        recovered = call('recover')
        old_waiting = any(e['note']=='Fixture decision saved before controller restart'
                          for e in recovered['run']['waiting_user'])
        assert old_waiting
        resources = protocol.read_json(Path(state()['targets']['verifier']['request_path']).parent/'resources.json')
        # Explicitly scoped synthetic decision; never used by native runner modes.
        tmux('send-keys','-t',resources['pane'],'-l','ALLOW')
        tmux('send-keys','-t',resources['pane'],'Enter')
        receive('verifier')
        resolve_pending('Synthetic decision/blocked response reconciled; continuing in new author round')
        call('follow','author',packet('IMPLEMENT_BASELINE'),extra=('--note','Fixture writer yielded; JSON selected'))
        watch_init()
        submit('author'); receive('author')
        rejected = call('verify','author',extra=('--note','Fixture writer yielded for independent rejection'),allowed=(1,))
        assert not rejected['passed']
        call('follow','author',packet('FIX_DECIMAL'),extra=('--note','Fixture writer yielded for rework'))
        watch_init()
        submit('author'); receive('author')
        accepted = call('verify','author',extra=('--note','Fixture writer yielded for independent acceptance'))
        assert accepted['passed']
        assert call('verify','verifier',extra=('--note','Fixture oracle files yielded'))['passed']
        resolve_pending('All fixture results independently checked; no live native approvals')
        for name in ('author','verifier'):
            target = state()['targets'][name]
            resources = protocol.read_json(Path(target['request_path']).parent/'resources.json')
            tmux('send-keys','-t',resources['pane'],'-l','/exit')
            tmux('send-keys','-t',resources['pane'],'Enter')
            deadline=time.monotonic()+5
            while tmux('has-session','-t','='+target['agent'],check=False).returncode==0:
                if time.monotonic()>deadline:
                    raise RuntimeError('owned fixture did not exit')
                time.sleep(0.02)
            proof = write('host-exited',{'fixture':True,'session':target['agent'],
                'session_absent':True,'native_background':'fixture creates no children'})
            call('native',name,{'native':{'session_id':'fixture-native-'+name,'turn_id':target['round_id']},
                'evidence_path':proof,'note':'Explicit synthetic identity, not a model session'})
            now = time.time()
            call('host',name,{'host':'omp','host_version':'fixture-only','status':'settled',
                'disposition':'exited','observed_at':now,'valid_until':now+60,
                'checks':{k:'clear' for k in ('foreground','background','native_children','interaction','side_effects')},
                'children':[],'side_effect_paths':[target['cwd']],'evidence_path':proof,
                'note':'Actual fixture session exited; no native-model coverage claimed'})
            call('close',name,extra=('--evidence',proof,'--note','Fixture delivery and process exit checked'))
            call('cleanup-plan',name)
        stop_observers()
        resolve_pending('Fixture targets exited; final observer events reconciled after joining owned handles')
        recovery = call('recover')
        counts = {}
        for name in ('author','verifier'):
            records=[json.loads(line) for line in (lab/'native'/(name+'.received.jsonl')).read_text().splitlines()]
            assert len(records)==len({r['round_id'] for r in records})
            assert len({r['job_id'] for r in records})==1
            counts[name]=len(records)
        report = {'runner_path':manifest,'evidence_root':str(root),'native_models_launched':0,
            'received_rounds':counts,'old_waiting_user_recovered':old_waiting,
            'rejected_attempt':{'passed':rejected['passed'],'attempt_path':rejected['attempt_path']},
            'accepted_attempt':{'passed':accepted['passed'],'attempt_path':accepted['attempt_path']},
            'closed_jobs':sum(j['closed'] is not None for j in recovery['run']['jobs']),
            'pending_counts':recovery['run']['counts'],'sentinel_preserved':
                tmux('has-session','-t','=sentinel',check=False).returncode==0,
            'source_seed_unchanged':(seed/'money.py').read_text()==initial and
                not recovery['side_effects']['changed_paths'], 'labels':labels}
        protocol.publish(root/'report.json',report)
        return report
    except BaseException as exc:
        protocol.publish(root/'failure.json',{'error':str(exc),'runner_path':manifest})
        raise
    finally:
        stop_observers()
        if cli:
            for session in owned:
                tmux('kill-session','-t','='+session,check=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True,help='existing parent for retained evidence')
    args=parser.parse_args()
    print(json.dumps(run_demo(args.root.resolve()),ensure_ascii=False))


if __name__=='__main__':
    main()
