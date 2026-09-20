"""Published responses stay identifiable after edits, deletion and observer recovery."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_token_tools import ToolCase
import protocol
import reviews
import watch


class PublicationTests(ToolCase):
    def test_corrupt_pin_preserves_permission_observation_and_deduplication(self):
        handle = self.make_watch()
        protocol.publish(self.info['result_path'], self.response())
        self.poll(handle)
        pin = Path(self.info['request_path']).parent / 'publication.json'
        pin.write_text('{}')
        with patch.object(watch, 'observe', return_value={
                'state': 'working', 'screen': 'Command: inspect\nAllow once / Deny'}):
            events = watch.poll_watch(handle)['events']
            self.assertIn('observation_error', [e['kind'] for e in events])
            self.assertIn('attention', [e['kind'] for e in events])
            self.assertNotIn('result', [e['kind'] for e in events])
            self.assertEqual(watch.poll_watch(handle)['events'], [])
        self.assertEqual(pin.read_text(), '{}')

    def test_file_and_transport_errors_are_both_retained_without_healthy_timestamp(self):
        handle = self.make_watch()
        protocol.publish(self.info['result_path'], self.response())
        self.poll(handle)
        state_path = Path(handle).parent / 'state.json'
        checked = protocol.read_json(state_path)['targets'][self.info['round_id']]['last_successful_check_at']
        (Path(self.info['request_path']).parent / 'publication.json').write_text('{}')
        events = self.poll(handle, offline=True)
        event = next(e for e in events if e['kind'] == 'observation_error')
        snapshot = protocol.read_json(event['evidence_path'])
        self.assertIn('publication receipt', snapshot['file_error'])
        self.assertEqual(snapshot['transport_error'], 'offline')
        with patch.object(watch, 'observe', return_value={
                'state': 'working', 'screen': 'Command: inspect\nAllow once / Deny'}):
            self.assertIn('attention', [e['kind'] for e in watch.poll_watch(handle)['events']])
        self.assertEqual(protocol.read_json(state_path)['targets'][self.info['round_id']]
                         ['last_successful_check_at'], checked)

    def test_request_change_is_rejected_before_publication_or_terminal_io(self):
        handle = self.make_watch()
        path = Path(self.info['request_path'])
        original = path.read_bytes()
        for key, value in (('task', 'changed task'), ('cwd', '/tmp'), ('max_depth', 4),
                           ('prepared_at', '2026-01-01T00:00:00Z')):
            with self.subTest(key=key):
                request = json.loads(original)
                request[key] = value
                path.write_text(json.dumps(request))
                with patch.object(watch, 'observe', return_value={'state': 'idle', 'screen': 'ready'}) as observe:
                    result = watch.collect(protocol.read_json(handle)['targets'][0])
                self.assertIn('request', result.get('error', ''))
                self.assertEqual(observe.call_count, 0)
        path.write_bytes(original)
        self.assertNotIn('observation_error', [e['kind'] for e in self.poll(handle)])

    def test_legacy_watch_requires_recreation_without_inventing_request_pin(self):
        handle = self.make_watch()
        config = protocol.read_json(handle)
        config['targets'][0].pop('request_sha256', None)
        Path(handle).write_text(json.dumps(config))
        with patch.object(watch, 'observe', return_value={'state': 'idle', 'screen': 'ready'}) as observe:
            events = watch.poll_watch(handle)['events']
        self.assertTrue(any(e['kind'] == 'observation_error' and 'recreate' in e['message'] for e in events))
        self.assertEqual(observe.call_count, 0)
        self.assertNotIn('request_sha256', protocol.read_json(handle)['targets'][0])

    def make_watch(self):
        if not (Path(self.info['request_path']).parent / 'resources.json').exists():
            self.record()
        return watch.init_watch([self.info['request_path']], self.root)['watch_path']

    def poll(self, handle, offline=False):
        effect = OSError('offline') if offline else None
        with patch.object(watch, 'observe', side_effect=effect,
                          return_value={'state': 'idle', 'screen': 'ready'}):
            return watch.poll_watch(handle)['events']

    def test_rewrite_is_urgent_and_preserves_first_exact_response_across_watches(self):
        handle = self.make_watch()
        path = Path(self.info['result_path'])
        protocol.publish(path, self.response(output='original accepted answer'))
        original = path.read_bytes()
        self.poll(handle)
        path.write_text(json.dumps(self.response(output='different answer')))
        events = self.poll(handle, offline=True)
        self.assertIn('result_changed', [e['kind'] for e in events])
        self.assertNotIn('result', [e['kind'] for e in events])
        event = next(e for e in events if e['kind'] == 'result_changed')
        evidence = protocol.read_json(event['evidence_path'])['publication']
        first = protocol.read_json(evidence['path'])
        self.assertEqual(first['result_text'].encode(), original)
        self.assertEqual(first['sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(evidence['current_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        pending = reviews.pending(handle)['action_required']
        self.assertEqual(next(x for x in pending if x['seq'] == event['seq'])['priority'], 0)
        self.assertNotIn('result_changed', [e['kind'] for e in self.poll(handle)])
        new = self.make_watch()
        self.assertIn('result_changed', [e['kind'] for e in self.poll(new)])
        result = reviews.record_review(handle, event['seq'], 'handled', 'Reconciled; follow-up uses a new round', 0)
        self.assertEqual(result['review']['status'], 'handled')

    def test_invalid_before_first_valid_does_not_pin_but_later_invalid_and_missing_alert(self):
        handle = self.make_watch()
        path = Path(self.info['result_path'])
        path.write_text('{"unfinished":')
        self.assertIn('invalid_result', [e['kind'] for e in self.poll(handle)])
        path.write_text(json.dumps(self.response()))
        self.assertIn('result', [e['kind'] for e in self.poll(handle)])
        original = path.read_bytes()
        path.write_text('{"unfinished":')
        self.assertIn('result_changed', [e['kind'] for e in self.poll(handle)])
        path.unlink()
        self.assertIn('result_changed', [e['kind'] for e in self.poll(handle)])
        self.assertNotIn('result_changed', [e['kind'] for e in self.poll(handle)])
        path.write_bytes(original)
        self.assertIn('result_changed', [e['kind'] for e in self.poll(handle)])
        self.assertNotIn('result', [e['kind'] for e in self.poll(handle)])

    def test_formatting_only_rewrite_and_blocked_or_error_publications_are_immutable(self):
        for status in ('success', 'blocked', 'error'):
            with self.subTest(status=status):
                self.info = self.prepare()
                self.request = protocol.load_request(self.info['request_path'])
                handle = self.make_watch()
                path = Path(self.info['result_path'])
                value = self.response(status=status, error='failure' if status == 'error' else None,
                                      blocked_reason='decision' if status == 'blocked' else None)
                protocol.publish(path, value)
                self.poll(handle)
                path.write_text(json.dumps(value, separators=(',', ':')))
                self.assertIn('result_changed', [e['kind'] for e in self.poll(handle)])

    def test_corrupt_pin_is_an_observation_error_and_never_replaced(self):
        handle = self.make_watch()
        protocol.publish(self.info['result_path'], self.response())
        first = next(e for e in self.poll(handle) if e['kind'] == 'result')
        pin = protocol.read_json(first['evidence_path']).get('publication', {}).get('path')
        self.assertIsNotNone(pin)
        path = Path(pin)
        path.write_text('{}')
        events = self.poll(handle)
        self.assertIn('observation_error', [e['kind'] for e in events])
        self.assertEqual(path.read_text(), '{}')

    def test_fresh_round_does_not_inherit_predecessor_pin(self):
        handle = self.make_watch()
        protocol.publish(self.info['result_path'], self.response())
        self.poll(handle)
        self.info = self.prepare(cwd=None, parent_depth=None, previous=self.info['request_path'])
        self.request = protocol.load_request(self.info['request_path'])
        handle = self.make_watch()
        protocol.publish(self.info['result_path'], self.response(output='new round answer'))
        kinds = [e['kind'] for e in self.poll(handle)]
        self.assertIn('result', kinds)
        self.assertNotIn('result_changed', kinds)

    def test_concurrent_watches_share_one_unchanged_pin(self):
        first, second = self.make_watch(), self.make_watch()
        protocol.publish(self.info['result_path'], self.response())
        with patch.object(watch, 'observe', return_value={'state': 'idle', 'screen': 'ready'}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                observed = list(pool.map(watch.poll_watch, (first, second)))
        publications = []
        for output in observed:
            self.assertNotIn('observation_error', [e['kind'] for e in output['events']])
            event = next(e for e in output['events'] if e['kind'] == 'result')
            publications.append(protocol.read_json(event['evidence_path'])['publication'])
        self.assertEqual(publications[0], publications[1])
