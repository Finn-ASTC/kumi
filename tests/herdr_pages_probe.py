#!/usr/bin/env python3
"""Opt-in real herdr layout probe: private server, shell panes, no model calls."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/agent-orchestrator/scripts'
sys.path.insert(0, str(SCRIPTS))
import pages  # noqa: E402


def probe(root: Path, viewer: bool = False) -> dict:
    """Retain metadata evidence; terminate only the uniquely owned server."""
    lab = Path(tempfile.mkdtemp(prefix='kumi-pages-', dir=root.resolve()))
    session = lab.name
    cli = ['herdr', '--session', session]
    commands = []
    viewer_process = None
    original_window = None

    def desktop(argv: list[str]) -> str:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=True).stdout

    def viewer_text() -> str:
        return desktop(['kitty', '@', '--to', 'unix:' + str(lab / 'viewer.sock'),
                        'get-text', '--extent', 'screen'])

    def read(argv: list[str]) -> dict:
        value = pages.read_herdr(argv)
        commands.append({'argv': argv, 'result': value})
        return value

    with (lab / 'server.log').open('w') as log:
        server = subprocess.Popen(cli + ['server'], stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    read(cli + ['workspace', 'list'])
                    break
                except ValueError:
                    if server.poll() is not None or time.monotonic() > deadline:
                        raise
                    time.sleep(0.1)
            parent = read(cli + ['workspace', 'create', '--cwd', str(lab), '--label', '调度方'])
            pane = parent['root_pane']['pane_id']
            tab = parent['tab']['tab_id']
            workspace = parent['workspace']['workspace_id']
            read(cli + ['tab', 'rename', tab, '调度方'])
            if viewer:
                # Only IDs are retained from desktop state; never log unrelated window titles.
                focused = json.loads(desktop(['niri', 'msg', '-j', 'focused-window']))
                original_window = focused['id'] if focused else None
                viewer_process = subprocess.Popen(['kitty', '--config', 'NONE',
                    '--class', session, '--title', session,
                    '--override', 'allow_remote_control=socket-only',
                    '--listen-on', 'unix:' + str(lab / 'viewer.sock'),
                    'herdr', 'session', 'attach', session],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                deadline = time.monotonic() + 15
                while True:
                    try:
                        if '调度方' in viewer_text():
                            break
                    except subprocess.CalledProcessError:
                        pass
                    if viewer_process.poll() is not None or time.monotonic() > deadline:
                        raise AssertionError('test viewer did not render parent')
                    time.sleep(0.1)
                if original_window is not None:
                    desktop(['niri', 'msg', 'action', 'focus-window', '--id', str(original_window)])
            children = []
            for kind, profile in [('codex', None), ('codex', None), ('opencode', 'pure')]:
                # Exercise the installed-package CLI, not only the imported function.
                planned = subprocess.run([sys.executable, str(SCRIPTS / 'pages.py'),
                    '--session', session, '--cwd', str(lab), '--purpose', '验证页面', '--kind', kind,
                    '--parent-pane', pane, '--parent-tab', tab,
                    *(['--opencode-mode', profile] if profile else [])],
                    capture_output=True, text=True, timeout=15, check=True)
                plan = json.loads(planned.stdout)
                child = read(plan['create_argv'])
                pages.verify_membership(read(cli + ['pane', 'get', child['root_pane']['pane_id']])['pane'],
                                        child['root_pane']['pane_id'], child['tab']['tab_id'])
                assert child['tab']['workspace_id'] == workspace
                assert child['tab']['label'] == plan['label']
                assert plan['owns_tab'] and not plan['owns_workspace']
                children.append({'plan': plan, 'created': child})
            assert children[1]['plan']['label'] == '验证页面 · Codex · 2'
            assert children[2]['plan']['label'] == '验证页面 · OpenCode (pure)'
            before_cleanup = read(cli + ['workspace', 'get', workspace])
            assert before_cleanup['workspace']['active_tab_id'] == tab
            assert read(cli + ['pane', 'get', pane])['pane']['focused']
            if viewer:
                deadline = time.monotonic() + 5
                while True:
                    rendered = viewer_text()
                    if 'Codex' in rendered and 'Codex · 2' in rendered:
                        break
                    if time.monotonic() > deadline:
                        (lab / 'viewer.txt').write_text(rendered)
                        raise AssertionError('viewer did not render host labels')
                    time.sleep(0.1)
                (lab / 'viewer.txt').write_text(rendered)
                focused = json.loads(desktop(['niri', 'msg', '-j', 'focused-window']))
                if original_window is not None:
                    assert focused['id'] == original_window, 'background page creation changed desktop focus'
                # Narrow tab bars can hide the last host suffix; select only our own test tab.
                read(cli + ['tab', 'focus', children[2]['created']['tab']['tab_id']])
                deadline = time.monotonic() + 5
                while True:
                    selected_text = viewer_text()
                    if 'OpenCode (pure)' in selected_text:
                        break
                    if time.monotonic() > deadline:
                        (lab / 'viewer-selected.txt').write_text(selected_text)
                        raise AssertionError('selected tab did not render OpenCode profile')
                    time.sleep(0.1)
                (lab / 'viewer-selected.txt').write_text(selected_text)
                read(cli + ['tab', 'focus', tab])
            # Wrong same-workspace tab identity must reject without allocation.
            try:
                pages.plan(session, str(lab), '错误父页', 'codex', parent_pane=pane,
                           parent_tab=children[0]['created']['tab']['tab_id'], read=read)
            except ValueError:
                pass
            else:
                raise AssertionError('mismatched parent accepted')
            explicit = pages.plan(session, str(lab), '隔离验证', 'hermes', layout='workspace',
                                  parent_pane=pane, parent_tab=tab, read=read)
            separate = read(explicit['create_argv'])
            read(cli + ['tab', 'rename', separate['tab']['tab_id'], explicit['label']])
            assert separate['workspace']['workspace_id'] != workspace
            assert read(cli + ['pane', 'get', pane])['pane']['focused']
            for child in children:
                read(cli + ['tab', 'close', child['created']['tab']['tab_id']])
            read(cli + ['workspace', 'close', separate['workspace']['workspace_id']])
            remaining = read(cli + ['tab', 'list', '--workspace', workspace])['tabs']
            assert [item['tab_id'] for item in remaining] == [tab]
            assert read(cli + ['pane', 'get', pane])['pane']['focused']
            report = {'passed': True, 'session': session, 'children': children,
                      'before_cleanup': before_cleanup, 'remaining_tabs': remaining,
                      'model_calls': 0, 'desktop_viewer_verified': viewer,
                      'viewer_evidence': 'kitty rendered screen text; no screenshot or manual interaction' if viewer else None}
            (lab / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            return {'lab': str(lab), 'passed': True, 'desktop_viewer_verified': viewer}
        finally:
            (lab / 'commands.json').write_text(json.dumps(commands, ensure_ascii=False, indent=2) + '\n')
            if viewer_process is not None:
                viewer_process.terminate()
                try:
                    viewer_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    viewer_process.kill()
                    viewer_process.wait(timeout=5)
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--viewer', action='store_true', help='open own kitty viewer on niri; restore prior focus')
    args = parser.parse_args()
    print(json.dumps(probe(args.root, args.viewer), ensure_ascii=False))
