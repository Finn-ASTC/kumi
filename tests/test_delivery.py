"""F05 delivery inputs, source isolation and immutable verification evidence."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from test_token_tools import ToolCase
import protocol


class DeliveryTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.store = self.root / 'deliveries'
        self.store.mkdir()
        (self.cwd / 'value.txt').write_text('version one')

    def capture(self, **kwargs):
        import delivery
        return delivery.capture(self.info['request_path'], ['.'], [], 'Writer yielded this scope',
                                root=self.store, **kwargs)

    def plan(self, code='print("verified")', **kwargs):
        return {'commands': [{'argv': [sys.executable, '-c', code], 'timeout_seconds': 5}],
                'artifacts': [], 'dependencies': {'python': sys.version.split()[0]},
                'environment': {}, **kwargs}

    def verify(self, snapshot, plan=None):
        import delivery
        return delivery.verify(snapshot['snapshot_path'], plan or self.plan(), root=self.store)

    def test_no_git_snapshot_remains_fixed_after_author_changes(self):
        import delivery
        first = self.capture()
        (self.cwd / 'value.txt').write_text('version two')
        self.assertTrue(delivery.check(first['snapshot_path'])['valid'])
        self.assertEqual((Path(first['source_path']) / 'value.txt').read_text(), 'version one')
        self.assertIsNone(protocol.read_json(first['snapshot_path'])['actual_diff'])
        self.assertNotEqual(os.stat(self.cwd / 'value.txt').st_ino,
                            os.stat(Path(first['source_path']) / 'value.txt').st_ino)

    def test_capture_reports_each_missing_comparison_input(self):
        initial = self.capture()
        self.assertEqual({w['code'] for w in initial['warnings']}, {'no_baseline', 'no_result'})
        self.assertEqual(protocol.read_json(initial['snapshot_path'])['warnings'], initial['warnings'])
        diff_only = self.capture(baseline=initial['snapshot_path'])
        manifest = protocol.read_json(diff_only['snapshot_path'])
        self.assertEqual([w['code'] for w in diff_only['warnings']], ['no_result'])
        self.assertIsNotNone(manifest['actual_diff'])
        self.assertIsNone(manifest['declaration_comparison'])

        (self.cwd / 'value.txt').write_text('changed')
        protocol.publish(self.info['result_path'], self.response(files_modified=['value.txt']))
        unbased = self.capture()
        manifest = protocol.read_json(unbased['snapshot_path'])
        self.assertEqual([w['code'] for w in unbased['warnings']], ['no_baseline'])
        self.assertIsNotNone(manifest['declarations'])
        self.assertIsNone(manifest['actual_diff'])
        self.assertIsNone(manifest['declaration_comparison'])
        complete = self.capture(baseline=initial['snapshot_path'])
        self.assertEqual(complete['warnings'], [])
        comparison = protocol.read_json(complete['snapshot_path'])['declaration_comparison']
        for key in ('undeclared_changes', 'declared_without_scoped_change',
                    'outside_snapshot_scope', 'category_mismatches'):
            self.assertEqual(comparison[key], [])

    def test_capture_cli_exposes_the_sealed_warnings(self):
        import delivery
        result = self.run_tool('delivery.py', 'capture', '--request', self.info['request_path'],
                               '--include', '.', '--handoff', 'Initial baseline', '--root', self.store)
        self.assertEqual(result.returncode, 0, result.stderr)
        captured = json.loads(result.stdout)
        _, manifest = delivery.load_snapshot(captured['snapshot_path'])
        self.assertEqual({w['code'] for w in captured['warnings']}, {'no_baseline', 'no_result'})
        self.assertEqual(captured['warnings'], manifest['warnings'])
        self.assertTrue(delivery.check(captured['snapshot_path'])['valid'])

    def test_legacy_snapshot_without_warnings_can_still_be_a_baseline(self):
        import delivery
        captured = self.capture()
        path = Path(captured['snapshot_path'])
        manifest = protocol.read_json(path)
        manifest.pop('warnings', None)
        # Re-create the legacy shape and its matching seal in this test fixture.
        path.write_text(json.dumps(manifest))
        path.with_name('snapshot-seal.json').write_text(json.dumps({'sha256': delivery.file_record(path)['sha256']}))
        self.assertTrue(delivery.check(path)['valid'])
        protocol.publish(self.info['result_path'], self.response())
        current = self.capture(baseline=path)
        self.assertEqual(current['warnings'], [])

    def test_copy_race_leaves_failed_attempt_without_published_snapshot(self):
        import delivery
        original = delivery.copy_tree
        def racing(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.cwd / 'value.txt').write_text('racing writer')
            return result
        with patch.object(delivery, 'copy_tree', side_effect=racing), self.assertRaisesRegex(ValueError, 'changed|stable'):
            self.capture()
        self.assertEqual(list(self.store.glob('*/snapshot.json')), [])
        self.assertEqual(len(list(self.store.glob('*/failure.json'))), 1)

    def test_scoped_snapshot_includes_untracked_files_and_excludes_git_and_cache(self):
        import delivery
        (self.cwd / '.git').mkdir()
        (self.cwd / '.git/config').write_text('git metadata')
        (self.cwd / 'cache').mkdir()
        (self.cwd / 'cache/noise').write_text('not a source input')
        info = delivery.capture(self.info['request_path'], ['.'], ['cache'], 'Scoped handoff', root=self.store)
        files = protocol.read_json(info['snapshot_path'])['files']
        self.assertEqual(set(files), {'value.txt'})
        self.assertFalse((Path(info['source_path']) / '.git').exists())

    def test_external_symlink_and_special_file_rejected(self):
        (self.cwd / 'escape').symlink_to(self.task)
        with self.assertRaises(ValueError):
            self.capture()
        (self.cwd / 'escape').unlink()
        os.mkfifo(self.cwd / 'pipe')
        with self.assertRaises(ValueError):
            self.capture()

    def test_internal_relative_link_and_executable_preserved(self):
        import delivery
        script = self.cwd / 'script'
        script.write_text('#!/bin/sh\nprintf ok')
        script.chmod(0o755)
        (self.cwd / 'alias').symlink_to('script')
        info = self.capture()
        source = Path(info['source_path'])
        self.assertEqual(os.readlink(source / 'alias'), 'script')
        self.assertTrue(os.access(source / 'script', os.X_OK))
        self.assertTrue(delivery.check(info['snapshot_path'])['valid'])

    def test_reject_unsafe_scope_and_destination_inside_source(self):
        import delivery
        for include in (['../outside'], ['/etc'], ['missing'], []):
            with self.subTest(include=include), self.assertRaises(ValueError):
                delivery.capture(self.info['request_path'], include, [], 'handoff', root=self.store)
        with self.assertRaises(ValueError):
            delivery.capture(self.info['request_path'], ['.'], [], 'handoff', root=self.cwd)

    def test_baseline_diff_is_separate_from_child_declarations(self):
        first = self.capture()
        (self.cwd / 'value.txt').write_text('actually modified')
        (self.cwd / 'new.txt').write_text('undeclared addition')
        protocol.publish(self.info['result_path'], self.response(files_modified=['value.txt']))
        latest = self.capture(baseline=first['snapshot_path'])
        manifest = protocol.read_json(latest['snapshot_path'])
        self.assertEqual(manifest['actual_diff']['added'], ['new.txt'])
        self.assertEqual(manifest['actual_diff']['modified'], ['value.txt'])
        self.assertEqual(manifest['declarations']['files_created'], [])
        self.assertEqual(manifest['declaration_comparison']['undeclared_changes'], ['new.txt'])

    def test_snapshot_corruption_prevents_any_command_execution(self):
        import delivery
        info = self.capture()
        source = Path(info['source_path']) / 'value.txt'
        source.chmod(0o644)
        source.write_text('tampered')
        self.assertFalse(delivery.check(info['snapshot_path'])['valid'])
        with self.assertRaises(ValueError):
            self.verify(info)
        self.assertEqual(list(self.store.glob('verify-*')), [])

    def test_manifest_corruption_fails_before_paths_are_used(self):
        import delivery
        info = self.capture()
        path = Path(info['snapshot_path'])
        manifest = protocol.read_json(path)
        manifest['source_path'] = str(self.cwd)
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            delivery.check(path)

    def test_concurrent_variants_have_distinct_outputs_and_caches(self):
        one = self.capture()
        (self.cwd / 'value.txt').write_text('version two')
        two = self.capture()
        code = ('import os,pathlib; p=pathlib.Path; '
                '(p(os.environ["ORCH_BUILD_DIR"])/"artifact").write_text(p("value.txt").read_text()); '
                '(p(os.environ["XDG_CACHE_HOME"])/"marker").write_text("cache")')
        plan = self.plan(code, artifacts=['build/artifact'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda s: self.verify(s, plan), (one, two)))
        self.assertTrue(all(r['passed'] for r in results))
        self.assertNotEqual(results[0]['attempt_path'], results[1]['attempt_path'])
        values = [(Path(r['attempt_path']).parent / 'build/artifact').read_text() for r in results]
        self.assertEqual(values, ['version one', 'version two'])
        self.assertNotEqual(results[0]['artifacts']['build/artifact']['sha256'],
                            results[1]['artifacts']['build/artifact']['sha256'])

    def test_failure_and_rerun_use_new_evidence_and_retain_exit_code(self):
        info = self.capture()
        failed = self.verify(info, self.plan('import sys; print("original failure"); sys.exit(7)'))
        path = Path(failed['attempt_path']).parent / 'result.json'
        before = path.read_bytes()
        passed = self.verify(info)
        self.assertFalse(failed['passed'])
        self.assertEqual(failed['commands'][0]['exit_code'], 7)
        self.assertTrue(passed['passed'])
        self.assertNotEqual(failed['attempt_path'], passed['attempt_path'])
        self.assertEqual(path.read_bytes(), before)
        self.assertIn('original failure', Path(failed['commands'][0]['stdout']['path']).read_text())

    def test_source_mutation_during_check_is_not_reported_as_verified(self):
        info = self.capture()
        result = self.verify(info, self.plan('from pathlib import Path; Path("value.txt").write_text("changed")'))
        self.assertEqual(result['commands'][0]['exit_code'], 0)
        self.assertFalse(result['source_unchanged'])
        self.assertFalse(result['passed'])
        self.assertEqual((Path(info['source_path']) / 'value.txt').read_text(), 'version one')

    def test_temporary_source_rewrite_then_restore_is_not_clean_verification(self):
        info = self.capture()
        code = ('from pathlib import Path; p=Path("value.txt"); old=p.read_bytes(); '
                'p.write_text("temporary compiler input"); p.write_bytes(old)')
        result = self.verify(info, self.plan(code))
        self.assertFalse(result['source_unchanged'])
        self.assertFalse(result['passed'])
        self.assertTrue(result['source_changes'][0]['metadata_changed'])

    def test_verification_root_cannot_be_inside_the_sealed_copy(self):
        import delivery
        info = self.capture()
        with self.assertRaises(ValueError):
            delivery.verify(info['snapshot_path'], self.plan(), root=info['source_path'])
        self.assertTrue(delivery.check(info['snapshot_path'])['valid'])

    def test_deleted_files_and_wrong_declaration_category_are_visible(self):
        first = self.capture()
        (self.cwd / 'value.txt').unlink()
        (self.cwd / 'new.txt').write_text('created, not modified')
        protocol.publish(self.info['result_path'], self.response(files_modified=['new.txt'], files_deleted=['value.txt']))
        manifest = protocol.read_json(self.capture(baseline=first['snapshot_path'])['snapshot_path'])
        self.assertEqual(manifest['warnings'], [])
        self.assertEqual(manifest['actual_diff']['deleted'], ['value.txt'])
        self.assertEqual(manifest['declaration_comparison']['category_mismatches'], ['new.txt'])

    def test_verification_records_resolved_executable_fingerprint(self):
        result = self.verify(self.capture())
        executable = result['commands'][0]['executable']
        self.assertEqual(executable['path'], str(Path(sys.executable).resolve()))
        self.assertEqual(len(executable['sha256']), 64)

    def test_timeout_missing_artifact_and_missing_program_are_retained_failures(self):
        info = self.capture()
        plan = self.plan('import time; time.sleep(30)')
        plan['commands'][0]['timeout_seconds'] = .05
        result = self.verify(info, plan)
        self.assertFalse(result['passed'])
        self.assertEqual(result['commands'][0]['outcome'], 'timeout')
        self.assertFalse(self.verify(info, self.plan(artifacts=['build/missing']))['passed'])
        plan['commands'][0]['argv'] = ['/nonexistent/orch-program']
        result = self.verify(info, plan)
        self.assertFalse(result['passed'])
        self.assertEqual(result['commands'][0]['outcome'], 'launch_error')

    def test_invalid_plan_or_artifact_paths_never_execute(self):
        info = self.capture()
        plans = [self.plan(artifacts=['../outside']), self.plan(artifacts=['/tmp/a']),
                 self.plan(environment={'HOME': '/tmp'}), self.plan(commands=[])]
        for plan in plans:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                self.verify(info, plan)

    def test_argv_metacharacters_arrive_literally_and_context_is_pinned(self):
        info = self.capture()
        literal = '$(touch NEVER) `false` 中文'
        plan = self.plan()
        plan['commands'][0]['argv'] = [sys.executable, '-c', 'import sys; print(sys.argv[1])', literal]
        result = self.verify(info, plan)
        self.assertTrue(result['passed'])
        self.assertEqual(Path(result['commands'][0]['stdout']['path']).read_text().strip(), literal)
        manifest = protocol.read_json(result['attempt_path'])
        self.assertEqual(manifest['plan']['dependencies'], plan['dependencies'])
        self.assertIn('platform', manifest['runtime'])
        self.assertFalse((self.cwd / 'NEVER').exists())

    def test_inspection_detects_changed_artifacts_and_keeps_failed_attempts_distinct(self):
        import delivery
        info = self.capture()
        plan = self.plan('import os,pathlib; (pathlib.Path(os.environ["ORCH_BUILD_DIR"])/"a").write_text("artifact")',
                         artifacts=['build/a'])
        result = self.verify(info, plan)
        inspected = delivery.inspect(result['attempt_path'])
        self.assertTrue(inspected['complete'])
        self.assertTrue(inspected['passed'])
        (Path(result['attempt_path']).parent / 'build/a').write_text('changed after verification')
        inspected = delivery.inspect(result['attempt_path'])
        self.assertFalse(inspected['passed'])
        self.assertTrue(inspected['errors'])
        failed = self.verify(info, self.plan('raise SystemExit(8)'))
        checked = delivery.inspect(failed['attempt_path'])
        self.assertTrue(checked['complete'])
        self.assertTrue(checked['evidence_valid'])
        self.assertFalse(checked['passed'])

    def test_inspection_reports_incomplete_attempt_without_inventing_a_verdict(self):
        import delivery
        info = self.capture()
        result = self.verify(info)
        (Path(result['attempt_path']).parent / 'result.json').unlink()
        inspected = delivery.inspect(result['attempt_path'])
        self.assertFalse(inspected['complete'])
        self.assertIsNone(inspected['passed'])

    def test_relative_path_search_uses_verification_cwd(self):
        (self.cwd / 'tools').mkdir()
        executable = self.cwd / 'tools/local-check'
        executable.write_text('#!/bin/sh\nprintf local-project-check')
        executable.chmod(0o755)
        info = self.capture()
        plan = self.plan(commands=[{'argv': ['local-check'], 'timeout_seconds': 2}],
                         environment={'PATH': 'tools'})
        result = self.verify(info, plan)
        self.assertTrue(result['passed'])
        self.assertEqual(Path(result['commands'][0]['stdout']['path']).read_text(), 'local-project-check')
        self.assertTrue(Path(result['commands'][0]['executable']['path']).is_relative_to(
            Path(result['attempt_path']).parent / 'source'))

    def test_inspection_rejects_redirected_source_even_when_bytes_match(self):
        import delivery
        info = self.capture()
        result = self.verify(info)
        source = Path(result['attempt_path']).parent / 'source'
        source.rename(source.with_name('original-source'))
        source.symlink_to(self.cwd, target_is_directory=True)
        self.assertFalse(delivery.inspect(result['attempt_path'])['passed'])

    def test_cli_capture_check_and_verify_exit_status(self):
        captured = self.run_tool('delivery.py', 'capture', '--request', self.info['request_path'],
            '--include', '.', '--handoff', 'Writer yielded scope', '--root', self.store)
        self.assertEqual(captured.returncode, 0, captured.stderr)
        info = json.loads(captured.stdout)
        checked = self.run_tool('delivery.py', 'check', '--snapshot', info['snapshot_path'])
        self.assertEqual(checked.returncode, 0, checked.stderr)
        plan = self.root / 'plan.json'
        plan.write_text(json.dumps(self.plan('raise SystemExit(4)')))
        failed = self.run_tool('delivery.py', 'verify', '--snapshot', info['snapshot_path'], '--plan', plan,
                               '--root', self.store)
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertEqual(json.loads(failed.stdout)['commands'][0]['exit_code'], 4)
