"""Regression guard against repository-only dependencies in distributed skills."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

try:
    import check_skill_package as guard
except ModuleNotFoundError:
    guard = None


class SkillPackageTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(guard, 'package reference guard is missing')
        temporary = tempfile.TemporaryDirectory(prefix='orch-package-')
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / 'skills'
        self.skill = self.root / 'worker'
        self.skill.mkdir(parents=True)
        self.document = self.skill / 'SKILL.md'
        self.document.write_text('# Worker\n')

    def check(self, text):
        self.document.write_text(text)
        return guard.check_package(self.root)

    def test_original_requirement_is_found_with_its_actual_line_number(self):
        report = self.check('# Transport\n\nWorkspace/tab syntax was checked; '
                            'runtime integration needs the scenario checks in the README.\n')
        self.assertEqual([(f['kind'], f['line']) for f in report['findings']], [('prose_review', 3)])

    def test_code_samples_and_reporting_substring_are_not_prose_dependencies(self):
        for marker in ('```', '~~~~'):
            with self.subTest(marker=marker):
                report = self.check(f'{marker}text\nSee the README and [old](../../README.md).\n'
                                    f'{marker}\nUse `the README` as sample text.\n'
                                    'Follow the reporting rules; run long tests/builds.\n'
                                    'Upstream: https://example.test/docs/the-README\n')
                self.assertEqual(report['findings'], [])
                self.assertEqual(report['local_links'], 0)

    def test_inline_code_uses_matching_delimiters(self):
        self.assertEqual(self.check('Example: ``literal `the README` text``.\n')['findings'], [])

    def test_reference_in_prose_after_code_keeps_original_line_number(self):
        report = self.check('```\nSee the README.\n```\n\nRead the project README.\n')
        self.assertEqual([f['line'] for f in report['findings']], [5])

    def test_inline_path_in_reading_instruction_is_not_hidden_as_example_code(self):
        for text in ('Read `README.md` first.', 'See `docs/setup.md`.',
                     'Consult the `README`.', '检查结果见 `docs/result.md`。',
                     'Runtime checks are in the `README`.'):
            with self.subTest(text=text):
                self.assertEqual(self.check(text)['findings'][0]['kind'], 'prose_review')

    def test_relative_reference_within_sibling_skill_is_allowed(self):
        sibling = self.root / 'controlled'
        sibling.mkdir()
        (sibling / 'SKILL.md').write_text('# Controlled\n')
        report = self.check('Read [the README](../controlled/SKILL.md#rules).\n')
        self.assertEqual(report['findings'], [])
        self.assertEqual(report['local_links'], 1)

    def test_explicit_link_disambiguates_an_inline_path_in_its_label(self):
        (self.skill / 'guide.md').write_text('# Guide\n')
        report = self.check('[Read `README.md`](guide.md)\n')
        self.assertEqual(report['findings'], [])
        self.assertEqual(report['local_links'], 1)

    def test_missing_and_escaping_links_are_rejected_even_when_external_file_exists(self):
        (self.parent / 'README.md').write_text('not distributed')
        report = self.check('[missing](absent.md) and [outside](../../README.md)\n')
        self.assertEqual([f['kind'] for f in report['findings']], ['missing_target', 'outside_package'])

    def test_absolute_and_file_uri_links_are_not_portable_even_inside_current_tree(self):
        for target in (str(self.document), self.document.as_uri()):
            with self.subTest(target=target):
                report = self.check(f'[current]({target})\n')
                self.assertEqual(report['findings'][0]['kind'], 'absolute_target')

    def test_symlink_and_url_encoded_traversal_cannot_escape_package(self):
        outside = self.parent / 'README.md'
        outside.write_text('outside')
        (self.skill / 'linked.md').symlink_to(outside)
        report = self.check('[symlink](linked.md) [encoded](%2e%2e/%2e%2e/README.md)\n')
        self.assertEqual(sum(f['kind'] == 'outside_package' for f in report['findings']), 2)

    def test_reference_definitions_and_angle_paths_are_checked(self):
        (self.skill / 'local guide.md').write_text('# Local\n')
        report = self.check('[guide](<local guide.md>)\n[bad][outside]\n\n'
                            '[outside]: ../../README.md "not distributed"\n')
        self.assertEqual(report['local_links'], 1)
        self.assertEqual([(f['kind'], f['line']) for f in report['findings']], [('outside_package', 4)])

    def test_external_sources_and_self_anchors_do_not_require_local_files(self):
        report = self.check('[upstream](https://example.test/docs/start) [mail](mailto:a@example.test) '
                            '[here](#worker)\n')
        self.assertEqual(report['findings'], [])
        self.assertEqual(report['local_links'], 0)

    def test_parent_metadata_is_not_included_in_the_distributed_skills(self):
        (self.root / 'PROVENANCE.md').write_text('See the README and [tools](../tools/check.py).')
        report = self.check('# Worker\n')
        self.assertEqual(report['findings'], [])
        self.assertEqual(report['documents'], 1)
        report = self.check('[metadata](../PROVENANCE.md)')
        self.assertEqual(report['findings'][0]['kind'], 'outside_package')

    def test_cli_exit_status_and_json_are_suitable_for_ci(self):
        for text, expected in (('# Worker\n', 0), ('Read the README.\n', 1)):
            self.document.write_text(text)
            result = subprocess.run([sys.executable, str(Path(guard.__file__)), '--root', str(self.root)],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual(bool(json.loads(result.stdout)['findings']), bool(expected))

    def test_missing_or_empty_package_is_an_error_not_a_green_scan(self):
        for root in (self.parent / 'missing', self.parent):
            with self.subTest(root=root), self.assertRaises(ValueError):
                guard.check_package(root)


if __name__ == '__main__':
    unittest.main()
