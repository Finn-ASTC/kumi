#!/usr/bin/env python3
"""Pinned intervention inspections and receipts; never sends keys or stops processes."""
import argparse
import json
from pathlib import Path
import sys
from typing import Any
import uuid

import jobs
import protocol
import reviews
import watch

KINDS = ('deny', 'interrupt', 'cancel')
STOP_CHECKS = {'foreground', 'background', 'native_children', 'interaction', 'side_effects'}
MAX_AGE = 30


def identity(state: dict[str, Any]) -> dict[str, Any]:
    """Pin active contract, actual transport and native session independently of labels."""
    return {'index_path': state['index_path'], 'job_id': state['job_id'], 'round_id': state['round_id'],
            'request': state['rounds'][-1], 'resources': jobs.resources_for(state['active_request']),
            'native': state['native'], 'launch': state['launch'],
            'attempt_id': state['attempts'][-1]['attempt_id'] if state['attempts'] else None}


def event_context(watch_path: str, seq: int, state: dict[str, Any]) -> dict[str, Any]:
    """Require a current, pinned incident; repeated identical dialogs have distinct issues."""
    detail = reviews.event_detail(watch_path, seq)
    active_watch = state['monitor']['watch_path']
    protocol.require(active_watch is None or active_watch == detail['watch_path'],
                     'event belongs to a replaced monitor; inspect current watch')
    event, target = detail['event'], detail['target']
    protocol.require(detail['integrity'] == 'pinned', 'new control needs pinned event evidence')
    protocol.require(event['kind'] == 'attention' and event.get('issue_id'), 'control needs an attention incident')
    protocol.require(event['job_id'] == state['job_id'] and event['round_id'] == state['round_id'] and
                     target['request_path'] == state['active_request'] and
                     target['request_sha256'] == state['rounds'][-1]['request_sha256'] and
                     target['resources'] == jobs.resources_for(state['active_request'])['record'],
                     'event target identity changed or belongs to an old round')
    current = protocol.read_json(Path(watch_path).parent / 'state.json')['targets'].get(state['round_id'], {})
    protocol.require(current.get('attention_issue') == event['issue_id'] and current.get('attention_key') and
                     not current.get('error'), 'event incident is no longer current or observation failed')
    protocol.require(detail['review']['status'] != 'handled', 'event already handled; inspect new incident')
    protocol.require(not detail['observation_error'], 'event observation is incomplete')
    return detail


def target_for(state: dict[str, Any]) -> dict[str, Any]:
    """Use only the exact recorded transport, including the tmux socket selector."""
    resources = jobs.resources_for(state['active_request'])
    protocol.require(resources is not None, 'exact resources required for intervention')
    return {'resources': resources['record'], 'tmux_selector': resources['record'].get('tmux_selector')}


def observe(state: dict[str, Any]) -> dict[str, Any]:
    """Keep an unreachable target observable as unknown, never as a proven stop."""
    import subprocess
    target = target_for(state)
    try:
        return watch.observe(target)
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        return {'state': 'unknown', 'screen': '', 'transport_error': str(exc)}


def incident_active(detail: dict[str, Any]) -> None:
    """Recheck observer occurrence after potentially slow live terminal reads."""
    current = protocol.read_json(Path(detail['watch_path']).parent / 'state.json')['targets'].get(
        detail['event']['round_id'], {})
    protocol.require(current.get('attention_issue') == detail['event']['issue_id'] and
                     current.get('attention_key') and not current.get('error'),
                     'event incident changed during live validation')


def check_dialog(detail: dict[str, Any], observed: dict[str, Any]) -> None:
    """Readback must retain the same captured operation/options; no key inference."""
    protocol.require(observed.get('state') != 'dead' and
                     watch.dialog_key(observed.get('screen', ''), 'working') is not None and
                     watch.dialog_key(observed['screen'], 'working') ==
                     watch.dialog_key(detail['screen'], 'working'), 'live dialog changed or disappeared')
    protocol.require(not observed.get('capture', {}).get('locally_truncated'),
                     'locally truncated dialog requires manual reconciliation')


