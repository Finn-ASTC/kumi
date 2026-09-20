"""Malformed local file channels cannot hold the observer or mix result versions."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from test_token_tools import ToolCase, SCRIPTS
import jobs
import protocol
import watch


class ReadBoundaryTests(ToolCase):
    def bounded_python(self, source, *args):
        try:
            return subprocess.run([sys.executable, '-c', source, str(SCRIPTS), *map(str, args)],
                                  capture_output=True, text=True, timeout=3)
        except subprocess.TimeoutExpired:
            self.fail('local file read blocked beyond the process watchdog')

    def test_json_reader_rejects_fifo_without_waiting_for_writer(self):
        path = self.root / 'blocked.json'
        os.mkfifo(path)
        result = self.bounded_python('''
import sys
sys.path.insert(0, sys.argv[1])
import protocol
try:
    protocol.read_json(sys.argv[2])
except (OSError, ValueError) as exc:
    print(str(exc))
else:
    raise AssertionError('FIFO accepted as JSON')
''', path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('regular file', result.stdout)

    def test_json_reader_rejects_links_and_directories(self):
        plain = self.root / 'plain.json'
        plain.write_text('{}')
        link = self.root / 'link.json'
        link.symlink_to(plain)
        for path in (link, self.root):
            with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                protocol.read_json(path)
        self.assertEqual(protocol.read_json(plain), {})

    def test_oversized_json_is_rejected(self):
        path = self.root / 'large.json'
        path.write_text('{"value":"' + 'x' * 1024 + '"}')
        with patch.object(protocol, 'MAX_FILE_BYTES', 1024, create=True):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                protocol.read_json(path)

    def test_growth_after_stat_is_still_bounded(self):
        path = self.root / 'growing.json'
        path.write_text('{}')
        original = os.fstat

        def grow(fd):
            info = original(fd)
            path.write_text('{"value":"' + 'x' * 1024 + '"}')
            return info

        with patch.object(protocol, 'MAX_FILE_BYTES', 1024, create=True), \
                patch.object(protocol.os, 'fstat', side_effect=grow):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                protocol.read_json(path)

    def test_bad_channel_does_not_block_later_sweeps_or_duration(self):
        self.record()
        first = self.info
        self.info = self.prepare()
        self.record(agent='healthy', pane='w1:p3')
        healthy = self.info
        for filename in ('result.json', 'publication.json', 'request.json', 'resources.json'):
            with self.subTest(filename=filename):
                handle = watch.init_watch([first['request_path'], healthy['request_path']], self.root)['watch_path']
                path = Path(first['request_path']).parent / filename
                original = path.read_bytes() if path.exists() else None
                if path.exists():
                    path.unlink()
                os.mkfifo(path)
                try:
                    result = self.bounded_python('''
import sys
sys.path.insert(0, sys.argv[1])
import watch
handle = sys.argv[2]
checks = {}
def observe(target):
    key = target['round_id']
    checks[key] = checks.get(key, 0) + 1
    return {'state':'working', 'screen':'work' if checks[key] == 1 else 'Command: check\\nAllow once / Deny'}
watch.observe = observe
sys.argv = ['watch.py', 'run', '--watch', handle, '--interval', '0.02', '--duration', '0.12']
raise SystemExit(watch.main())
''', handle)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    outputs = [json.loads(line) for line in result.stdout.splitlines()]
                    self.assertTrue(outputs[-1]['observer_stopped'])
                    events = [e for output in outputs for e in output.get('events', [])]
                    self.assertTrue(any(e['round_id'] == healthy['round_id'] and e['kind'] == 'attention'
                                        for e in events), events)
                    self.assertTrue(any(e['round_id'] == first['round_id'] and e['kind'] == 'observation_error'
                                        for e in events), events)
                finally:
                    path.unlink()
                    if original is not None:
                        path.write_bytes(original)

    def test_result_view_uses_bytes_that_were_validated(self):
        state = jobs.register(self.root / 'jobs', self.info['request_path'])
        path = Path(self.info['result_path'])
        protocol.publish(path, self.response(output='original answer'))
        original = path.read_bytes()
        validate = protocol.validate_result

        def replace_after_validation(request, result, *args, **kwargs):
            value = validate(request, result, *args, **kwargs)
            path.write_text('{"invalid":true}')
            return value

        with patch.object(protocol, 'validate_result', side_effect=replace_after_validation):
            view = jobs.result_view(state)
        self.assertEqual(view['status'], 'success')
        self.assertEqual(view['output'], 'original answer')
        self.assertEqual(view['sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(jobs.result_view(state)['status'], 'invalid')
