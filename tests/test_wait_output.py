"""Checkpoint log waits finish independently of the business process lifetime."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_token_tools import TOOL
import wait_output


class OutputWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'stream.log'

    def wait(self, **kwargs):
        return wait_output.wait_output(self.path, ['CHECKPOINT', 'FINISHED'], **kwargs)

    def test_returns_first_complete_match_and_resume_does_not_repeat_it(self):
        self.path.write_text('中文\nCHECKPOINT one\nFINISHED\n')
        first = self.wait(timeout=1)
        self.assertEqual(first['status'], 'matched')
        self.assertEqual(first['line'], 'CHECKPOINT one')
        self.assertEqual(first['after'], len('中文\nCHECKPOINT one\n'.encode()))
        second = self.wait(after=first['after'], identity=first['identity'], timeout=1)
        self.assertEqual(second['matched'], 'FINISHED')
        self.assertEqual(second['identity'], first['identity'])

    def test_partial_line_is_not_consumed_by_timeout(self):
        self.path.write_text('ready\nCHECKPOINT')
        first = self.wait(timeout=0)
        self.assertEqual(first['status'], 'timeout')
        self.assertEqual(first['after'], 6)
        with self.path.open('a') as stream:
            stream.write(' complete\n')
        self.assertEqual(self.wait(after=first['after'], identity=first['identity'], timeout=1)['line'],
                         'CHECKPOINT complete')

    def test_waits_for_file_and_partial_append(self):
        steps = [lambda: self.path.write_bytes(b'CHECK'),
                 lambda: self.path.write_bytes(b'CHECKPOINT\n')]
        with patch.object(wait_output.time, 'sleep', side_effect=lambda _: steps.pop(0)()):
            self.assertEqual(self.wait(timeout=1)['status'], 'matched')
        self.assertEqual(steps, [])

    def test_rejects_rotation_truncation_and_disappearance(self):
        for action in ('rotate', 'truncate', 'remove'):
            with self.subTest(action=action):
                self.path.write_text('original\n')
                def mutate(_):
                    if action == 'truncate':
                        self.path.write_bytes(b'')
                    else:
                        self.path.rename(self.path.with_suffix('.old'))
                        if action == 'rotate':
                            self.path.write_text('CHECKPOINT\n')
                with patch.object(wait_output.time, 'sleep', side_effect=mutate), \
                        self.assertRaises((ValueError, OSError)):
                    self.wait(timeout=1)

    def test_resume_rejects_changed_identity_and_invalid_offset(self):
        self.path.write_text('ready\n')
        first = self.wait(timeout=0)
        self.path.rename(self.path.with_suffix('.old'))
        self.path.write_text('CHECKPOINT\n')
        with self.assertRaises(ValueError):
            self.wait(after=first['after'], identity=first['identity'], timeout=1)
        for offset in (-1, 1, 999):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.wait(after=offset, timeout=0)

    def test_rejects_nonregular_files_and_symlinks_without_blocking(self):
        target = self.path.with_suffix('.target')
        target.write_text('CHECKPOINT\n')
        self.path.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            self.wait(timeout=0)
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises(ValueError):
            self.wait(timeout=0)

    def test_bounds_lines_and_requires_valid_utf8(self):
        for raw in (b'x' * 17, b'x' * 17 + b'\n', b'\xff\n'):
            self.path.write_bytes(raw)
            with patch.object(wait_output, 'MAX_LINE_BYTES', 16), self.assertRaises(ValueError):
                self.wait(timeout=0)

    def test_missing_log_timeout_and_argument_validation(self):
        result = self.wait(timeout=0)
        self.assertEqual((result['status'], result['after'], result['identity']), ('timeout', 0, None))
        for kwargs in ({'timeout': float('nan')}, {'timeout': -1}, {'interval': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.wait(**kwargs)
        for patterns in ([], [''], ['a\nb']):
            with self.subTest(patterns=patterns), self.assertRaises(ValueError):
                wait_output.wait_output(self.path, patterns, timeout=0)

    def test_cli_is_read_only_and_timeout_is_not_success(self):
        self.path.write_text('ready\n')
        cmd = [sys.executable, str(TOOL.with_name('wait_output.py')), '--path', str(self.path),
               '--contains', 'CHECKPOINT', '--timeout', '0']
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'timeout')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertEqual(self.path.read_text(), 'ready\n')
        self.path.write_text('CHECKPOINT\n')
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'matched')
