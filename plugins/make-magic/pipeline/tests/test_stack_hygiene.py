"""Tests for the repo-root dormancy detector + observe-mode sweep `scripts/stack_hygiene.py`.

This is the safety-critical layer: it must never classify live work as dormant,
and the sweep must force-push NOTHING in its default (observe) mode. The detector
is exercised against REAL git worktrees in a temp repo — one worktree per
NOT-dormant reason (live omnigent session / recent mtime / dirty tree / locked)
plus the one all-quiet dormant case — so the classification is proven against the
actual `git worktree list --porcelain` + on-disk mtimes it reads in production,
not a mock. `gh stack view --json` is injected as a fixture (`--gh-stack-json`),
so in observe mode a stubbed `gh` on PATH must be invoked ZERO times.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = _REPO_ROOT / 'scripts' / 'stack_hygiene.py'

# A fixed "now" so the idle threshold is deterministic regardless of wall clock.
_NOW = 1_000_000_000.0
_OLD = _NOW - 3600  # 60 min ago: past the 45 min threshold
_RECENT = _NOW - 600  # 10 min ago: inside the threshold


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            'git',
            '-c',
            'user.name=Test',
            '-c',
            'user.email=test@example.com',
            '-c',
            'commit.gpgsign=false',
            '-C',
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _set_mtime(git_dir: Path, when: float) -> None:
    for name in ('HEAD', 'index', 'logs/HEAD'):
        target = git_dir / name
        if target.exists():
            os.utime(target, (when, when))


def _wt_git_dir(main: Path, name: str) -> Path:
    # `git worktree add` names the per-worktree git dir after the path's basename,
    # i.e. `wt-<name>`, not `<name>`.
    return main / '.git' / 'worktrees' / f'wt-{name}'


def _make_repo(tmp_path: Path) -> Path:
    """A main repo plus one worktree per classification outcome.

    Each NOT-dormant worktree isolates a single blocking reason (its mtime is set
    old where the reason is something else, so the reason under test is the only
    one). The two dormant worktrees differ only in whether gh flags them.
    """
    main = tmp_path / 'main'
    main.mkdir()
    _git(main, 'init', '-b', 'main')
    (main / 'seed.txt').write_text('seed\n', encoding='utf-8')
    _git(main, 'add', 'seed.txt')
    _git(main, 'commit', '-m', 'seed')

    for name in ('dormant', 'dormant-unflagged', 'dirty', 'locked', 'recent', 'live'):
        _git(main, 'worktree', 'add', '-b', f'feat/{name}', str(tmp_path / f'wt-{name}'))

    # Make the dirty worktree dirty (untracked file counts).
    (tmp_path / 'wt-dirty' / 'scratch.txt').write_text('wip\n', encoding='utf-8')
    # Lock the locked worktree.
    _git(main, 'worktree', 'lock', str(tmp_path / 'wt-locked'))

    # Set mtimes: everything OLD except `recent`, so each NOT-dormant case is
    # isolated to its own reason (session/dirty/locked), not a fresh mtime.
    for name in ('dormant', 'dormant-unflagged', 'dirty', 'locked', 'live'):
        _set_mtime(_wt_git_dir(main, name), _OLD)
    _set_mtime(_wt_git_dir(main, 'recent'), _RECENT)
    return main


def _run(
    main: Path,
    subcommand: str,
    *,
    sessions: Path | None,
    gh_stack_json: Path | None,
    extra: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(_SCRIPT), subcommand, '--root', str(main), '--now', str(_NOW)]
    if sessions is not None:
        cmd += ['--sessions', str(sessions)]
    if gh_stack_json is not None:
        cmd += ['--gh-stack-json', str(gh_stack_json)]
    cmd += extra or []
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, env=run_env)


def _write_sessions(tmp_path: Path, workspaces: list[str]) -> Path:
    path = tmp_path / 'sessions.json'
    path.write_text(json.dumps([{'workspace': w} for w in workspaces]), encoding='utf-8')
    return path


def _write_gh_stack(tmp_path: Path, flagged: list[str]) -> Path:
    """Mix the two shapes the parser must tolerate: a boolean and a status string."""
    entries: list[dict[str, object]] = []
    for i, name in enumerate(flagged):
        if i % 2 == 0:
            entries.append({'name': name, 'needsRebase': True})
        else:
            entries.append({'name': name, 'status': '⚠ Needs rebase'})
    path = tmp_path / 'gh_stack.json'
    path.write_text(json.dumps(entries), encoding='utf-8')
    return path


def _verdict_by_branch(stdout: str) -> dict[str, dict[str, object]]:
    payload = json.loads(stdout)
    return {v['branch']: v for v in payload['verdicts'] if v['branch']}


# --- the full classification matrix (real worktrees) --------------------------


def test_only_the_all_quiet_worktree_is_dormant(tmp_path: Path) -> None:
    main = _make_repo(tmp_path)
    sessions = _write_sessions(tmp_path, [str(tmp_path / 'wt-live')])
    # Every feat/* branch is flagged needs-rebase EXCEPT dormant-unflagged.
    gh = _write_gh_stack(tmp_path, ['feat/dormant', 'feat/dirty', 'feat/locked', 'feat/recent', 'feat/live'])
    result = _run(main, 'detect', sessions=sessions, gh_stack_json=gh)
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout)
    verdicts = _verdict_by_branch(result.stdout)

    # Exactly one dormant: the clean, idle, unlocked, session-free worktree.
    assert verdicts['feat/dormant']['dormant'] is True
    assert verdicts['feat/dormant-unflagged']['dormant'] is True

    # Each NOT-dormant case fails on its own isolated reason.
    assert verdicts['feat/live']['dormant'] is False
    assert any('omnigent session' in r for r in verdicts['feat/live']['reasons'])
    assert verdicts['feat/dirty']['dormant'] is False
    assert any('uncommitted changes' in r for r in verdicts['feat/dirty']['reasons'])
    assert verdicts['feat/locked']['dormant'] is False
    assert any('locked' in r for r in verdicts['feat/locked']['reasons'])
    assert verdicts['feat/recent']['dormant'] is False
    assert any('threshold' in r for r in verdicts['feat/recent']['reasons'])

    # Candidate = dormant AND flagged. dormant-unflagged is dormant but NOT flagged,
    # so it must NOT be a candidate — the intersection, never one condition alone.
    candidate_branches = {c['branch'] for c in payload['candidates']}
    assert candidate_branches == {'feat/dormant'}


def test_missing_sessions_data_makes_everything_not_dormant(tmp_path: Path) -> None:
    # FAIL SAFE: with no omnigent session data we cannot confirm liveness, so
    # nothing may be swept even though the branches are flagged needs-rebase.
    main = _make_repo(tmp_path)
    gh = _write_gh_stack(tmp_path, ['feat/dormant'])
    result = _run(main, 'detect', sessions=None, gh_stack_json=gh)
    assert result.returncode == 0, result.stderr
    assert 'session state unknown' in result.stderr or 'nothing is dormant' in result.stderr
    verdicts = _verdict_by_branch(result.stdout)
    assert verdicts['feat/dormant']['dormant'] is False
    assert json.loads(result.stdout)['candidates'] == []


def test_unreadable_sessions_file_fails_safe(tmp_path: Path) -> None:
    main = _make_repo(tmp_path)
    bad = tmp_path / 'bad.json'
    bad.write_text('not json{', encoding='utf-8')
    gh = _write_gh_stack(tmp_path, ['feat/dormant'])
    result = _run(main, 'detect', sessions=bad, gh_stack_json=gh)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['candidates'] == []


# --- sweep: observe mode force-pushes nothing ---------------------------------


def _write_fake_gh(bin_dir: Path, log: Path) -> None:
    """A `gh` stub that records every invocation. If ever called with `stack sync`
    the test fails — observe mode must never reach it."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / 'gh'
    gh.write_text(
        '#!/usr/bin/env bash\n'
        f'echo "$@" >> {log}\n'
        '# Answer a `stack view --json` with an empty stack if ever asked; the test\n'
        '# normally injects --gh-stack-json so this path is unused.\n'
        'if [ "$1" = "stack" ] && [ "$2" = "view" ]; then echo "[]"; fi\n'
        'exit 0\n',
        encoding='utf-8',
    )
    gh.chmod(0o755)


