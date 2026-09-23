"""Page planning never allocates terminals or relies on current focus."""
import unittest

from test_token_tools import SCRIPTS  # noqa: F401
import pages


class PageTests(unittest.TestCase):
    def test_auto_layout_and_explicit_override(self):
        self.assertEqual(pages.select_layout('auto', 'w1:p1', 'w1:t1'), 'tab')
        self.assertEqual(pages.select_layout('workspace', 'w1:p1', 'w1:t1'), 'workspace')
        self.assertEqual(pages.select_layout('auto', None, None), 'workspace')
        for pane, tab in [('w1:p1', None), (None, 'w1:t1'), ('w1:p1', 'w2:t1'),
                          ('focused', 'w1:t1')]:
            with self.assertRaises(ValueError):
                pages.select_layout('auto', pane, tab)
        with self.assertRaises(ValueError):
            pages.select_layout('tab', None, None)

    def test_profiles_duplicates_and_literal_task(self):
        self.assertEqual(pages.page_label('修复上传', 'codex'), '修复上传 · Codex')
        self.assertEqual(pages.page_label('检查', 'opencode'), '检查 · OpenCode (OMO)')
        self.assertEqual(pages.page_label('检查', 'opencode', 'pure'), '检查 · OpenCode (pure)')
        self.assertEqual(pages.page_label('检查', 'hermes', existing=[
            '检查 · Hermes', '检查 · Hermes · 2']), '检查 · Hermes · 3')
        self.assertEqual(pages.page_label('$(touch /tmp/no) `literal`', 'omp'),
                         '$(touch /tmp/no) `literal` · omp')
        for label in ['', '  ', 'x\nY', 'x\x7f', 'x\x1b']:
            with self.assertRaises(ValueError):
                pages.page_label(label, 'codex')
        with self.assertRaises(ValueError):
            pages.page_label('检查', 'codex', 'pure')

    def test_plan_checks_actual_parent_membership_before_selecting_titles(self):
        calls = []
        def read(argv):
            calls.append(argv)
            if argv[-3:-1] == ['pane', 'get']:
                return {'pane': {'pane_id': 'w1:p1', 'workspace_id': 'w1', 'tab_id': 'w1:t2'}}
            self.fail('must reject mismatched live parent first')
        with self.assertRaisesRegex(ValueError, 'membership'):
            pages.plan('test', '/tmp', '修复', 'codex', parent_pane='w1:p1',
                       parent_tab='w1:t1', read=read)
        self.assertEqual(len(calls), 1)

    def test_tab_plan_uses_live_duplicates_and_explicit_argv(self):
        def read(argv):
            if 'pane' in argv:
                return {'pane': {'pane_id':'wZ:pA', 'workspace_id':'wZ', 'tab_id':'wZ:tB'}}
            self.assertEqual(argv[-4:], ['tab', 'list', '--workspace', 'wZ'])
            return {'tabs':[{'label':'验证 · Codex'}]}
        result = pages.plan('private', '/tmp', '验证', 'codex', parent_pane='wZ:pA',
                            parent_tab='wZ:tB', read=read)
        self.assertEqual(result['label'], '验证 · Codex · 2')
        self.assertEqual(result['create_argv'], ['herdr','--session','private','tab','create',
                         '--workspace','wZ','--cwd','/tmp','--label','验证 · Codex · 2','--no-focus'])
        self.assertTrue(result['owns_tab'])
        self.assertFalse(result['owns_workspace'])

    def test_isolated_plan_does_not_inspect_or_guess_a_parent(self):
        def read(argv):
            self.assertEqual(argv, ['herdr','--session','private','workspace','list'])
            return {'workspaces': []}
        result = pages.plan('private', '/tmp', '隔离检查', 'hermes', read=read)
        self.assertEqual(result['mode'], 'isolated')
        self.assertEqual(result['layout'], 'workspace')
        self.assertTrue(result['owns_workspace'])


if __name__ == '__main__':
    unittest.main()
