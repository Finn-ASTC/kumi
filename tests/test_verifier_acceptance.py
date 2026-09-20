"""Execute the documented verifier-job recipe, including a false-green oracle."""

import json
from pathlib import Path
import sys

from test_token_tools import ToolCase
import delivery
import jobs
import protocol


class VerifierAcceptanceTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.record()
        self.store = self.root / 'delivery-store'
        self.index = self.root / 'index'
        state = jobs.register(self.index, self.info['request_path'])
        self.state = jobs.claim(self.index, self.info['job_id'], state['revision'], 'controller', 300)
        self.token = self.state['lease']['token']
        # Run the actual JSON examples published to skill readers.
        doc = (Path(__file__).resolve().parents[1] /
               'skills/agent-orchestrator/references/completion.md').read_text()
        section = doc.split('### Accepting a verification round', 1)[1].split('## Scoped host observation', 1)[0]
        examples = section.split('```json\n')[1:]
        self.plan, self.acceptance_template = [json.loads(value.split('```', 1)[0]) for value in examples]
        self.proof = self.root / 'controller-review.txt'
        self.proof.write_text('Controller checked the oracle against independent known-good and wrong totals.')

    def verifier_attempt(self, oracle):
        baseline = delivery.capture(self.info['request_path'], ['.'], ['work'],
                                    'Before verifier starts', root=self.store)
        (self.cwd / 'oracle.py').write_text(oracle)
        (self.cwd / 'REPORT.md').write_text('Verification report to be independently checked.')
        protocol.publish(self.info['result_path'], self.response(files_created=['oracle.py', 'REPORT.md']))
        snapshot = delivery.capture(self.info['request_path'], ['.'], ['work'],
                                    'Verifier handed off its files', root=self.store,
                                    baseline=baseline['snapshot_path'])
        self.assertEqual(snapshot['warnings'], [])
        return delivery.verify(snapshot['snapshot_path'], self.plan, root=self.store)

    def accept(self, attempt):
        payload = {**self.acceptance_template, 'attempt_path': attempt,
                   'evidence_path': str(self.proof)}
        self.state = jobs.change(self.index, self.info['job_id'], self.state['revision'], self.token,
                                 'accept', payload)

    def test_documented_plan_rejects_an_oracle_that_accepts_everything(self):
        attempt = self.verifier_attempt('raise SystemExit(0)\n')
        self.assertFalse(attempt['passed'])
        self.assertEqual(attempt['commands'][0]['exit_code'], 1)
        self.assertIn('oracle exits: 0 0', Path(attempt['commands'][0]['stdout']['path']).read_text())
        with self.assertRaisesRegex(ValueError, 'passed'):
            self.accept(attempt['attempt_path'])
        self.assertFalse(jobs.recover(self.index, self.info['job_id'])['completion']['accepted_by_verifier'])

    def test_verifier_uses_its_own_attempt_and_still_needs_host_settling(self):
        attempt = self.verifier_attempt(
            'import json, sys\nfrom pathlib import Path\n'
            'value = json.loads(Path(sys.argv[1]).read_text())\n'
            'raise SystemExit(0 if value == {"total": 6} else 1)\n')
        self.assertTrue(attempt['passed'])
        self.assertIn('oracle exits: 0 1', Path(attempt['commands'][0]['stdout']['path']).read_text())
        author_cwd = self.root / 'author'
        author_cwd.mkdir()
        (author_cwd / 'artifact.txt').write_text('6')
        author = self.prepare(cwd=str(author_cwd))
        protocol.publish(author['result_path'], self.response(job_id=author['job_id'],
            round_id=author['round_id'], files_created=['artifact.txt']))
        other_snapshot = delivery.capture(author['request_path'], ['.'], [], 'Author handoff', root=self.store)
        other = delivery.verify(other_snapshot['snapshot_path'], {
            'commands': [{'argv': [sys.executable, '-c', 'print("author check passed")'], 'timeout_seconds': 5}],
            'artifacts': [], 'dependencies': {}, 'environment': {}}, root=self.store)
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.accept(other['attempt_path'])
        self.accept(attempt['attempt_path'])
        completion = jobs.recover(self.index, self.info['job_id'])['completion']
        self.assertTrue(completion['accepted_by_verifier'])
        self.assertFalse(completion['host_settled'])
        self.assertFalse(completion['ready_to_complete'])
