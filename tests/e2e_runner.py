#!/usr/bin/env python3
"""Opt-in, resumable E2E controller. No automatic approvals, resend or host exit."""
import argparse
from contextlib import AbstractContextManager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/agent-orchestrator/scripts'
sys.path.insert(0, str(SCRIPTS))
import protocol  # noqa: E402
import jobs  # noqa: E402
import runs  # noqa: E402
import watch  # noqa: E402
import delivery  # noqa: E402
import pages  # noqa: E402

require = protocol.require
KINDS = ('omp', 'codex', 'hermes', 'opencode')


def snapshot_paths(paths: list[str]) -> dict:
    """Retain hashes/metadata of explicitly declared observation scopes, not contents."""
    result = {}
    for name in paths:
        path = Path(name)
        if not os.path.lexists(path):
            result[name] = None
        elif path.is_dir():
            result[name] = delivery.inventory(path, ['.'], [])[0]
        else:
            result[name] = delivery.file_record(path)
    return result


def initialize(root: str | Path, spec: dict, fixture: bool = False, *, control_experiment: str | None = None) -> dict:
    """Copy seeds into a fresh owned lab, prepare shared jobs and pre-edit baselines."""
    root = Path(root).resolve()
    require(root.is_dir(), 'existing test parent required')
    require(set(spec) <= {'version', 'targets', 'observe_paths'} and spec.get('version') == 1,
            'invalid scenario fields/version')
    targets = spec.get('targets')
    require(isinstance(targets, list) and bool(targets), 'nonempty targets required')
    names = set()
    for target in targets:
        require(isinstance(target, dict) and set(target) <= {'name', 'label', 'kind', 'seed',
                'task_packet', 'scope', 'verification_plan', 'opencode_mode'}, 'unknown target fields')
        name = target.get('name')
        require(isinstance(name, str) and re.fullmatch('[a-z][a-z0-9_-]{0,39}', name) and
                name not in names, 'invalid or duplicate target name')
        names.add(name)
        require(target.get('kind') in KINDS, 'unknown host kind')
        pages.page_label(target.get('label'), target['kind'], target.get('opencode_mode'))
        require(target.get('opencode_mode') in (None, 'omo', 'pure') and
                (target.get('opencode_mode') is None or target['kind'] == 'opencode'), 'invalid host profile')
        seed = Path(target['seed'])
        require(seed.is_absolute() and seed.is_dir(), 'absolute existing seed directory required')
        require(not root.is_relative_to(seed.resolve()), 'test parent must be outside the seed directory')
        entries, _ = delivery.inventory(seed, ['.'], [])
        require(all(e['kind'] != 'symlink' for e in entries.values()), 'seed symlinks are not supported')
        scope = target['scope']
        require(set(scope) == {'include', 'exclude'} and isinstance(scope['include'], list) and
                bool(scope['include']) and isinstance(scope['exclude'], list), 'explicit snapshot scope required')
        for value in scope['include'] + scope['exclude']:
            delivery.relative(value, True)
        delivery.validate_plan(target['verification_plan'])
        packet = target['task_packet']
        require(isinstance(packet, dict) and all(packet.get(k) for k in ('objective', 'scope', 'acceptance')),
                'task packet requires objective, scope and acceptance')
    scopes = spec.get('observe_paths', [])
    require(isinstance(scopes, list) and all(isinstance(p, str) and Path(p).is_absolute() for p in scopes),
            'observe_paths must be explicit absolute paths')
    before = snapshot_paths(scopes)
    lab = Path(tempfile.mkdtemp(prefix='orch-e2e-', dir=root))
    for name in ('workspaces', 'packets', 'evidence', 'profiles', 'native'):
        (lab / name).mkdir(mode=0o700)
    run = runs.init_run(lab)
    data = {'version':1, 'runner_path':str(lab/'runner.json'), 'run_path':run['run_path'],
            'index_path':run['index_path'], 'fixture':fixture, 'created_at':protocol.utc_now(),
            'observe_paths':scopes, 'before':before, 'targets':{}, 'watch_path':None}
    if control_experiment is not None:
        data['control_experiment'] = control_experiment
    watch.atomic_save(lab/'runner.json', data)
    try:
        for target in targets:
            name = target['name']
            cwd = lab/'workspaces'/name
            shutil.copytree(target['seed'], cwd)
            packet_path = lab/'packets'/(name+'.json')
            protocol.publish(packet_path, target['task_packet'])
            info = protocol.prepare(argparse.Namespace(cwd=str(cwd), parent_depth=0, max_depth=None,
                previous=None, root=None, run=run['run_path'], task_file=None,
                task_packet=str(packet_path), temporary=False, brief=True))
            state = jobs.register(run['index_path'], info['request_path'])
            state = jobs.claim(run['index_path'], info['job_id'], state['revision'], 'e2e-runner', 3600)
            item = {**target, **info, 'cwd':str(cwd), 'phase':'prepared',
                    'token':state['lease']['token'], 'agent':'e2e-'+name[:19]+'-'+uuid.uuid4().hex[:8],
                    'baseline':None, 'verifications':[]}
            data['targets'][name] = item
            watch.atomic_save(lab/'runner.json', data)
            item['baseline'] = delivery.capture(info['request_path'], **target['scope'],
                handoff='Fresh owned seed copy; target has not started')['snapshot_path']
            watch.atomic_save(lab/'runner.json', data)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        protocol.publish(lab/'initialization-failure.json', {'error':str(exc), 'runner_path':data['runner_path']})
        raise protocol.ProtocolError(f'{exc}; partial lab retained: {lab}') from exc
    return {'runner_path':data['runner_path'], 'run_path':run['run_path'], 'fixture':fixture,
            'targets':list(data['targets']), 'launches_agents':False}


