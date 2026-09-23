#!/usr/bin/env python3
"""Read-only herdr page planner; emitted argv never launches or focuses a page."""
import argparse
from collections.abc import Callable, Iterable
import json
import re
import subprocess
import sys
import unicodedata

import protocol


def select_layout(layout: str, parent_pane: str | None, parent_tab: str | None) -> str:
    """Resolve defaults from complete explicit identity, never from focus."""
    protocol.require(layout in ('auto', 'workspace', 'tab'), 'invalid layout')
    number = protocol.HERDR_PUBLIC_NUMBER
    if parent_pane is not None or parent_tab is not None:
        protocol.require(isinstance(parent_pane, str) and
                         re.fullmatch(f'w{number}:p{number}', parent_pane), 'exact parent pane required')
        protocol.require(isinstance(parent_tab, str) and
                         re.fullmatch(f'w{number}:t{number}', parent_tab) and
                         parent_pane.split(':')[0] == parent_tab.split(':')[0],
                         'exact matching parent tab required')
    protocol.require(layout != 'tab' or parent_pane is not None, 'tab needs exact parent pane/tab')
    return ('tab' if parent_pane else 'workspace') if layout == 'auto' else layout


def page_label(purpose: str, kind: str, opencode_mode: str | None = None,
               existing: Iterable[str] = ()) -> str:
    """Render a task-first title; disambiguate a snapshot of existing titles."""
    protocol.require(isinstance(purpose, str) and bool(purpose.strip()) and
                     not any(unicodedata.category(c).startswith('C') or c in '\u2028\u2029'
                             for c in purpose), 'task label required without controls')
    names = {'codex': 'Codex', 'hermes': 'Hermes', 'omp': 'omp', 'opencode': 'OpenCode'}
    protocol.require(kind in names, 'unknown host kind')
    protocol.require(opencode_mode in (None, 'omo', 'pure') and
                     (opencode_mode is None or kind == 'opencode'), 'invalid host profile')
    host = names[kind]
    if kind == 'opencode':
        host += ' (' + ('pure' if opencode_mode == 'pure' else 'OMO') + ')'
    base = purpose.strip() + ' · ' + host
    labels = set(existing)
    label, ordinal = base, 2
    while label in labels:
        label = f'{base} · {ordinal}'
        ordinal += 1
    return label


def verify_membership(pane: dict, pane_id: str, tab_id: str) -> None:
    """Check a live pane response against the exact expected tab and workspace."""
    protocol.require(pane.get('pane_id') == pane_id and pane.get('tab_id') == tab_id and
                     pane.get('workspace_id') == tab_id.split(':')[0],
                     'live pane/tab membership differs from explicit identity')


def read_herdr(argv: list[str]) -> dict:
    """Read metadata with bounded execution and explicit error envelopes."""
    result = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
    protocol.require(result.returncode == 0, 'herdr metadata read failed: ' + result.stderr.strip())
    value = protocol.parse_json(result.stdout)
    protocol.require(not value.get('error') and value.get('ok') is not False, 'herdr metadata error')
    return value.get('result', value)


def plan(session: str, cwd: str, purpose: str, kind: str, *, layout: str = 'auto',
         parent_pane: str | None = None, parent_tab: str | None = None,
         opencode_mode: str | None = None, read: Callable[[list[str]], dict] = read_herdr) -> dict:
    """Return an allocation plan after checking parent identity and current titles."""
    protocol.require(isinstance(session, str) and bool(session.strip()), 'explicit herdr session required')
    selected = select_layout(layout, parent_pane, parent_tab)
    page_label(purpose, kind, opencode_mode)  # Validate before any transport I/O.
    cli = ['herdr', '--session', session]
    if parent_pane:
        live = read(cli + ['pane', 'get', parent_pane])
        verify_membership(live['pane'], parent_pane, parent_tab)
    if selected == 'tab':
        container = ['--workspace', parent_pane.split(':')[0]]
        listed = read(cli + ['tab', 'list', *container])['tabs']
    else:
        container = []
        listed = read(cli + ['workspace', 'list'])['workspaces']
    protocol.require(isinstance(listed, list) and all(isinstance(item, dict) for item in listed),
                     'invalid page listing')
    label = page_label(purpose, kind, opencode_mode, [item.get('label', '') for item in listed])
    return {'layout': selected, 'label': label, 'mode': 'insider' if parent_pane else 'isolated',
            'parent_pane': parent_pane, 'parent_tab': parent_tab,
            'owns_tab': selected == 'tab', 'owns_workspace': selected == 'workspace',
            'create_argv': cli + [selected, 'create', *container, '--cwd', cwd,
                                  '--label', label, '--no-focus']}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True)
    parser.add_argument('--cwd', required=True)
    parser.add_argument('--purpose', required=True)
    parser.add_argument('--kind', required=True, choices=('codex', 'omp', 'hermes', 'opencode'))
    parser.add_argument('--opencode-mode', choices=('omo', 'pure'))
    parser.add_argument('--layout', choices=('auto', 'workspace', 'tab'), default='auto')
    parser.add_argument('--parent-pane')
    parser.add_argument('--parent-tab')
    args = parser.parse_args()
    try:
        print(json.dumps(plan(**vars(args)), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
