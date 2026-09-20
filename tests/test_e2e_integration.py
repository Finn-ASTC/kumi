"""Opt-in real tmux E2E runner exercise; deterministic peers, no models or credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.environ.get('ORCH_RUN_TMUX_TESTS') == '1', 'opt-in private tmux socket')
class RunnerIntegrationTests(unittest.TestCase):
    def test_two_targets_recover_rework_verify_and_close_without_duplicate_submission(self):
        with tempfile.TemporaryDirectory(prefix='orch-runner-e2e-') as directory:
            result = subprocess.run([sys.executable,str(Path(__file__).with_name('e2e_demo.py')),
                '--root',directory],capture_output=True,text=True,timeout=45)
            self.assertEqual(result.returncode,0,result.stderr + result.stdout)
            report = json.loads(result.stdout)
            self.assertEqual(report['native_models_launched'],0)
            self.assertEqual(report['received_rounds'],{'author':3,'verifier':1})
            self.assertFalse(report['rejected_attempt']['passed'])
            self.assertTrue(report['accepted_attempt']['passed'])
            self.assertTrue(report['old_waiting_user_recovered'])
            self.assertTrue(report['sentinel_preserved'])
            self.assertEqual(report['closed_jobs'],2)
            self.assertEqual(report['pending_counts'],{'open':0,'waiting_user':0})
            self.assertTrue(report['source_seed_unchanged'])


if __name__ == '__main__':
    unittest.main()
