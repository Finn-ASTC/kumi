"""Generated publication commands work without copying a result destination."""
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
import io

from test_token_tools import ToolCase, TOOL, protocol


class ReportingRecipeTests(ToolCase):
    def publish_stdin(self, text):
        return subprocess.run([sys.executable, str(TOOL), 'write-result', '--request',
                               self.info['request_path'], '--input', '-'],
                              input=text, text=True, capture_output=True)

    def test_stdin_publishes_without_candidate_or_overwrite(self):
        before = set(Path(self.info['request_path']).parent.iterdir())
        response = self.response(output='Exact report $() `text` 中文')
        first = self.publish_stdin(json.dumps(response))
        self.assertEqual(first.returncode, 0, first.stderr)
        path = Path(self.info['result_path'])
        self.assertEqual(protocol.read_json(path), response)
        self.assertEqual(set(path.parent.iterdir()) - before, {path})
        original = path.read_bytes()
        self.assertNotEqual(self.publish_stdin(json.dumps(self.response(output='replace'))).returncode, 0)
        self.assertEqual(path.read_bytes(), original)

    def test_stdin_rejects_bad_identity_and_duplicate_keys_without_publishing(self):
        for value in (json.dumps(self.response(round_id='a' * 32)), '{"x":1,"x":2}', '[]'):
            with self.subTest(value=value):
                self.assertNotEqual(self.publish_stdin(value).returncode, 0)
                self.assertFalse(Path(self.info['result_path']).exists())

    def test_stdin_is_bounded_and_uses_the_strict_json_decoder(self):
        for raw in (b'x' * 33, b'{"x":NaN}', b'{"x":"\xff"}'):
            with self.subTest(raw=raw), patch.object(protocol, 'MAX_FILE_BYTES', 32), \
                    self.assertRaises((ValueError, UnicodeError)):
                protocol.read_response_stdin(io.BytesIO(raw))

    def test_generated_recipe_is_executable_and_keeps_nested_paths_as_data(self):
        root = self.root / 'nested "quote" $(touch NOT_ALLOWED) 中'
        root.mkdir()
        self.info = self.prepare(root=str(root))
        self.request = protocol.load_request(self.info['request_path'])
        prompt = Path(self.info['prompt_path']).read_text()
        # Recipe is a JSON argv, never a shell command assembled from paths.
        recipe, _ = json.JSONDecoder().raw_decode(prompt.split('PUBLICATION_ARGV=', 1)[1])
        result = subprocess.run(recipe, input=json.dumps(self.response()), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(recipe[recipe.index('--request') + 1]), Path(self.info['request_path']))
        self.assertEqual(protocol.read_json(self.info['result_path']), self.response())
        self.assertEqual(len(prompt.splitlines()), 1)