def inspect(index: str | Path, job: str, *, watch_path: str | None = None, seq: int | None = None,
            now: float | None = None) -> dict[str, Any]:
    """Read live identity and preserve full observation; never authorizes or executes input."""
    index = Path(index).resolve()
    state = jobs.load(index, job)
    protocol.require(state['closed'] is None, 'job is closed')
    protocol.require((watch_path is None) == (seq is None), 'watch and seq must be supplied together')
    detail = event_context(watch_path, seq, state) if watch_path is not None else None
    observed = observe(state)
    if detail:
        check_dialog(detail, observed)
        incident_active(detail)
    current = jobs.clock(now)
    protocol.require(jobs.load(index, job)['revision'] == state['revision'], 'job changed during inspection')
    event_ref = None if detail is None else {
        'watch_path': detail['watch_path'], 'watch_id': detail['watch_id'], 'seq': seq,
        'issue_id': detail['event']['issue_id'], 'event_fingerprint': detail['event_fingerprint'],
        'evidence_sha256': detail['evidence_sha256'],
        'watch_sha256': jobs.fingerprint(Path(detail['watch_path']))}
    if detail:
        protocol.require(detail['event']['observed_at'] <= current, 'event time is in the future')
    record = {'version': 1, 'identity': identity(state), 'observed_at': current,
              'event_ref': event_ref, 'observation': observed, 'executes_commands': False}
    directory = jobs.job_directory(index, job) / 'control-inspections'
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / (uuid.uuid4().hex + '.json')
    protocol.publish(path, record)
    return {'inspection_path': str(path), 'sha256': jobs.fingerprint(path),
            'job_id': job, 'round_id': state['round_id'], 'observed_at': current,
            'event_ref': event_ref, 'capture': observed.get('capture', {'completeness': 'unknown'}),
            'executes_commands': False, 'note': 'Inspection is evidence, not approval or atomic input permission.'}


def begin(state: dict[str, Any], payload: dict[str, Any], now: float) -> None:
    """Persist an uncertain intent under the job lease before any external action."""
    protocol.require(set(payload) == {'kind', 'inspection_path', 'method', 'evidence_path', 'note'},
                     'invalid control intent fields')
    kind = payload['kind']
    protocol.require(kind in KINDS, 'invalid control kind')
    old = state.get('control')
    protocol.require(not old or old['status'] != 'uncertain', 'unresolved control; reconcile without resending')
    protocol.require(not old or old['kind'] != 'cancel' or old['status'] != 'confirmed',
                     'cancel confirmed; close the job')
    path = Path(payload['inspection_path']).resolve()
    protocol.require(path.parent == jobs.job_directory(Path(state['index_path']), state['job_id']) /
                     'control-inspections', 'inspection must belong to this job')
    inspection = protocol.read_json(path)
    protocol.require(inspection.get('version') == 1 and inspection.get('identity') == identity(state),
                     'inspection identity changed')
    resources = jobs.resources_for(state['active_request'])
    protocol.require(resources is not None, 'exact resources required for intervention')
    index = Path(state['index_path'])
    for directory in sorted(index.iterdir()):
        if not directory.is_dir() or not jobs.ID.fullmatch(directory.name) or directory.name == state['job_id']:
            continue
        other = jobs.load(index, directory.name)
        other_resources = jobs.resources_for(other['active_request'])
        protocol.require(other['closed'] is not None or other_resources is None or
                         jobs.target_key(other_resources) != jobs.target_key(resources),
                         'control target belongs to another open job in this index')
    stamp = jobs.timestamp(inspection['observed_at'])
    protocol.require(0 <= now - stamp <= MAX_AGE, 'inspection stale or expired')
    method = payload['method']
    protocol.require(isinstance(method, dict) and set(method) == {'kind', 'description'} and
                     method['kind'] in ('keys', 'native_api', 'process_signal', 'manual'), 'invalid control method')
    jobs.require_text(method['description'], 'exact control method description')
    reference = inspection['event_ref']
    protocol.require(kind != 'deny' or reference is not None, 'deny requires an exact event')
    detail = None
    if reference:
        protocol.require(jobs.fingerprint(Path(reference['watch_path'])) == reference['watch_sha256'],
                         'watch identity changed')
        detail = event_context(reference['watch_path'], reference['seq'], state)
        protocol.require(all(reference[k] == detail[k] for k in ('watch_id', 'event_fingerprint', 'evidence_sha256'))
                         and reference['issue_id'] == detail['event']['issue_id'], 'event identity changed')
        for prior in state.get('controls', []):
            if prior['status'] != 'not_sent' and prior.get('event_ref') == reference:
                raise protocol.ProtocolError('incident already has an action; inspect without repeating')
    observed = observe(state)
    if detail:
        check_dialog(detail, observed)
        incident_active(detail)
    protocol.require(not (observed.get('transport_error') or observed.get('state') == 'dead') or
                     method['kind'] == 'manual',
                     'unavailable target permits manual reconciliation only; no input intent')
    if detail is None:
        protocol.require(observed.get('screen') == inspection['observation'].get('screen') and
                         observed.get('state') == inspection['observation'].get('state'),
                         'live target changed since inspection; inspect again')
    protocol.require(inspection['identity'] == identity(state), 'target identity changed during validation')
    control = {'version': 1, 'control_id': uuid.uuid4().hex, 'kind': kind, 'status': 'uncertain',
               'identity': identity(state), 'event_ref': reference, 'begun_at': now,
               'owner': state['lease']['owner'], 'method': method,
               'inspection': {'path': str(path), 'sha256': jobs.fingerprint(path)},
               'live_observation': {'state': observed.get('state'),
                                    'screen_sha256': watch.digest(observed.get('screen', '')),
                                    'capture': observed.get('capture'),
                                    'transport_error': observed.get('transport_error')},
               'decision_evidence': jobs.evidence(payload), 'receipt': None}
    state.setdefault('controls', []).append(control)
    state['control'] = control


