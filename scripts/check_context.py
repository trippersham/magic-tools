#!/usr/bin/env python3
"""Check that AGENTS.md stays honest against the real tree.

The single-source-of-truth gate for the agent contract. Every path the repo map
and command table name must exist on disk, and every path the Planned section
names must not exist yet. This linter fails the build when either drifts: a
mapped path that is gone, or a planned path that has since been created. Run last
by `scripts/check`.

Phase-1 tolerance: `AGENTS.md` is not authored until Phase 2, so a tree without
it is reported clean (exit 0) rather than as an error.
"""

import argparse
import re
import shlex
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

CODE_SPAN = re.compile(r'(?<!`)`([^`]+)`(?!`)')
LINK = re.compile(r'\]\((?:<([^>]+)>|([^\s)]+))(?:\s+[^)]*)?\)')
PATH = re.compile(r'(?:[\w.+ -]+/)*[\w.+ -]+/?')
FILE = re.compile(r'[\w.+ -]*\.[A-Za-z][\w+-]*')
SEPARATOR = re.compile(r'\|[\s|:-]+\|')


def code_paths(text, *, map_cell=False):
    """Find repo-root-relative code paths, including shell command operands."""
    for span in CODE_SPAN.findall(text):
        if map_cell:
            yield span
            continue
        try:
            words = shlex.split(span)
        except ValueError:
            continue
        for word in words:
            if word.startswith('-'):
                _, equals, word = word.partition('=')
                if not equals:
                    continue
            word = word.rstrip(';')
            if PATH.fullmatch(word) and ('/' in word or FILE.fullmatch(word)):
                yield word


def links(text):
    for angle, plain in LINK.findall(text):
        target = urlsplit(angle or plain)
        if target.path and not (target.scheme or target.netloc):
            yield unquote(target.path)


def present(root, path):
    candidate = root / path
    return candidate.resolve().is_relative_to(root) and candidate.exists()


def check(root):
    root = root.resolve()
    agents = root / 'AGENTS.md'
    if not agents.exists():
        # Phase 1: AGENTS.md is authored in Phase 2. A tree without it is clean.
        return
    lines = agents.read_text(encoding='utf-8').splitlines()
    if len(lines) >= 200:
        yield 'AGENTS.md must be under 200 lines'

    section = ''
    map_rows = 0
    for number, line in enumerate(lines, 1):
        if line.startswith('## '):
            section = line
        cells = line.strip().strip('|').split('|')
        is_table_row = line.startswith('|') and line.endswith('|')

        # Planned section: every path named here must not exist yet.
        if section == '## Planned':
            for path in sorted(set(code_paths(line)) | set(links(line))):
                if present(root, path):
                    yield f'AGENTS.md:{number}: {path} is planned but already exists'
            continue

        # Repo-map rows: the first cell is a real path; check it and count the row.
        if is_table_row and section == '## Repo map':
            if cells[0].strip() == 'Path' or SEPARATOR.fullmatch(line):
                continue
            marked = set(code_paths(cells[0], map_cell=True))
            if not marked:
                yield f'AGENTS.md:{number}: repo-map row needs a path in an inline code span'
            else:
                map_rows += 1
            for path in sorted(marked):
                if not present(root, path):
                    yield f'AGENTS.md:{number}: missing path {path}'
            line = cells[1] if len(cells) > 1 else ''

        # Every backticked path or relative link in the rest of the line must exist.
        for path in sorted(set(code_paths(line)) | set(links(line))):
            if not present(root, path):
                yield f'AGENTS.md:{number}: missing path {path}'

    if not map_rows:
        yield 'AGENTS.md needs a Repo map with at least one path row'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        findings = list(check(args.root))
    except (OSError, UnicodeError, ValueError) as error:
        findings = [str(error)]
    if findings:
        for finding in findings:
            print(f'context: FAIL: {finding}', file=sys.stderr)
        return 1
    print('context: PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
