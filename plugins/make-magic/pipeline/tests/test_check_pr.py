"""Contract tests for the PR-body linter `scripts/check_pr.py`.

The linter is the machine-checked half of the PR contract, wired into CI's
`pr-body` job: a conformant body carries the six H2 sections of the PR template
in order, references an issue with `Closes #N` or `Part of #N`, and ends with
the attribution footer. It fails (exit 1) when any of those is missing. These
tests pin the three contract cases: a conformant body passes, a footer-less body
fails naming the footer, and a body without an issue reference fails.

The linter lives at the repo root (not in this package), so it is exercised as a
subprocess against a synthetic body file, which is exactly how `scripts/check`
and CI invoke it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# repo root = .../plugins/make-magic/pipeline/tests/this-file -> parents[4]
_REPO_ROOT = Path(__file__).resolve().parents[4]
_LINTER = _REPO_ROOT / 'scripts' / 'check_pr.py'

_CONFORMANT_BODY = """## Tasks

- Closes #42

## What changed

Grafted the check spine into CI and added the PR-body gate.

## Proof

Ran ./scripts/check — green end-to-end.

## Limits

None.

## Release impact

None.

## Agent context

Moved two rows out of Planned into the repo map; context linter PASS.

---

Posted on behalf of Tripp Wickersham by Claude Code (Opus 4.8)
"""


def _run(body_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_LINTER), str(body_file)],
        capture_output=True,
        text=True,
    )


def test_conformant_body_passes(tmp_path: Path) -> None:
    body = tmp_path / 'body.md'
    body.write_text(_CONFORMANT_BODY, encoding='utf-8')
    result = _run(body)
    assert result.returncode == 0, result.stderr
    assert 'PR: PASS' in result.stdout


def test_footerless_body_fails_naming_footer(tmp_path: Path) -> None:
    body = tmp_path / 'body.md'
    # Drop the attribution footer, keeping the six sections and the issue line.
    footerless = _CONFORMANT_BODY.replace(
        'Posted on behalf of Tripp Wickersham by Claude Code (Opus 4.8)\n',
        'Shipped it.\n',
    )
    body.write_text(footerless, encoding='utf-8')
    result = _run(body)
    assert result.returncode == 1
    assert 'Posted on behalf of' in result.stderr


def test_missing_issue_reference_fails(tmp_path: Path) -> None:
    body = tmp_path / 'body.md'
    # Remove the Closes/Part of line from the Tasks section.
    no_issue = _CONFORMANT_BODY.replace('- Closes #42\n', 'Did some work.\n')
    body.write_text(no_issue, encoding='utf-8')
    result = _run(body)
    assert result.returncode == 1
    assert 'Closes #N or Part of #N' in result.stderr


def _run_event(event_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_LINTER), '--event', str(event_file)],
        capture_output=True,
        text=True,
    )


def test_event_path_conformant_passes(tmp_path: Path) -> None:
    # The --event JSON path is how CI invokes the linter.
    event = tmp_path / 'event.json'
    event.write_text(json.dumps({'pull_request': {'body': _CONFORMANT_BODY}}), encoding='utf-8')
    result = _run_event(event)
    assert result.returncode == 0, result.stderr
    assert 'PR: PASS' in result.stdout


def test_event_null_body_fails(tmp_path: Path) -> None:
    # A PR opened with an empty body is a valid event but fails the contract.
    event = tmp_path / 'event.json'
    event.write_text(json.dumps({'pull_request': {'body': None}}), encoding='utf-8')
    result = _run_event(event)
    assert result.returncode == 1
    assert 'Posted on behalf of' in result.stderr


def test_event_without_pull_request_fails_cleanly(tmp_path: Path) -> None:
    # A malformed event must exit 1 with a clear message, not a bare KeyError.
    event = tmp_path / 'event.json'
    event.write_text(json.dumps({'action': 'opened'}), encoding='utf-8')
    result = _run_event(event)
    assert result.returncode == 1
    assert 'no pull_request.body' in result.stderr