def receipt(state: dict[str, Any], payload: dict[str, Any], now: float) -> None:
    """Record actual outcome separately from intent; uncertain never implies not sent."""
    protocol.require(set(payload) == {'control_id', 'status', 'outcome', 'observed_at', 'checks',
                                      'evidence_path', 'note'}, 'invalid control receipt fields')
    control = state.get('control')
    protocol.require(control is not None and payload['control_id'] == control['control_id'],
                     'control identity mismatch')
    refresh_stop = (control['status'] == 'confirmed' and control['kind'] in ('cancel', 'interrupt') and
                    payload['status'] == 'confirmed' and payload['outcome'] == 'stopped')
    protocol.require(control['status'] == 'uncertain' or refresh_stop, 'control already resolved')
    protocol.require(control['identity'] == identity(state), 'control target identity changed')
    stamp = jobs.timestamp(payload['observed_at'])
    protocol.require(not refresh_stop or stamp >= control['receipt']['observed_at'],
                     'stop refresh cannot move observation time backwards')
    protocol.require(control['begun_at'] <= stamp <= now,
                     'invalid control outcome time')
    status, outcome = payload['status'], payload['outcome']
    protocol.require(status in ('uncertain', 'not_sent', 'confirmed'), 'invalid control receipt status')
    checks = payload['checks']
    protocol.require(isinstance(checks, dict) and all(v in ('clear', 'pending', 'unknown') for v in checks.values()),
                     'invalid control checks')
    if status == 'confirmed':
        expected = 'denied' if control['kind'] == 'deny' else 'stopped'
        protocol.require(outcome == expected or (control['kind'] == 'deny' and outcome == 'invalidated'),
                         'outcome does not match control kind')
        required = {'operation', 'scope'} if control['kind'] == 'deny' else STOP_CHECKS
        protocol.require(set(checks) == required and all(v == 'clear' for v in checks.values()),
                         'confirmation requires complete clear checks')
    else:
        protocol.require(outcome is None, 'unconfirmed control has no outcome')
    entry = {'status': status, 'outcome': outcome, 'observed_at': stamp, 'recorded_at': now,
             'checks': checks, 'owner': state['lease']['owner'], 'evidence': jobs.evidence(payload),
             'job_revision': state['revision'] + 1}
    control.update(status=status, receipt=entry)
    # Serialized snapshots do not preserve aliasing between the list and latest item.
    state['controls'][-1] = control


