#!/usr/bin/env python3
"""Validate a pull request body against the make-magic PR template.

The machine-checked half of the PR contract. A conformant body carries the six
H2 sections of `.github/pull_request_template.md` in order, references an issue
from the Tasks section with `Closes #N` or `Part of #N`, and ends with the
attribution footer `Posted on behalf of <name> by <tool and model>` (with the
`<...>` placeholders replaced). This linter fails the build (exit 1) when any of
those is missing, and the `pr-body` job in `.github/workflows/ci.yml` runs it on
every PR event so the body re-validates on edits.

HTML comments and fenced code blocks are stripped before the checks, so the
template's own guidance comments never count as filled content. Reads a GitHub
`pull_request` event JSON via `--event` (how CI invokes it) or a plain body file
path (for local use).
"""

import argparse
import json
import re
import sys
from pathlib import Path

HEADINGS = ('Tasks', 'What changed', 'Proof', 'Limits', 'Release impact', 'Agent context')
ISSUE = re.compile(r' {0,3}(?:[-*] )?(?:Closes|Part of) #[1-9]\d*\b')
FOOTER = re.compile(r'Posted on behalf of (?!<)\S.*')
FENCE = re.compile(r' {0,3}(`{3,}|~{3,})(.*)')
HEADING = re.compile(r' {0,3}##\s+(.*)')


def visible_lines(body):
    """Yield non-blank lines with HTML comments and fenced code blocks removed."""
    fence = ''
    body = re.sub(r'<!--.*?(?:-->|\Z)', '', body, flags=re.DOTALL)
    for line in body.splitlines():
        match = FENCE.fullmatch(line)
        if fence:
            if match and match[1].startswith(fence) and not match[2].strip():
                fence = ''
        elif match:
            fence = match[1]
        elif line.strip():
            yield line.rstrip()


def check(body):
    """Yield one finding per contract violation; yield nothing for a clean body."""
    section = ''
    headings = []
    has_issue = False
    last_line = ''
    for line in visible_lines(body):
        last_line = line
        if match := HEADING.fullmatch(line):
            section = match[1]
            headings.append(section)
        elif section == 'Tasks' and ISSUE.match(line):
            has_issue = True
    if tuple(headings) != HEADINGS:
        yield f'H2 headings must appear in order: {", ".join(HEADINGS)}'
    if not has_issue:
        yield 'Tasks must include a Closes #N or Part of #N issue reference'
    if not FOOTER.fullmatch(last_line):
        yield "the last line must start with 'Posted on behalf of ' and name the author"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('body_file', type=Path, nargs='?', help='a plain PR-body file')
    mode.add_argument('--event', type=Path, help='GitHub pull_request event JSON file')
    args = parser.parse_args()
    try:
        body = (args.event or args.body_file).read_text(encoding='utf-8')
        if args.event:
            body = json.loads(body)['pull_request']['body']
        findings = list(check('' if body is None else body))
    except (OSError, ValueError, KeyError, TypeError) as error:
        findings = [str(error)]
    if findings:
        for finding in findings:
            print(f'PR: FAIL: {finding}', file=sys.stderr)
        return 1
    print('PR: PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
