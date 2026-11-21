"""Contract tests for the repo-root stale-base guard `scripts/check_base.py`.

A PR's base must be live: `main`, or the head ref of a still-open PR (a live stack
parent). A base that merged or was deleted leaves the PR on ground that no longer
exists, so the guard fails it. The decision is pure and offline; the `base-guard`
job in `.github/workflows/ci.yml` feeds it `$GITHUB_BASE_REF` and the open-PR head
refs it lists with `gh`. Exercised as a subprocess against synthetic inputs,
matching the sibling `test_check_versions.py` / `test_check_pr.py` style.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_GUARD = _REPO_ROOT / 'scripts' / 'check_base.py'


def _run(base: str, open_heads: list[str] | None, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(_GUARD), '--base', base]
    if open_heads is not None:
        heads_file = tmp_path / 'open-heads.txt'
        heads_file.write_text('\n'.join(open_heads) + '\n', encoding='utf-8')
        cmd += ['--open-heads-file', str(heads_file)]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_base_main_passes(tmp_path: Path) -> None:
    result = _run('main', [], tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'base: PASS' in result.stdout


def test_base_is_open_pr_head_passes(tmp_path: Path) -> None:
    result = _run('feat/parent', ['feat/parent', 'feat/other'], tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'base: PASS' in result.stdout


def test_base_merged_or_deleted_fails(tmp_path: Path) -> None:
    # The parent is gone (merged/deleted): not main, not an open PR head.
    result = _run('feat/merged-parent', ['feat/other'], tmp_path)
    assert result.returncode == 1
    assert 'merged or deleted' in result.stderr
    assert 'feat/merged-parent' in result.stderr


def test_base_with_no_open_prs_and_not_main_fails(tmp_path: Path) -> None:
    result = _run('feat/orphan', [], tmp_path)
    assert result.returncode == 1


def test_empty_base_fails_rather_than_silently_passing(tmp_path: Path) -> None:
    result = _run('', [], tmp_path)
    assert result.returncode == 1
    assert 'no base ref' in result.stderr


def test_missing_open_heads_file_treated_as_no_open_prs(tmp_path: Path) -> None:
    # A base that is not main, with no heads file at all, is stale -> fail (never
    # a silent pass just because the listing was absent).
    result = subprocess.run(
        [sys.executable, str(_GUARD), '--base', 'feat/x'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