class Runner(AbstractContextManager):
    """One cooperating controller per lab; jobs retain the authoritative round state."""
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.root = self.path.parent
        self.data = protocol.read_json(self.path)
        require(self.data.get('version') == 1 and self.data.get('runner_path') == str(self.path),
                'runner manifest moved or invalid')
        self.lock = None

    def __enter__(self):
        self.lock = (self.root/'.runner.lock').open('a')
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            self.lock = None
            raise protocol.ProtocolError('runner has an active controller') from exc
        self.data = protocol.read_json(self.path)
        return self

    def __exit__(self, *args):
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def save(self) -> None:
        require(self.lock is not None, 'runner mutation needs the controller lock')
        watch.atomic_save(self.path, self.data)

    def target(self, name: str) -> dict:
        require(name in self.data['targets'], 'unknown target name')
        return self.data['targets'][name]

    def evidence(self, label: str, value: dict) -> str:
        path = self.root/'evidence'/(label+'-'+uuid.uuid4().hex+'.json')
        protocol.publish(path, value)
        return str(path)

    def mutate(self, name: str, action: str, payload: dict) -> dict:
        target = self.target(name)
        state = jobs.load(Path(self.data['index_path']), target['job_id'])
        require(state['active_request'] == target['request_path'],
                'runner request differs from job index; reconcile partial operation')
        return jobs.change(self.data['index_path'], target['job_id'], state['revision'],
                           target['token'], action, payload)

    def execute(self, argv: list[str], timeout: float = 15, env: dict | None = None) -> dict:
        """Keep command intent and output even after a client timeout; never infer non-delivery."""
        path = self.root/'evidence'/('command-'+uuid.uuid4().hex)
        path.mkdir(mode=0o700)
        record = {'argv':argv, 'started_at':time.time(), 'timeout_seconds':timeout}
        protocol.publish(path/'started.json', record)
        with (path/'stdout.log').open('xb') as stdout, (path/'stderr.log').open('xb') as stderr:
            try:
                result = subprocess.run(argv, cwd=self.root, env={**os.environ, **(env or {})},
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=timeout)
                record['exit_code'] = result.returncode
            except subprocess.TimeoutExpired:
                record.update(exit_code=None, timeout=True)
            except OSError as exc:
                record.update(exit_code=None, error=str(exc))
        record.update(finished_at=time.time(), stdout_path=str(path/'stdout.log'), stderr_path=str(path/'stderr.log'))
        protocol.publish(path/'result.json', record)
        return {**record, 'stdout':protocol.read_bytes(path/'stdout.log').decode('utf-8', errors='replace'),
                'evidence_path':str(path/'result.json')}

    @staticmethod
    def decoded(receipt: dict) -> dict:
        require(receipt['exit_code'] == 0, 'transport uncertain/failed; inspect '+receipt['evidence_path'])
        value = protocol.parse_json(receipt['stdout'])
        require(not value.get('error') and value.get('ok') is not False, 'transport error envelope')
        return value.get('result', value)

    def launch(self, target: dict, preflight: dict) -> tuple[list[str], dict]:
        """Use known host profiles; host preflight is caller evidence, not inferred approval."""
        required = {'model_and_approvals_preserved', 'runtime_dependencies_verified'}
        if target['kind'] == 'hermes' and not self.data['fixture']:
            required |= {'private_home_verified', 'automatic_maintenance_disabled'}
        require(set(preflight) <= {'host_version','evidence_path','note','checks','hermes_home','scanner_sha256'},
                'unknown preflight fields')
        require(set(preflight['checks']) == required and all(v is True for v in preflight['checks'].values()),
                'host preflight is incomplete')
        jobs.require_text(preflight['host_version'], 'host version')
        jobs.evidence(preflight)
        kind = target['kind']
        if self.data['fixture']:
            return [sys.executable, str(Path(__file__).parent/'fixtures/e2e_peer.py'),
                    str(self.path), target['name']], {}
        executable = shutil.which(kind)
        require(executable is not None, f'host executable unavailable: {kind}')
        env = {}
        argv = [executable]
        if kind == 'codex':
            argv += ['--no-alt-screen','--sandbox','workspace-write','--add-dir',
                     str(Path(self.data['run_path']).parent/'rounds')]
        elif kind == 'omp':
            argv += ['--session-dir', str(self.root/'native'/target['name'])]
        elif kind == 'opencode' and target.get('opencode_mode') == 'pure':
            argv += ['--pure']
        elif kind == 'hermes':
            home = Path(preflight['hermes_home'])
            require(home.is_absolute() and home == home.resolve() and home.parent == self.root/'profiles',
                    'Hermes needs its prepared owned profiles/<name> home')
            require((home/'config.yaml').is_file(), 'private Hermes config missing')
            scanner = home/'bin/tirith'
            require(scanner.is_file() and os.access(scanner, os.X_OK) and
                    delivery.file_record(scanner)['sha256'] == preflight['scanner_sha256'],
                    'private scanner missing or changed; do not auto-install')
            env = {'HERMES_HOME':str(home)}
            check = self.execute([executable,'config','path'], env=env)
            require(check['exit_code'] == 0 and check['stdout'].strip() == str(home/'config.yaml'),
                    'Hermes effective home differs; reconcile before launch')
        return argv, env

    def start(self, name: str, preflight: dict, *, transport: str = 'herdr', session: str | None = None,
              parent_pane: str | None = None, parent_tab: str | None = None, layout: str = 'auto') -> dict:
        target = self.target(name)
        if self.data.get('control_experiment'):
            from control_lab import preflight as experiment_preflight
            experiment_preflight(self, name)
        require(target['phase'] == 'prepared', 'startup may have landed; reconcile instead of starting again')
        require(transport in ('herdr','tmux'), 'invalid transport')
        require(transport != 'herdr' or bool(session), 'explicit existing herdr session required')
        layout = pages.select_layout(layout, parent_pane, parent_tab)
        require(transport != 'tmux' or (layout == 'workspace' and parent_pane is None and parent_tab is None),
                'tmux uses independent owned sessions')
        require(transport != 'tmux' or len(os.fsencode(self.root/'tmux.sock')) < 104,
                'tmux socket path too long; initialize under a shorter test root')
        require(not self.data['fixture'] or transport == 'tmux', 'fixture peers run only through tmux')
        argv, env = self.launch(target, preflight)
        page_label = pages.page_label(target['label'], target['kind'], target.get('opencode_mode'),
            [t['page_label'] for t in self.data['targets'].values() if t.get('page_label')])
        target.update(phase='start_uncertain', preflight=self.evidence('preflight',preflight),
                      page_label=page_label,
                      launch_argv=argv, host_version=preflight['host_version'], transport=transport,
                      start_context={'session':session,'layout':layout,'parent_pane':parent_pane,'parent_tab':parent_tab})
        self.save()
        self.mutate(name,'update',{'launch':{'argv':argv,'profile':preflight.get('hermes_home',
            target.get('opencode_mode') or 'existing-user-config')}})
        if transport == 'tmux':
            socket = str(self.root/'tmux.sock')
            cli = ['tmux','-S',socket,'-f','/dev/null']
            command = cli + ['new-session','-d','-P','-F','#{pane_id}','-s',target['agent'],
                             '-n',page_label,'-c',target['cwd']]
            for key,value in env.items():
                command += ['-e',key+'='+value]
            receipt = self.execute(command+argv)
            require(receipt['exit_code'] == 0, 'startup uncertain; inspect '+receipt['evidence_path'])
            resources = {'mode':'tmux','session':target['agent'],'agent':target['agent'],
                         'pane':receipt['stdout'].strip(),'tmux_socket':socket,'owns_session':True,
                         'owns_agent':True,'owns_pane':True,'owns_workspace':False}
        else:
            cli = ['herdr','--session',session]
            page_plan = pages.plan(session, target['cwd'], target['label'], target['kind'],
                layout=layout, parent_pane=parent_pane, parent_tab=parent_tab,
                opencode_mode=target.get('opencode_mode'), read=lambda a: self.decoded(self.execute(a)))
            target.update(page_label=page_plan['label'], page_plan=page_plan)
            self.save()
            command = list(page_plan['create_argv'])
            for key,value in env.items():
                command += ['--env',key+'='+value]
            created = self.decoded(self.execute(command))
            pane = created['root_pane']['pane_id']
            resources = {'mode':'insider' if parent_pane else 'isolated', 'session':session,
                'agent':target['agent'],'pane':pane,'workspace':pane.split(':')[0],
                'tab':created['tab']['tab_id'],'parent_pane':parent_pane,'parent_tab':parent_tab,
                'owns_workspace':layout=='workspace','owns_tab':layout=='tab',
                'owns_pane':True,'owns_agent':True,'owns_session':False}
            target['allocated_resources'] = resources
            self.save()
            live = self.decoded(self.execute(cli+['pane','get',pane]))
            pages.verify_membership(live['pane'], pane, resources['tab'])
            self.record(name, resources)
            self.decoded(self.execute(cli+['tab','rename',resources['tab'],target['page_label']]))
            receipt = self.execute(cli+['agent','start',target['agent'],'--kind',target['kind'],
                '--pane',pane,'--timeout','8000','--',*argv[1:]], timeout=12)
            target['startup_receipt'] = receipt['evidence_path']
            self.save()
            self.decoded(receipt)
        if transport == 'tmux':
            target['allocated_resources'] = resources
            self.save()
            self.record(name, resources)
        target['phase'] = 'started'
        self.save()
        return {'target':name,'resources':resources,'page_label':target['page_label'],
                'input_readiness':'requires fresh controller evidence'}

    def record(self, name: str, resources: dict) -> None:
        defaults = dict(workspace=None,tab=None,parent_pane=None,parent_tab=None,owns_tab=False,
                        tmux_socket=None,tmux_server=None,tmux_default_server=False)
        if resources.get('mode') == 'tmux' and 'tmux_selector' in resources:
            selector = protocol.validate_tmux_selector(resources['tmux_selector'])
            require(selector and selector[0] == '-S','runner needs its private socket')
            defaults['tmux_socket'] = selector[1]
        protocol.record(argparse.Namespace(**{**defaults, **resources, 'request':self.target(name)['request_path']}))

    def bind(self, name: str, payload: dict) -> dict:
        """Explicitly reconcile uncertain startup with inspected IDs; never allocate/relaunch."""
        target = self.target(name)
        require(target['phase'] == 'start_uncertain', 'bind is only for uncertain startup')
        jobs.evidence(payload)
        proposed = {**payload['resources'], 'job_id':target['job_id']}
        context = target['start_context']
        require(proposed.get('agent') == target['agent'] and proposed.get('owns_agent') is True and
                proposed.get('owns_pane') is True,'resource differs from startup intent')
        if target['transport'] == 'tmux':
            selector = proposed.get('tmux_selector', ['-S',proposed.get('tmux_socket')])
            require(proposed.get('mode') == 'tmux' and proposed.get('session') == target['agent'] and
                    selector == ['-S',str(self.root/'tmux.sock')] and proposed.get('owns_session') is True,
                    'resource differs from startup intent')
            proposed['tmux_selector'] = selector
        else:
            require(proposed.get('session') == context['session'] and proposed.get('owns_session') is False and
                    proposed.get('mode') == ('insider' if context['parent_pane'] else 'isolated') and
                    proposed.get('parent_pane') == context['parent_pane'] and
                    proposed.get('parent_tab') == context['parent_tab'] and
                    proposed.get('owns_workspace',False) == (context['layout']=='workspace') and
                    proposed.get('owns_tab',False) == (context['layout']=='tab'),
                    'resource differs from startup intent')
        protocol.validate_resources(proposed,protocol.load_request(target['request_path']))
        current = Path(target['request_path']).parent/'resources.json'
        if current.exists():
            require(proposed == protocol.read_json(current), 'recorded resources cannot be replaced by bind')
        observed = watch.observe({'resources':proposed,'tmux_selector':proposed.get('tmux_selector')})
        require(observed['state'] != 'dead','target has exited; preserve startup uncertainty')
        if not current.exists():
            self.record(name,proposed)
        target['phase'] = 'started'
        target['startup_reconciliation'] = self.evidence('startup-reconciliation',{'controller':payload,'live':observed})
        self.save()
        return {'target':name,'phase':'started','executes_commands':False}

    def watch_init(self) -> dict:
        active = [t for t in self.data['targets'].values() if t['phase'] != 'closed']
        handle = watch.init_watch([t['request_path'] for t in active])
        self.data['watch_path'] = handle['watch_path']
        self.save()
        return {**handle, 'observer_argv':[sys.executable,str(SCRIPTS/'watch.py'),'run','--watch',
                handle['watch_path'],'--interval','15','--duration','300'],
                'note':'Launch through a managed handle or use poll; no observer started here.'}

    def poll(self) -> dict:
        require(self.data['watch_path'] is not None,'watch-init required')
        return watch.poll_watch(self.data['watch_path'])

    def monitor(self, name: str, payload: dict) -> dict:
        """Record caller-owned supervision evidence, without starting an observer."""
        jobs.evidence(payload)
        require(payload['monitor']['watch_path'] == self.data['watch_path'],
                'monitor receipt must use the current runner watch')
        self.evidence('monitor',payload)
        state = self.mutate(name,'update',{'monitor':payload['monitor']})
        return {'monitor':state['monitor'],'observer_started':False}

    def submit(self, name: str, readiness: dict) -> dict:
        if self.data.get('control_experiment'):
            from control_lab import preflight as experiment_preflight
            experiment_preflight(self, name, submitting=True)
        target = self.target(name)
        require(target['phase'] == 'started', 'start and reconcile target before submission')
        state = jobs.load(Path(self.data['index_path']),target['job_id'])
        require(state['submission'] in ('prepared','rejected'), 'uncertain/accepted round cannot be resubmitted')
        resources = protocol.read_json(Path(target['request_path']).parent/'resources.json')
        stamp = readiness.get('observed_at')
        require(type(stamp) in (int,float) and 0 <= time.time()-stamp <= 30 and readiness.get('ready') is True and
                readiness.get('request_path') == target['request_path'] and readiness.get('resources') == resources,
                'fresh readiness for the exact request/resources required')
        jobs.evidence(readiness)
        observed = watch.observe({'resources':resources,'tmux_selector':resources.get('tmux_selector')})
        require(observed['state'] not in ('blocked','dead','working') and
                watch.dialog_key(observed['screen'],observed['state']) is None and
                (observed['state'] != 'unknown' or bool(observed['screen'].strip())),
                'target needs manual reconciliation before input')
        self.evidence('readiness',{'controller':readiness,'live':observed})
        import supervision
        coverage = supervision.check(self.data['run_path'])
        target_coverage = next((row for row in coverage['coverage'] if row['job_id'] == target['job_id']), None)
        # A fresh explicit readiness review may reconcile a nonempty no-hook UI.
        # It does not turn unknown UI into permission for independent work.
        require(coverage['complete'] and target_coverage is not None and
                not (set(target_coverage['issues']) - {'observation_unknown'}),
                'live supervision of the current round required before input; initialize/start watch and record monitor')
        self.evidence('submission-supervision', coverage)
        prompt = protocol.read_bytes(target['prompt_path']).decode('utf-8')
        require(json.loads(prompt.split('REQUEST=',1)[1]) == protocol.load_request(target['request_path']),
                'prompt no longer matches request')
        state = self.mutate(name,'begin',{})
        if resources['mode'] == 'tmux':
            cli = ['tmux',*resources['tmux_selector']]
            buffer = 'e2e-'+uuid.uuid4().hex
            commands = [cli+['load-buffer','-b',buffer,target['prompt_path']],
                        cli+['paste-buffer','-p','-d','-b',buffer,'-t',resources['pane']],
                        cli+['send-keys','-t',resources['pane'],'Enter']]
        else:
            commands = [['herdr','--session',resources['session'],'agent','prompt',resources['agent'],prompt]]
        receipts = []
        for command in commands:
            receipt = self.execute(command)
            receipts.append(receipt['evidence_path'])
            if receipt['exit_code'] != 0:
                break
            if 'paste-buffer' in command:
                time.sleep(0.15)
        target['send_receipts'] = receipts
        self.save()
        return {'attempt_id':state['attempts'][-1]['attempt_id'],'submission':'uncertain','evidence':receipts,
                'note':'Inspect result/live target, then record receipt. CLI exit alone is not acceptance.'}

    def receipt(self, name: str, payload: dict) -> dict:
        state = jobs.load(Path(self.data['index_path']),self.target(name)['job_id'])
        require(bool(state['attempts']),'no submission attempt')
        return self.mutate(name,'receipt',{**payload,'attempt_id':state['attempts'][-1]['attempt_id']})

    def follow(self, name: str, packet: dict, handoff: str) -> dict:
        target = self.target(name)
        require(target['phase'] == 'started','target is not reusable')
        require('candidate_round' not in target,'reconcile candidate round before preparing another')
        packet_path = self.root/'packets'/(name+'-'+uuid.uuid4().hex+'.json')
        protocol.publish(packet_path,packet)
        info = protocol.prepare(argparse.Namespace(previous=target['request_path'],cwd=None,parent_depth=None,
            max_depth=None,root=None,task_file=None,task_packet=str(packet_path),brief=True))
        baseline = delivery.capture(info['request_path'],**target['scope'],handoff=handoff)['snapshot_path']
        target['candidate_round'] = {**info,'baseline':baseline}
        self.save()
        self.mutate(name,'activate',{'request_path':info['request_path']})
        target.update(info,baseline=baseline)
        target.pop('candidate_round',None)
        self.save()
        return info

    def reconcile_round(self, name: str) -> dict:
        """Resume a recorded activation boundary without preparing or submitting again."""
        target = self.target(name)
        candidate = target.get('candidate_round')
        require(isinstance(candidate,dict),'no recorded candidate round')
        state = jobs.load(Path(self.data['index_path']),target['job_id'])
        require(delivery.check(candidate['baseline'])['valid'],'candidate baseline changed')
        if state['active_request'] == target['request_path']:
            self.mutate(name,'activate',{'request_path':candidate['request_path']})
        else:
            require(state['active_request'] == candidate['request_path'] and
                    state['round_id'] == candidate['round_id'] and state['submission'] == 'prepared',
                    'candidate does not match the indexed unsent round')
            jobs.check_contract(state)
        target.update(candidate)
        target.pop('candidate_round')
        self.save()
        return {'request_path':target['request_path'],'round_id':target['round_id'],'submitted':False}

    def inspect(self, name: str) -> dict:
        """Capture live identity/UI for a controller to review; readiness defaults false."""
        target = self.target(name)
        resources = protocol.read_json(Path(target['request_path']).parent/'resources.json')
        observed = watch.observe({'resources':resources,'tmux_selector':resources.get('tmux_selector')})
        value = {'request_path':target['request_path'],'resources':resources,'observed_at':time.time(),
                 'ready':False,'note':'Controller must inspect target/UI before confirming readiness',
                 'observation':observed}
        return {**value,'evidence_path':self.evidence('inspection',value)}

    def claim(self, name: str, payload: dict) -> dict:
        """Explicit takeover after the caller reconciles that the former input owner stopped."""
        jobs.evidence(payload)
        target = self.target(name)
        state = jobs.load(Path(self.data['index_path']),target['job_id'])
        claimed = jobs.claim(self.data['index_path'],target['job_id'],state['revision'],'e2e-runner',3600)
        target['token'] = claimed['lease']['token']
        target['claim_evidence'] = self.evidence('claim',payload)
        self.save()
        return {'revision':claimed['revision'],'expires_at':claimed['lease']['expires_at'],
                'submission':claimed['submission'],'submitted':False}

    def verify(self, name: str, handoff: str) -> dict:
        target = self.target(name)
        state = jobs.load(Path(self.data['index_path']),target['job_id'])
        require(jobs.result_view(state)['status'] == 'success','valid success needed before delivery verification')
        captured = delivery.capture(target['request_path'],**target['scope'],handoff=handoff,baseline=target['baseline'])
        verified = delivery.verify(captured['snapshot_path'],target['verification_plan'])
        proof = self.evidence('verification',verified)
        target['verifications'].append({'round_id':target['round_id'],**captured,**verified})
        self.save()
        self.mutate(name,'accept',{'kind':'delivery','verdict':'accepted' if verified['passed'] else 'rejected',
            'attempt_path':verified['attempt_path'],'evidence_path':proof,'note':handoff})
        return verified

    def native(self, name: str, payload: dict) -> dict:
        jobs.evidence(payload)
        proof = self.evidence('native-binding',payload)
        state = self.mutate(name,'update',{'native':payload['native']})
        return {'native':state['native'],'evidence_path':proof}

    def host(self, name: str, payload: dict) -> dict:
        require(payload['host'] == self.target(name)['kind'],'host kind mismatch')
        self.evidence('side-effects-after',self.side_effects())
        state = self.mutate(name,'host',payload)
        return state['completion']['host']

    def close(self, name: str, evidence_path: str, note: str) -> dict:
        state = self.mutate(name,'close',{'outcome':'completed','evidence_path':evidence_path,'note':note})
        self.target(name)['phase'] = 'closed'
        self.save()
        return state['closed']

    def side_effects(self) -> dict:
        after = snapshot_paths(self.data['observe_paths'])
        return {'before':self.data['before'],'after':after,
                'changed_paths':[p for p in after if after[p] != self.data['before'][p]],
                'scope':'declared paths only; changes require controller review'}

    def recover(self) -> dict:
        return {'runner_path':str(self.path),'fixture':self.data['fixture'],
                'targets':{name:{k:t.get(k) for k in ('phase','request_path','candidate_round','baseline','startup_receipt')}
                           for name,t in self.data['targets'].items()},
                'run':runs.recover(self.data['run_path']), 'side_effects':self.side_effects(),
                'executes_commands':False}

    def checkpoint(self) -> dict:
        """Inspect durable queues and real observer lifetime before a work block."""
        import supervision
        return supervision.check(self.data['run_path'])

    def cleanup_plan(self, name: str) -> dict:
        return protocol.cleanup_plan(argparse.Namespace(request=self.target(name)['request_path']))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('init','start','bind','submit','receipt','follow','watch-init','poll',
                                        'recover','verify','native','host','close','cleanup-plan','inspect',
                                        'claim','reconcile-round','monitor','checkpoint'))
    parser.add_argument('--root',type=Path)
    parser.add_argument('--runner',type=Path)
    parser.add_argument('--target')
    parser.add_argument('--input',type=Path,help='scenario, preflight or exact action receipt JSON')
    parser.add_argument('--fixture',action='store_true',help='init only: deterministic peers, never native models')
    parser.add_argument('--transport',choices=('herdr','tmux'),default='herdr')
    parser.add_argument('--session',help='existing, explicitly selected herdr session')
    parser.add_argument('--parent-pane')
    parser.add_argument('--parent-tab')
    parser.add_argument('--layout',choices=('auto','workspace','tab'),default='auto')
    parser.add_argument('--note',help='writer handoff or closure note')
    parser.add_argument('--evidence',help='closure evidence path')
    args = parser.parse_args()
    try:
        require(args.action == 'init' or not args.fixture,'--fixture is an init-only choice')
        payload = protocol.read_json(args.input) if args.input else None
        if args.action == 'init':
            require(args.root is not None and payload is not None,'init needs --root and --input')
            output = initialize(args.root,payload,args.fixture)
        else:
            require(args.runner is not None,'--runner required')
            with Runner(args.runner) as controller:
                if args.action == 'start':
                    output = controller.start(args.target,payload,transport=args.transport,session=args.session,
                        parent_pane=args.parent_pane,parent_tab=args.parent_tab,layout=args.layout)
                elif args.action in ('bind','submit','receipt','native','host','claim','monitor'):
                    require(payload is not None,'--input required')
                    output = getattr(controller,args.action)(args.target,payload)
                elif args.action == 'follow':
                    output = controller.follow(args.target,payload,args.note)
                elif args.action == 'verify':
                    output = controller.verify(args.target,args.note)
                elif args.action == 'close':
                    output = controller.close(args.target,args.evidence,args.note)
                elif args.action in ('cleanup-plan','inspect','reconcile-round'):
                    output = getattr(controller,args.action.replace('-','_'))(args.target)
                else:
                    output = getattr(controller,args.action.replace('-','_'))()
        print(json.dumps(output,ensure_ascii=False,allow_nan=False))
        if args.action == 'checkpoint':
            return 0 if output['can_work'] else 1
        return 1 if args.action == 'verify' and not output['passed'] else 0
    except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