def test_sweep_observe_mode_invokes_gh_zero_times(tmp_path: Path) -> None:
    main = _make_repo(tmp_path)
    sessions = _write_sessions(tmp_path, [str(tmp_path / 'wt-live')])
    gh_json = _write_gh_stack(tmp_path, ['feat/dormant'])

    bin_dir = tmp_path / 'bin'
    log = tmp_path / 'gh-calls.log'
    _write_fake_gh(bin_dir, log)
    env = {'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}'}

    result = _run(main, 'sweep', sessions=sessions, gh_stack_json=gh_json, env=env)
    assert result.returncode == 0, result.stderr
    assert 'mode=OBSERVE' in result.stdout
    # The candidate's would-run command is logged verbatim...
    assert 'gh stack sync --prune' in result.stdout
    assert 'OBSERVE branch=feat/dormant' in result.stdout
    assert 'force-pushed nothing' in result.stdout
    # ...but gh was NEVER actually invoked (no sync, no view — nothing).
    assert not log.exists() or log.read_text(encoding='utf-8').strip() == '', (
        f'gh was invoked in observe mode: {log.read_text(encoding="utf-8") if log.exists() else ""}'
    )


def test_sweep_apply_flag_reaches_the_sync_call(tmp_path: Path) -> None:
    # Proves the gate is the only thing holding the sync back: with --apply the
    # ONE sync call site is reached (against the stub, which records it).
    main = _make_repo(tmp_path)
    sessions = _write_sessions(tmp_path, [str(tmp_path / 'wt-live')])
    gh_json = _write_gh_stack(tmp_path, ['feat/dormant'])

    bin_dir = tmp_path / 'bin'
    log = tmp_path / 'gh-calls.log'
    _write_fake_gh(bin_dir, log)
    env = {'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}'}

    result = _run(main, 'sweep', sessions=sessions, gh_stack_json=gh_json, extra=['--apply'], env=env)
    assert result.returncode == 0, result.stderr
    assert 'mode=APPLY' in result.stdout
    calls = log.read_text(encoding='utf-8') if log.exists() else ''
    assert 'stack sync --prune' in calls, f'apply mode did not reach the sync call; calls={calls!r}'


def test_sweep_apply_via_env_var(tmp_path: Path) -> None:
    main = _make_repo(tmp_path)
    sessions = _write_sessions(tmp_path, [str(tmp_path / 'wt-live')])
    gh_json = _write_gh_stack(tmp_path, ['feat/dormant'])
    bin_dir = tmp_path / 'bin'
    log = tmp_path / 'gh-calls.log'
    _write_fake_gh(bin_dir, log)
    env = {'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}', 'MM_STACK_SWEEP_APPLY': '1'}
    result = _run(main, 'sweep', sessions=sessions, gh_stack_json=gh_json, env=env)
    assert result.returncode == 0, result.stderr
    assert 'mode=APPLY' in result.stdout
    assert 'stack sync --prune' in (log.read_text(encoding='utf-8') if log.exists() else '')
