#!/usr/bin/env python3
"""Audit retained attention/review timing, without observing terminals or changing queues."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any

import jobs
import protocol
import reviews
import runs


FAILURES = (OSError, ValueError, KeyError, TypeError)


def evidence_status(reference: dict[str, str] | None) -> str:
    """Check retained bytes, never interpret a screenshot/log as action authorization."""
    if reference is None:
        return 'absent'
    try:
        actual = hashlib.sha256(protocol.read_bytes(reference['path'])).hexdigest()
        return 'verified' if actual == reference['sha256'] else 'changed'
    except FAILURES:
        return 'unavailable'


def distribution(values: list[float]) -> dict[str, Any]:
    """Nearest-rank percentiles on observed samples, with no imputation for missing data."""
    ordered = sorted(values)
    def percentile(fraction: float) -> float | None:
        return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None
    return {'samples': len(ordered), 'p50': percentile(.5), 'p95': percentile(.95),
            'max': ordered[-1] if ordered else None}


def event_audit(watch_path: str, event: dict[str, Any], now: float, threshold: float) -> dict[str, Any]:
    """Separate first review, action-evidence recording and declared resolution times.

    Event detection is not dialog appearance; review recording is not a tool
    action timestamp. Older resolved receipts survive in history but cannot make
    a subsequently reopened event resolved again.
    """
    directory, _ = reviews.context(watch_path)
    latest = reviews.latest_review(directory, event)
    detected = jobs.timestamp(event['observed_at'])
    protocol.require(detected <= now, 'event observed in the future')
    receipts = []
    pinned_receipts = []
    previous = detected
    for revision in range(1, latest['revision'] + 1):
        path = directory / 'reviews' / f"{event['seq']:012d}" / f'{revision:012d}.json'
        raw = protocol.read_bytes(path)
        value = protocol.parse_json(raw.decode('utf-8'))
        pinned_receipts.append((path, hashlib.sha256(raw).hexdigest()))
        recorded = jobs.timestamp(value['recorded_at'])
        protocol.require(previous <= recorded <= now, 'review clock moved backwards or into the future')
        previous = recorded
        receipts.append(value)
    # Revalidate the immutable chain after reads, detecting concurrent append/rewrite.
    protocol.require(reviews.latest_review(directory, event) == latest, 'reviews changed during audit; retry')
    detail = reviews.event_detail(watch_path, event['seq'])
    protocol.require(detail['event'] == event and detail['review'] == latest, 'event or reviews changed during audit')
    protocol.require(all(jobs.fingerprint(path) == sha for path, sha in pinned_receipts),
                     'earlier review changed during audit; retry')
    issues = []
    for value in receipts:
        for field in ('action_evidence', 'resolution_evidence'):
            status = evidence_status(value.get(field))
            if status not in ('absent', 'verified'):
                issues.append({'revision': value['revision'], 'field': field, 'status': status})
    first_review = receipts[0]['recorded_at'] if receipts else None
    # Only a verified reference is eligible for a measured action-evidence endpoint.
    action = next((value for value in receipts if value.get('action_evidence') is not None
                   and evidence_status(value['action_evidence']) == 'verified'), None)
    resolution = latest.get('resolved_at') if evidence_status(latest.get('resolution_evidence')) == 'verified' else None
    wait_seconds = 0.0
    for index, value in enumerate(receipts):
        if value['status'] == 'waiting_user':
            end = receipts[index + 1]['recorded_at'] if index + 1 < len(receipts) else now
            wait_seconds += end - value['recorded_at']
    review_seconds = first_review - detected if first_review is not None else None
    review_state = ('within_target' if review_seconds <= threshold else 'late') if review_seconds is not None else (
        'overdue_unreviewed' if now - detected > threshold else 'pending')
    integrity = detail['integrity']
    return {'event_ref': f"{detail['watch_id']}:{event['seq']}",
            'watch_path': watch_path, 'seq': event['seq'], 'job_id': event['job_id'], 'round_id': event['round_id'],
            'issue_id': event.get('issue_id'), 'correlation_key': event.get('correlation_key'),
            'detected_at': detected, 'review_revision': latest['revision'], 'review_status': latest['status'],
            'first_review_recorded_at': first_review,
            'first_action_evidence_recorded_at': action['recorded_at'] if action else None,
            'latest_declared_resolution_at': resolution,
            'detection_to_first_review_seconds': review_seconds,
            'detection_to_action_evidence_record_seconds': action['recorded_at'] - detected if action else None,
            'detection_to_latest_resolution_seconds': resolution - detected if resolution is not None else None,
            'age_without_resolution_evidence_seconds': now - detected if resolution is None else None,
            'declared_waiting_user_seconds': wait_seconds,
            'review_target_status': review_state, 'resolution_supported': resolution is not None,
            'event_evidence_integrity': integrity, 'evidence_issues': issues,
            'historical_resolution_records': sum(v.get('resolved_at') is not None for v in receipts),
            'actual_action_at': None, 'dialog_appearance_at': None,
            'native_behavior_verified': False}


def audit(run_path: str | Path, threshold: float = 30, limit: int = 20, offset: int = 0,
          now: float | None = None) -> dict[str, Any]:
    """Audit all retained run watches, including handled and historical-round events.

    Aggregate before pagination. Failures remain explicit and preclude an overall
    passing verdict. Never join independent incidents by matching prompt text.
    """
    protocol.require(type(threshold) in (float, int) and math.isfinite(threshold) and threshold > 0,
                     'invalid review target')
    protocol.require(type(limit) is int and 1 <= limit <= 200 and type(offset) is int and offset >= 0,
                     'invalid audit pagination')
    current = jobs.clock(now)
    recovered = runs.recover(run_path, now=current)
    errors = [{'scope': 'recovery', 'path': value.get('path'), 'error': 'run recovery incomplete'}
              for value in recovered['errors']]
    records = []
    candidates = 0
    for watcher in recovered['watches']:
        path = Path(watcher['watch_path'])
        try:
            registration = Path(recovered['run']['run_path']).parent / 'watch-registry' / (watcher['watch_id'] + '.json')
            membership = protocol.read_json(registration)
            manifest_sha = jobs.fingerprint(path)
            protocol.require(membership['watch_path'] == str(path) and membership['config_sha256'] == manifest_sha
                             and membership['watch_id'] == watcher['watch_id']
                             and membership['run_id'] == recovered['run']['run_id'], 'watch membership changed')
            directory, identities = reviews.context(str(path))
            paths = sorted((directory / 'events').glob('*.json'))
            protocol.require((directory / 'events').is_dir() and not (directory / 'events').is_symlink(),
                             'event directory missing or redirected')
            protocol.require(all(p.name == f'{number:012d}.json' for number, p in enumerate(paths, 1)),
                             'event history missing or gapped')
            state = protocol.read_json(directory / 'state.json')
            # The producer may have appended since recovery; pin one committed inventory.
            protocol.require(type(state['cursor']) is int and state['cursor'] == len(paths),
                             'event history and state disagree; retry')
            for event_path in paths:
                event = reviews.load_event(directory, identities, int(event_path.stem))
                if event['kind'] != 'attention':
                    continue
                candidates += 1
                try:
                    records.append(event_audit(str(path), event, current, threshold))
                except FAILURES:
                    errors.append({'scope': 'event', 'watch_id': watcher['watch_id'], 'seq': event['seq'],
                                   'error': 'attention evidence or review history invalid; inspect private records'})
            protocol.require(paths == sorted((directory / 'events').glob('*.json')),
                             'events appended during audit; retry')
            protocol.require(jobs.fingerprint(path) == manifest_sha and protocol.read_json(registration) == membership,
                             'watch membership changed during audit; retry')
        except FAILURES:
            errors.append({'scope': 'watch', 'watch_id': watcher['watch_id'],
                           'error': 'watch inventory invalid or changed; inspect private records and retry'})
    records.sort(key=lambda r: (r['detected_at'], r['event_ref']))
    counts = {key: sum(r['review_target_status'] == key for r in records)
              for key in ('within_target', 'late', 'pending', 'overdue_unreviewed')}
    counts.update(attention_records=len(records), candidates_in_readable_watches=candidates,
                  resolution_supported=sum(r['resolution_supported'] for r in records),
                  handled_without_resolution=sum(r['review_status'] == 'handled' and not r['resolution_supported'] for r in records),
                  legacy_unpinned=sum(r['event_evidence_integrity'] != 'pinned' for r in records),
                  evidence_errors=sum(bool(r['evidence_issues']) for r in records))
    complete = not errors and not counts['evidence_errors']
    verdict = ('incomplete' if not complete else 'no_samples' if not records else
               'missed' if counts['late'] or counts['overdue_unreviewed'] else
               'pending' if counts['pending'] else 'within_target')
    metrics = ('detection_to_first_review_seconds', 'detection_to_action_evidence_record_seconds',
               'detection_to_latest_resolution_seconds')
    return {'version': 1, 'run_id': recovered['run']['run_id'], 'checked_at': current,
            'review_target_seconds': threshold, 'review_recording_verdict': verdict,
            'complete': complete, 'counts': counts,
            'distributions': {field: distribution([r[field] for r in records if r[field] is not None]) for field in metrics},
            'records': records[offset:offset + limit], 'next_offset': offset + len(records[offset:offset + limit]),
            'more_records': offset + limit < len(records), 'errors': errors,
            'executes_commands': False, 'changes_review_state': False,
            'approval_response_sla_verified': False, 'history_completeness_verified': False,
            'note': 'Retained attention records, not unique approvals. Matching correlations are not merged. '
                    'Time starts at observer detection, not dialog appearance. Action-evidence recording is not action time. '
                    'Resolution is a controller declaration with checked evidence bytes, not independent native verification. '
                    'Waiting-user time is declared queue time and is not subtracted. Missing endpoints are unknown. '
                    'Legacy event evidence is unpinned; deleted review tails have no independent head watermark. '
                    'No_samples is not a pass. Pagination does not limit aggregates.'}


def main() -> int:
    """Emit an advisory JSON report; exit 1 for missed/pending, 2 for incomplete data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--review-target-seconds', type=float, default=30)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--offset', type=int, default=0)
    args = parser.parse_args()
    try:
        result = audit(args.run, args.review_target_seconds, args.limit, args.offset)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 2 if not result['complete'] else 0 if result['review_recording_verdict'] == 'within_target' else 1
    except FAILURES:
        print(json.dumps({'complete': False, 'error': 'audit failed; inspect private run records and arguments'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
