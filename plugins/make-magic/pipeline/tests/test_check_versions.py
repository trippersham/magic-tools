"""Contract tests for the repo-root lockstep linter `scripts/check_versions.py`.

The plugin manifest (`.claude-plugin/plugin.json`, what the marketplace serves) and
the Python package (`pyproject.toml`) declare the plugin's one version in two files.
They ship as a single lockstep version, so a bump that touches one but not the other
is drift that ships a wrong marketplace version. This linter asserts the two agree and
is wired into `scripts/check` so it runs inside `ci-ok` at PR time (in BOTH `--fast`
and full modes), not only at release time.

The linter lives at the repo root (not in this package), so it is exercised as a
subprocess against a synthetic `--root`, exactly how `scripts/check` invokes it — the
same shape as `test_check_context.py`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# repo root = .../plugins/make-magic/pipeline/tests/this-file -> parents[4]
_REPO_ROOT = Path(__file__).resolve().parents[4]
_LINTER = _REPO_ROOT / 'scripts' / 'check_versions.py'

_PLUGIN_JSON_REL = Path('plugins/make-magic/.claude-plugin/plugin.json')
_PYPROJECT_REL = Path('plugins/make-magic/pipeline/pyproject.toml')


def _write_tree(root: Path, *, plugin_version: str, pyproject_version: str) -> None:
    plugin_json = root / _PLUGIN_JSON_REL
    pyproject = root / _PYPROJECT_REL
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    pyproject.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.write_text(
        '{\n  "name": "make-magic",\n  "version": "' + plugin_version + '"\n}\n',
        encoding='utf-8',
    )
    pyproject.write_text(
        '[project]\nname = "make-magic-pipeline"\nversion = "' + pyproject_version + '"\n',
        encoding='utf-8',
    )


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_LINTER), '--root', str(root)],
        capture_output=True,
        text=True,
    )


def test_matched_versions_pass(tmp_path: Path) -> None:
    _write_tree(tmp_path, plugin_version='0.8.1', pyproject_version='0.8.1')
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'versions: PASS' in result.stdout


def test_mismatched_versions_fail_naming_both(tmp_path: Path) -> None:
    _write_tree(tmp_path, plugin_version='0.8.1', pyproject_version='0.8.0')
    result = _run(tmp_path)
    assert result.returncode == 1
    # The message must name BOTH versions so the drift is diagnosable at a glance.
    assert '0.8.1' in result.stderr
    assert '0.8.0' in result.stderr


def test_real_repo_tree_is_in_lockstep() -> None:
    """The live repo's two version files agree — guards this test's own fixtures
    against a real drift and proves the default `--root` resolves the repo."""
    result = subprocess.run(
        [sys.executable, str(_LINTER)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert 'versions: PASS' in result.stdout
