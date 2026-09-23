"""Version-bound acceptance and scoped host observations; never sends terminal input."""

from pathlib import Path
from typing import Any

import protocol
import jobs


CHECKS = {'foreground', 'background', 'native_children', 'interaction', 'side_effects'}


def binding(state: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Bind facts to the active round and exact published response bytes."""
    return {'job_id': state['job_id'], 'round_id': state['round_id'],
            'request_sha256': state['rounds'][-1]['request_sha256'],
            'result_sha256': result.get('sha256')}


def target(state: dict[str, Any]) -> dict[str, Any]:
    """A replaced native conversation or launch profile invalidates host observations."""
    return {'resources': state['resources'], 'native': state['native'], 'launch': state['launch']}


def evidence_valid(record: dict[str, Any]) -> None:
    """Detect changed or missing controller evidence on each readback."""
    ref = record['evidence']
    path = Path(ref['path'])
    protocol.require(path.is_absolute() and jobs.fingerprint(path) == ref['sha256'],
                     'completion evidence changed')


def delivery_check(attempt_path: str, expected: dict[str, Any]) -> dict[str, Any]:
    """Independently read back the attempt and its exact round/result binding."""
    import delivery
    report = delivery.inspect(attempt_path)
    protocol.require(report['complete'] and report['evidence_valid'], 'delivery evidence is incomplete or changed')
    _, snapshot = delivery.load_snapshot(report['snapshot_path'])
    protocol.require(all(snapshot.get(k) == v for k, v in expected.items()), 'delivery identity mismatch')
    return report


def acceptance_record(state: dict[str, Any], payload: dict[str, Any], now: float) -> dict[str, Any]:
    """Validate a review against one exact round, independent of the current job head."""
    kind = payload.get('kind')
    protocol.require(kind in ('answer', 'delivery'), 'acceptance kind must be answer or delivery')
    keys = {'kind', 'verdict', 'evidence_path', 'note'} | ({'attempt_path'} if kind == 'delivery' else set())
    protocol.require(set(payload) == keys and payload['verdict'] in ('accepted', 'rejected'),
                     'invalid acceptance fields/verdict')
    result = jobs.result_view(state)
    protocol.require(result['status'] == 'success', 'acceptance needs a valid success response')
    identity = binding(state, result)
    record = {'version': 1, **identity, 'kind': kind, 'verdict': payload['verdict'],
              'recorded_at': now, 'evidence': jobs.evidence(payload)}
    if kind == 'answer':
        response = protocol.read_json(result['path'])
        protocol.require(not any(response[k] for k in protocol.FILE_FIELDS),
                         'declared files require delivery acceptance')
    else:
        jobs.require_text(payload['attempt_path'], 'attempt_path')
        path = Path(payload['attempt_path']).resolve()
        report = delivery_check(str(path), identity)
        if payload['verdict'] == 'accepted':
            protocol.require(report['passed'], 'accepted delivery must have passed independent checks')
        record['attempt'] = {'path': str(path), 'sha256': jobs.fingerprint(path)}
    return record


def record_acceptance(state: dict[str, Any], payload: dict[str, Any], now: float) -> None:
    """Append a current review or a historical annotation under the job's live lease."""
    import round_history
    payload = dict(payload)
    round_id = payload.pop('round_id', state['round_id'])
    selected, transition = round_history.select(state, round_id)
    record = acceptance_record(selected, payload, now)
    historical = round_id != state['round_id']
    if historical or state['closed'] is not None:
        previous = selected.get('completion', {}).get('acceptance') or {}
        pinned_result = (transition or {}).get('result_sha256', previous.get('result_sha256'))
        protocol.require(pinned_result is None or pinned_result == record['result_sha256'],
                         'historical result changed; cannot annotate replacement bytes')
    record.update(backfilled=historical or state['closed'] is not None,
                  recorded_revision=state['revision'] + 1)
    if historical:
        state.setdefault('historical_acceptance', {})[round_id] = record
    else:
        state.setdefault('completion', {})['acceptance'] = record


def acceptance_summary(state: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Distinguish a valid negative review from missing or changed evidence."""
    record = state.get('completion', {}).get('acceptance')
    if result['status'] in ('blocked', 'error') and record is None:
        return {'status': 'not_applicable', 'valid': False, 'reason': 'no success response to accept'}
    if record is None:
        return {'status': 'missing', 'valid': False, 'reason': 'independent acceptance is missing'}
    try:
        protocol.require(result['status'] == 'success', 'acceptance needs a valid success response')
        identity = binding(state, result)
        protocol.require(record['version'] == 1 and all(record[k] == v for k, v in identity.items()),
                         'acceptance identity changed')
        evidence_valid(record)
        protocol.require(record['verdict'] in ('accepted', 'rejected'), 'invalid acceptance verdict')
        if record['kind'] == 'delivery':
            ref = record['attempt']
            protocol.require(jobs.fingerprint(Path(ref['path'])) == ref['sha256'], 'delivery attempt changed')
            report = delivery_check(ref['path'], identity)
            if record['verdict'] == 'accepted':
                protocol.require(report['passed'], 'delivery has not passed')
        else:
            protocol.require(record['kind'] == 'answer', 'invalid acceptance kind')
            response = protocol.read_json(result['path'])
            protocol.require(not any(response[k] for k in protocol.FILE_FIELDS),
                             'declared files require delivery acceptance')
        return {'status': record['verdict'], 'valid': True, 'reason': None}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        return {'status': 'invalid', 'valid': False, 'reason': str(exc)}


def record_host(state: dict[str, Any], payload: dict[str, Any], now: float) -> None:
    """Record caller-observed lifecycle scope, retaining unknown/pending checks explicitly."""
    keys = {'host', 'host_version', 'status', 'disposition', 'observed_at', 'valid_until',
            'checks', 'children', 'side_effect_paths', 'evidence_path', 'note'}
    protocol.require(set(payload) == keys, 'invalid host observation fields')
    protocol.require(payload['host'] in ('hermes', 'omp', 'codex', 'opencode'), 'unknown host')
    jobs.require_text(payload['host_version'], 'host_version')
    protocol.require(payload['status'] in ('active', 'waiting', 'settled', 'unknown'), 'invalid host status')
    protocol.require(payload['disposition'] in ('reusable', 'exited', None), 'invalid host disposition')
    observed, until = jobs.timestamp(payload['observed_at']), jobs.timestamp(payload['valid_until'])
    protocol.require(observed <= now and 0 < until - observed <= 300, 'invalid host observation time window')
    checks = payload['checks']
    protocol.require(isinstance(checks, dict) and set(checks) == CHECKS and
                     all(v in ('clear', 'pending', 'unknown') for v in checks.values()), 'invalid host checks')
    children = payload['children']
    protocol.require(isinstance(children, list), 'children must be an array')
    seen = set()
    for child in children:
        protocol.require(isinstance(child, dict) and set(child) == {'session_id', 'parent_session_id', 'status'},
                         'invalid native child')
        for key in ('session_id', 'parent_session_id'):
            jobs.require_text(child[key], key)
        protocol.require(child['session_id'] not in seen and child['session_id'] != child['parent_session_id'] and
                         child['status'] in ('active', 'settled', 'unknown'), 'invalid or duplicate native child')
        seen.add(child['session_id'])
    parents = {c['session_id']: c['parent_session_id'] for c in children}
    root = state['native']['session_id']
    protocol.require(root not in parents, 'native lineage includes its own root')
    for session in parents:
        visited = set()
        while session != root:
            protocol.require(session in parents and session not in visited,
                             'native lineage must reach the recorded root without cycles')
            visited.add(session)
            session = parents[session]
    paths = payload['side_effect_paths']
    protocol.require(isinstance(paths, list) and all(isinstance(p, str) and Path(p).is_absolute() for p in paths),
                     'side_effect_paths must be absolute observation scopes')
    result = jobs.result_view(state)
    if payload['status'] == 'settled':
        protocol.require(state['resources'] is not None and state['native']['session_id'] is not None,
                         'settled host needs exact transport and native identity')
        protocol.require(result['status'] in ('success', 'error', 'blocked'),
                         'settled host needs a published response for this round')
        protocol.require(until > now and payload['disposition'] is not None and
                         all(v == 'clear' for v in checks.values()) and
                         all(c['status'] == 'settled' for c in children),
                         'settled host requires fresh clear checks and settled native children')
    else:
        protocol.require(payload['disposition'] is None, 'unsettled host cannot be reusable or exited')
    record = {k: v for k, v in payload.items() if k not in ('evidence_path', 'note')}
    record.update(version=1, **binding(state, result), target=target(state), recorded_at=now,
                  evidence=jobs.evidence(payload))
    state.setdefault('completion', {})['host'] = record


def summarize(state: dict[str, Any], result: dict[str, Any], now: float) -> dict[str, Any]:
    """Recheck evidence without inferring live health from a historical completed state."""
    facts = state.get('completion', {})
    identity = binding(state, result)
    published = result['status'] in ('success', 'error', 'blocked')
    reasons = []
    accepted = settled = False
    if result['status'] != 'success':
        reasons.append('current round has no valid success response')
    acceptance, host = facts.get('acceptance'), facts.get('host')
    review = acceptance_summary(state, result)
    accepted = review['status'] == 'accepted'
    if not accepted:
        reasons.append('acceptance: ' + (review['reason'] or 'verifier rejected the result'))
    if host is None:
        reasons.append('host observation is missing')
    else:
        try:
            protocol.require(host['version'] == 1 and all(host[k] == v for k, v in identity.items()) and
                             host['target'] == target(state), 'host observation identity changed')
            evidence_valid(host)
            protocol.require(host['status'] == 'settled' and host['disposition'] in ('reusable', 'exited') and
                             set(host['checks']) == CHECKS and all(v == 'clear' for v in host['checks'].values()) and
                             all(c['status'] == 'settled' for c in host['children']), 'host is not settled')
            protocol.require(host['observed_at'] <= now < host['valid_until'], 'host observation expired')
            settled = True
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            reasons.append(f'host: {exc}')
    return {'response_published': published, 'response_status': result['status'],
            'acceptance_status': review['status'],
            'accepted_by_verifier': accepted, 'host_settled': settled,
            'ready_to_complete': result['status'] == 'success' and accepted and settled,
            'acceptance_kind': acceptance.get('kind') if isinstance(acceptance, dict) else None,
            'host_status': host.get('status') if isinstance(host, dict) else 'unknown',
            'host_valid_until': host.get('valid_until') if isinstance(host, dict) else None,
            'reasons': reasons, 'executes_commands': False,
            'note': 'Host checks are scoped controller observations, not an OS guarantee. '
                    'Recheck live state before input; historical closure does not grant reuse.'}
