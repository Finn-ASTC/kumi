#!/usr/bin/env python3
"""Check packaged Markdown links and known ambiguous repository references.

This is a development regression guard, not a complete Markdown parser or a
semantic proof of self-containment. External documentation is not fetched.
"""
import argparse
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit


FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
INLINE = re.compile(r'(?<!`)(`+)(?!`)([^\n]*?)(?<!`)\1(?!`)')
LINK = re.compile(r'\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))[^)\n]*\)')
DEFINITION = re.compile(r'^ {0,3}\[[^\]\n]+\]:\s*(?:<([^>\n]+)>|([^\s]+))[^\n]*', re.M)
PROSE = re.compile(
    r'\bthe\s+(?:project\s+)?README\b|\bthe\s+repo(?:sitory)?\b'
    r'|\b(?:see|read|consult|from)\s+(?:docs|tests)/'
    r'|(?:参见|见|读取|阅读)\s*(?:docs|tests)/', re.I)
READING_PREFIX = re.compile(
    r'(?:\b(?:see|read|consult|from)\s+(?:the\s+)?|\bin\s+the\s+'
    r'|(?:参见|见|读取|阅读)\s*)$', re.I)
PACKAGE_PATH = re.compile(r'(?:README(?:\.md)?|(?:\.\./)*(?:README(?:\.md)?|(?:docs|tests)/\S+))$', re.I)
URL = re.compile(r'(?:https?://|mailto:)\S+', re.I)


def blank(text: str) -> str:
    """Mask content without changing offsets or source line numbers."""
    return ''.join('\n' if char == '\n' else ' ' for char in text)


def outside_code_blocks(raw: str) -> str:
    """Mask top-level backtick/tilde fences and indented code."""
    output = []
    fence = None
    for line in raw.splitlines(keepends=True):
        match = FENCE.match(line.rstrip('\r\n'))
        if fence is not None:
            output.append(blank(line))
            if (match and match[1][0] == fence[0] and len(match[1]) >= len(fence)
                    and not match[2].strip()):
                fence = None
        elif match:
            fence = match[1]
            output.append(blank(line))
        elif line.startswith(('    ', '\t')):
            output.append(blank(line))
        else:
            output.append(line)
    return ''.join(output)


def check_package(root: Path) -> dict:
    """Check only immediate child directories containing SKILL.md.

    Parent metadata such as vault PROVENANCE.md is not part of the package.
    Local link targets must exist inside one of these distributed directories.
    Prose findings request review of known wording, not assert semantic escape.
    """
    root = root.resolve()
    if not root.is_dir():
        raise ValueError('package root must be an existing directory')
    skills = sorted(p for p in root.iterdir() if p.is_dir() and (p / 'SKILL.md').is_file())
    if not skills:
        raise ValueError('package root has no child skill directories')
    names = {p.name for p in skills}
    findings = []
    local_links = documents = 0

    def contained(path: Path) -> bool:
        resolved = path.resolve()
        relative = resolved.relative_to(root) if resolved.is_relative_to(root) else None
        return relative is not None and bool(relative.parts) and relative.parts[0] in names

    def finding(path: Path, line: int, kind: str, detail: str) -> None:
        findings.append({'path': str(path.relative_to(root)), 'line': line, 'kind': kind, 'detail': detail})

    for skill in skills:
        for path in sorted(skill.rglob('*.md')):
            if not contained(path):
                finding(path, 1, 'document_outside_package', 'document resolves outside distributed directories')
                continue
            raw = path.read_text(encoding='utf-8')
            documents += 1
            blocks = outside_code_blocks(raw)
            text = INLINE.sub(lambda m: blank(m[0]), blocks)
            references = sorted([*LINK.finditer(text), *DEFINITION.finditer(text)], key=lambda m: m.start())
            for match in references:
                target = match[1] if match[1] is not None else match[2]
                line = raw.count('\n', 0, match.start()) + 1
                parsed = urlsplit(target)
                if parsed.scheme.lower() in ('http', 'https', 'mailto'):
                    continue
                filename = unquote(parsed.path)
                if not filename and not parsed.scheme and not parsed.netloc:
                    continue  # Self anchor; anchor existence is not checked.
                if parsed.scheme == 'file' or parsed.netloc or Path(filename).is_absolute():
                    finding(path, line, 'absolute_target', target)
                elif parsed.scheme:
                    finding(path, line, 'unsupported_uri', target)
                elif not contained(path.parent / filename):
                    finding(path, line, 'outside_package', target)
                elif not (path.parent / filename).exists():
                    finding(path, line, 'missing_target', target)
                else:
                    local_links += 1

            # A valid explicit link disambiguates its label (e.g. "the README").
            # Invalid targets were already reported above.
            prose = LINK.sub(lambda m: blank(m[0]), text)
            prose = DEFINITION.sub(lambda m: blank(m[0]), prose)
            prose = URL.sub(lambda m: blank(m[0]), prose)
            review_lines = {number for number, line in enumerate(prose.splitlines(), 1) if PROSE.search(line)}
            # Backticks do not turn "Read `README.md`" into an illustrative code
            # sample. Check explicit reading instructions before masking spans.
            for match in INLINE.finditer(blocks):
                if any(reference.start() <= match.start() < reference.end() for reference in references):
                    continue
                prefix = blocks[:match.start()].rsplit('\n', 1)[-1]
                if PACKAGE_PATH.fullmatch(match[2].strip()) and READING_PREFIX.search(prefix):
                    review_lines.add(raw.count('\n', 0, match.start()) + 1)
            for line in sorted(review_lines):
                finding(path, line, 'prose_review', raw.splitlines()[line - 1].strip())

    return {'root': str(root), 'skills': len(skills), 'documents': documents,
            'local_links': local_links, 'findings': findings,
            'scope': 'Local Markdown destinations and known prose patterns; manual review still required.'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1] / 'skills',
                        help='parent of distributed skill directories; defaults to repository skills/')
    args = parser.parse_args()
    try:
        report = check_package(args.root)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report['findings'] else 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