def guard(state: dict[str, Any], action: str, payload: dict[str, Any]) -> None:
    """Block cooperating writers while intervention is unresolved, including after takeover."""
    control = state.get('control')
    if not control:
        return
    if control['status'] == 'uncertain':
        protocol.require(action not in ('begin', 'activate', 'close', 'control-begin') and
                         not (action == 'update' and bool({'native', 'launch'} & payload.keys())),
                         'unresolved control; reconcile without new input, identity change or closure')
    if control['kind'] == 'cancel' and control['status'] == 'confirmed':
        protocol.require(action not in ('begin', 'activate', 'control-begin') and
                         not (action == 'update' and bool({'native', 'launch'} & payload.keys())) and
                         not (action == 'close' and payload.get('outcome') != 'cancelled'),
                         'cancel confirmed; close the job before new work')


def cancellation(state: dict[str, Any], now: float) -> dict[str, Any]:
    """Require scoped stop evidence for an allocated/launched job, even without a result."""
    if jobs.resources_for(state['active_request']) is None and state['launch'] is None and not state['attempts']:
        return {'stop_status': 'not_started', 'control_id': None}
    control = state.get('control')
    protocol.require(control and control['kind'] in ('cancel', 'interrupt') and control['status'] == 'confirmed'
                     and control['receipt']['outcome'] == 'stopped', 'cancel closure requires confirmed stop')
    protocol.require(control['identity'] == identity(state), 'stop identity changed')
    proof = control['receipt']
    protocol.require(0 <= now - proof['observed_at'] <= MAX_AGE and
                     jobs.fingerprint(Path(proof['evidence']['path'])) == proof['evidence']['sha256'],
                     'stop confirmation expired or evidence changed')
    return {'stop_status': 'confirmed', 'control_id': control['control_id']}


def review(index: str | Path, job: str, control_id: str, expected_revision: int,
           note: str, now: float | None = None) -> dict[str, Any]:
    """Attach only a confirmed control to its original event; never clear newer incidents."""
    index = Path(index).resolve()
    state = jobs.load(index, job)
    control = next((c for c in state.get('controls', []) if c['control_id'] == control_id), None)
    protocol.require(control is not None and control['status'] == 'confirmed',
                     'review needs a confirmed control')
    reference = control['event_ref']
    protocol.require(reference is not None, 'control has no event to review')
    detail = reviews.event_detail(reference['watch_path'], reference['seq'])
    protocol.require(all(reference[k] == detail[k] for k in ('watch_id', 'event_fingerprint', 'evidence_sha256'))
                     and reference['issue_id'] == detail['event']['issue_id'], 'control review event changed')
    proof = control['receipt']
    protocol.require(jobs.fingerprint(Path(proof['evidence']['path'])) == proof['evidence']['sha256'],
                     'control outcome evidence changed')
    # Pin the immutable job revision carrying this control, not a mutable status file.
    action = jobs.job_directory(index, job) / f'{proof["job_revision"]:012d}.json'
    return reviews.record_review(reference['watch_path'], reference['seq'], 'handled', note,
        expected_revision, now=now, action_evidence=str(action),
        resolution_evidence=proof['evidence']['path'], resolved_at=proof['observed_at'])


def main() -> int:
    """Inspect terminal identity or record an already-confirmed event review."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inspect', 'review'), nargs='?', default='inspect')
    parser.add_argument('--index', required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--watch')
    parser.add_argument('--seq', type=int)
    parser.add_argument('--control-id')
    parser.add_argument('--expected-review-revision', type=int)
    parser.add_argument('--note-file')
    args = parser.parse_args()
    try:
        if args.action == 'review':
            protocol.require(args.control_id and args.expected_review_revision is not None and args.note_file,
                             'review needs control id, expected review revision and note file')
            output = review(args.index, args.job, args.control_id, args.expected_review_revision,
                            Path(args.note_file).read_text(encoding='utf-8'))
        else:
            output = inspect(args.index, args.job, watch_path=args.watch, seq=args.seq)
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
