#!/usr/bin/env python3
"""Assert a pull request's base ref is still a live branch, not a dead one.

The stale-base guard (design §10a A3). A native stack parent lives in GitHub's
PR base ref, not in PR-body text, so this is a `gh`-API check, not a text parse.
A PR is fine when its base is `main` OR the head ref of a still-OPEN PR (a live
stack parent). It is stale when the base is a branch that merged or was deleted —
then the PR sits on ground that no longer exists and its diff is wrong. This
linter fails the build (exit 1) in that case so the drift is a red check, not a
silent surprise.

The `base-guard` job in `.github/workflows/ci.yml` runs it on PR events only,
feeding `--base "$GITHUB_BASE_REF"` and the open-PR head refs it lists with `gh`.
The decision itself is pure (`decide`) and offline; only the gathering needs `gh`.
"""

import argparse
import sys
from pathlib import Path

_MAIN = 'main'


def decide(base_ref: str, open_pr_head_refs: set[str]) -> list[str]:
    """Yield findings (empty == pass). Base must be main or a live open-PR head."""
    base = (base_ref or '').strip()
    if not base:
        return ['no base ref supplied — cannot verify the PR base is live']
    if base == _MAIN:
        return []
    if base in open_pr_head_refs:
        return []
    return [
        f'PR base {base!r} is not {_MAIN!r} and not the head of any open PR — it is a '
        'merged or deleted branch. Rebase this PR onto main (or onto its still-open '
        'stack parent) so it lands on live ground.'
    ]


def _read_open_heads(path: str | None) -> set[str]:
    """Open-PR head refs, one per line (the shape `gh pr list --json headRefName
    --jq '.[].headRefName'` prints). Missing file == no open PRs."""
    if not path:
        return set()
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True, help='the PR base ref (GITHUB_BASE_REF)')
    parser.add_argument('--open-heads-file', help='file of open-PR head refs, one per line')
    args = parser.parse_args()
    findings = decide(args.base, _read_open_heads(args.open_heads_file))
    if findings:
        for finding in findings:
            print(f'base: FAIL: {finding}', file=sys.stderr)
        return 1
    print('base: PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
