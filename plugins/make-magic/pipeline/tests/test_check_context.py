"""Contract tests for the repo-root context linter `scripts/check_context.py`.

The linter is the single-source-of-truth gate wired into `scripts/check`: every
path the repo map names must exist, and every path the Planned section names must
not exist yet. It fails when either drifts from what is actually on disk. These
tests pin the three contract cases plus the Phase-1 tolerance for a not-yet-authored
`AGENTS.md`.

The linter lives at the repo root (not in this package), so it is exercised as a
subprocess against a synthetic `--root`, which is exactly how `scripts/check`
invokes it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# repo root = .../plugins/make-magic/pipeline/tests/this-file -> parents[4]
_REPO_ROOT = Path(__file__).resolve().parents[4]
_LINTER = _REPO_ROOT / 'scripts' / 'check_context.py'

_CLEAN_AGENTS = """# AGENTS

## Repo map

| Path | What it is |
|------|------------|
| `real.txt` | a file that exists |

## Planned

- `future.txt` not built yet, phase 2 adds it.
"""


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_LINTER), '--root', str(root)],
        capture_output=True,
        text=True,
    )


def test_clean_tree_passes(tmp_path: Path) -> None:
    (tmp_path / 'AGENTS.md').write_text(_CLEAN_AGENTS, encoding='utf-8')
    (tmp_path / 'real.txt').write_text('here', encoding='utf-8')
    # future.txt deliberately absent — it is marked planned.
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'context: PASS' in result.stdout


def test_dead_path_marked_exists_fails(tmp_path: Path) -> None:
    agents = _CLEAN_AGENTS.replace(
        '| `real.txt` | a file that exists |',
        '| `missing.txt` | claimed to exist |',
    )
    (tmp_path / 'AGENTS.md').write_text(agents, encoding='utf-8')
    # missing.txt deliberately NOT created.
    result = _run(tmp_path)
    assert result.returncode == 1
    assert 'missing.txt' in result.stderr


def test_stale_planned_mark_fails(tmp_path: Path) -> None:
    agents = _CLEAN_AGENTS.replace(
        '- `future.txt` not built yet, phase 2 adds it.',
        '- `present.txt` should have been flipped to the repo map.',
    )
    (tmp_path / 'AGENTS.md').write_text(agents, encoding='utf-8')
    (tmp_path / 'present.txt').write_text('oops, it exists', encoding='utf-8')
    result = _run(tmp_path)
    assert result.returncode == 1
    assert 'present.txt' in result.stderr
    assert 'planned' in result.stderr


def test_missing_agents_md_passes(tmp_path: Path) -> None:
    # Phase 1: AGENTS.md is not authored until Phase 2. A tree without it is clean.
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'context: PASS' in result.stdout
