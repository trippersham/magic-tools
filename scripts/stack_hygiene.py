#!/usr/bin/env python3
"""Dormancy detector + OBSERVE-MODE restack sweep for stacked-PR worktrees.

Layer 3 of the release/stack-hygiene design. When a stacked PR's parent squash-
merges, GitHub restacks the rest of the stack server-side, but a local working
copy only catches up when someone runs `gh stack sync --prune` in that worktree
— and that sync force-pushes (with lease) a rewritten child branch. Doing that
under live work is the design's named anti-goal ("state moved under me"), so a
sweep may only ever touch a worktree that is provably DORMANT.

SAFETY POSTURE — this file is the safety-critical layer. It FAILS SAFE: any
uncertainty classifies a worktree as NOT dormant, never the other way. A branch
is dormant ONLY when ALL of these hold (design §7 L3 + §10a A1):

  1. no omnigent session occupies its worktree (the orchestrator supplies the
     live-session workspaces; absence of that data is uncertainty -> NOT dormant);
  2. worktree-local quiescence — the newest mtime across the per-worktree git
     dir's HEAD, index, and logs/HEAD reflog is older than the idle threshold
     (default 45 min) AND no git worktree lock is present; and
  3. a clean working tree (no staged/unstaged/untracked changes).

omnigent's session list is NOT exhaustive (a Claude Code CLI session, a plain
terminal, or a human mid-edit are invisible to it), which is exactly why (2) and
(3) gate independently of (1): a branch sitting clean between commits during
active work still reads NOT dormant on its fresh mtime.

OBSERVE-FIRST (design §10c option A). The `sweep` subcommand ships INERT: it logs
the one `gh stack sync --prune` it WOULD run per dormant `⚠ Needs rebase`
candidate and force-pushes NOTHING. The real action is gated behind `--apply`
(or env `MM_STACK_SWEEP_APPLY=1`), which defaults OFF; with the flag off there is
no code path that reaches a `gh stack sync` call. `gh stack sync`'s own
`--force-with-lease` + conflict-safe abort is the backstop once a human flips it
active. Flipping to active is the one flag — see the "observe -> active" note and
the scheduled-task registration recipe at the bottom of this file.

CLI (both subcommands share the gathering flags):
  stack_hygiene.py detect [--root R] [--sessions F] [--idle-minutes N]
                          [--gh-stack-json F] [--now EPOCH]
  stack_hygiene.py sweep  [--apply] [same flags]

The orchestrator (an hourly omnigent scheduled task) gathers `sys_session_list` /
`sys_session_get_info` workspaces into the `--sessions` JSON, then runs `sweep`.
omnigent MCP calls live in that layer, never in this script — this script only
reads git state on disk and, in `--apply`, shells `gh stack sync --prune`.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

_DEFAULT_IDLE_MINUTES = 45
# The per-worktree files whose newest mtime stands in for "last touched". HEAD and
# index move on commit/checkout/stage; logs/HEAD is the reflog. Reading mtime is
# done BEFORE `git status` so a status-driven index refresh cannot backdate it.
_MTIME_FILES = ('HEAD', 'index', 'logs/HEAD')


@dataclasses.dataclass(frozen=True)
class WorktreeFacts:
    """Everything `classify` needs about one worktree, already gathered.

    `has_session`, `newest_mtime`, and `dirty` are tri-state: None means the fact
    could not be determined, which `classify` treats as uncertainty (NOT dormant).
    """

    path: str
    git_dir: str
    branch: str | None
    locked: bool
    newest_mtime: float | None
    dirty: bool | None
    has_session: bool | None


@dataclasses.dataclass(frozen=True)
class DormancyVerdict:
    path: str
    branch: str | None
    dormant: bool
    reasons: tuple[str, ...]


def classify(facts: WorktreeFacts, *, now: float, idle_seconds: float) -> DormancyVerdict:
    """Decide dormancy from gathered facts. Dormant iff NO blocking reason holds.

    Every branch that is not a clean, idle, unlocked, session-free worktree yields
    at least one reason and is therefore NOT dormant. Uncertainty (a None fact) is
    itself a blocking reason — the fail-safe default.
    """
    reasons: list[str] = []

    if facts.has_session is None:
        reasons.append('omnigent session state unknown (fail-safe: no sessions data supplied)')
    elif facts.has_session:
        reasons.append('a live omnigent session occupies this worktree')

    if facts.locked:
        reasons.append('the git worktree is locked')

    if facts.newest_mtime is None:
        reasons.append('worktree git mtime unreadable (fail-safe)')
    else:
        idle_for = now - facts.newest_mtime
        if idle_for < idle_seconds:
            reasons.append(f'worktree touched {idle_for / 60:.1f} min ago (< {idle_seconds / 60:.0f} min threshold)')

    if facts.dirty is None:
        reasons.append('working-tree state unknown (fail-safe)')
    elif facts.dirty:
        reasons.append('the working tree has uncommitted changes')

    return DormancyVerdict(path=facts.path, branch=facts.branch, dormant=not reasons, reasons=tuple(reasons))


# --- pure parsers (no I/O; unit-tested directly) ------------------------------


def parse_worktree_list(porcelain: str) -> list[dict[str, object]]:
    """Parse `git worktree list --porcelain` into one record per worktree.

    Records are blank-line separated. Recognizes `worktree <path>`, `branch
    refs/heads/<name>`, `locked [reason]`, `bare`, and `detached`.
    """
    records: list[dict[str, object]] = []
    current: dict[str, object] = {}
    for raw in porcelain.splitlines():
        line = raw.rstrip('\n')
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(' ')
        if key == 'worktree':
            if current:
                records.append(current)
            current = {'path': value}
        elif key == 'branch':
            current['branch'] = value.removeprefix('refs/heads/')
        elif key == 'locked':
            current['locked'] = True
        elif key in ('bare', 'detached'):
            current[key] = True
    if current:
        records.append(current)
    return records


def active_workspaces(sessions: object) -> set[str]:
    """Normalize omnigent session data into a set of realpath workspace strings.

    Accepts a list of workspace path strings, a list of session objects each with
    a `workspace` key (the `sys_session_get_info` shape), or a dict wrapping either
    under `sessions`/`workspaces`. Unknown shapes contribute nothing.
    """
    if isinstance(sessions, dict):
        sessions = sessions.get('sessions') or sessions.get('workspaces') or []
    out: set[str] = set()
    if not isinstance(sessions, list):
        return out
    for item in sessions:
        workspace: object = None
        if isinstance(item, str):
            workspace = item
        elif isinstance(item, dict):
            workspace = item.get('workspace') or item.get('path') or item.get('cwd')
        if isinstance(workspace, str) and workspace:
            out.add(os.path.realpath(workspace))
    return out


def needs_rebase_branches(stack_view: object) -> set[str]:
    """Branches `gh stack view --json` flags as needing a rebase.

    The extension's JSON shape is read defensively: a branch counts when any of
    `needsRebase`/`needs_rebase` is truthy, or a status/state string carries the
    `⚠ Needs rebase` marker. The entry list may be top-level or nested under
    `branches`/`stack`/`entries`.
    """
    entries: object = stack_view
    if isinstance(stack_view, dict):
        for key in ('branches', 'stack', 'entries'):
            if isinstance(stack_view.get(key), list):
                entries = stack_view[key]
                break
    out: set[str] = set()
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get('name') or entry.get('branch') or entry.get('ref')
        if not isinstance(name, str) or not name:
            continue
        name = name.removeprefix('refs/heads/')
        flagged = bool(entry.get('needsRebase') or entry.get('needs_rebase'))
        for field in ('status', 'state', 'rebase'):
            value = entry.get(field)
            if isinstance(value, str) and 'needs rebase' in value.lower():
                flagged = True
        if flagged:
            out.add(name)
    return out


# --- filesystem / git gathering (fail-safe: return None on any trouble) --------


def _git_dir_for(worktree_path: str) -> str | None:
    """Resolve a worktree's git dir. Main worktree: `<path>/.git` (a directory).
    Linked worktree: `<path>/.git` is a file `gitdir: <per-worktree dir>`."""
    dot_git = Path(worktree_path) / '.git'
    try:
        if dot_git.is_dir():
            return str(dot_git)
        if dot_git.is_file():
            text = dot_git.read_text(encoding='utf-8').strip()
            return text.removeprefix('gitdir:').strip() or None
    except OSError:
        return None
    return None


def newest_mtime(git_dir: str) -> float | None:
    """Newest mtime across the per-worktree HEAD/index/logs-HEAD. None if none
    readable — which `classify` treats as uncertainty (NOT dormant)."""
    newest: float | None = None
    for name in _MTIME_FILES:
        try:
            stat = (Path(git_dir) / name).stat()
        except OSError:
            continue
        newest = stat.st_mtime if newest is None else max(newest, stat.st_mtime)
    return newest


def _is_locked(git_dir: str | None, porcelain_locked: bool) -> bool:
    """Locked if `git worktree list` flagged it OR a `locked` file sits in the git
    dir. Either alone blocks dormancy."""
    if porcelain_locked:
        return True
    if git_dir and (Path(git_dir) / 'locked').exists():
        return True
    return False


def _run(cmd: Sequence[str], *, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def working_tree_dirty(worktree_path: str) -> bool | None:
    """True if `git status --porcelain` reports anything. None on any error, so an
    unreadable tree can never be mistaken for clean."""
    try:
        result = _run(['git', '-C', worktree_path, 'status', '--porcelain'])
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def gather(
    root: str,
    *,
    active: set[str] | None,
    porcelain: str | None = None,
) -> list[WorktreeFacts]:
    """Gather facts for every worktree of `root`. `active` is the live-session
    workspace set, or None when session data is unavailable (fail-safe: every
    worktree then reads has_session=None -> NOT dormant). `porcelain` overrides the
    `git worktree list` call for tests.

    Order matters: mtime is read BEFORE the dirty check so a status-driven index
    refresh cannot backdate the quiescence signal.
    """
    if porcelain is None:
        result = _run(['git', '-C', root, 'worktree', 'list', '--porcelain'])
        porcelain = result.stdout if result.returncode == 0 else ''
    facts: list[WorktreeFacts] = []
    for record in parse_worktree_list(porcelain):
        path = str(record.get('path') or '')
        if not path:
            continue
        git_dir = _git_dir_for(path)
        branch = record.get('branch')
        locked = _is_locked(git_dir, bool(record.get('locked')))
        mtime = newest_mtime(git_dir) if git_dir else None
        dirty = working_tree_dirty(path)
        has_session = None if active is None else os.path.realpath(path) in active
        facts.append(
            WorktreeFacts(
                path=path,
                git_dir=git_dir or '',
                branch=branch if isinstance(branch, str) else None,
                locked=locked,
                newest_mtime=mtime,
                dirty=dirty,
                has_session=has_session,
            )
        )
    return facts


def restack_candidates(
    verdicts: Iterable[DormancyVerdict], needs_rebase: set[str]
) -> list[DormancyVerdict]:
    """A verdict is a restack candidate only when it is dormant AND its branch is
    flagged `⚠ Needs rebase`. Both conditions, never one."""
    return [v for v in verdicts if v.dormant and v.branch is not None and v.branch in needs_rebase]


# --- CLI wiring ---------------------------------------------------------------


def _load_json_file(path: str | None) -> object | None:
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _resolve_active(sessions_path: str | None) -> tuple[set[str] | None, str | None]:
    """Return (active workspaces, warning). None workspaces => unavailable =>
    fail-safe NOT dormant for all. A given-but-unparseable file is also None."""
    if not sessions_path:
        return None, 'no --sessions supplied: omnigent liveness unknown, nothing is dormant (fail-safe)'
    data = _load_json_file(sessions_path)
    if data is None:
        return None, f'--sessions {sessions_path} unreadable or not JSON: nothing is dormant (fail-safe)'
    return active_workspaces(data), None


def _resolve_needs_rebase(gh_stack_json: str | None, root: str) -> set[str]:
    if gh_stack_json:
        return needs_rebase_branches(_load_json_file(gh_stack_json))
    result = _run(['gh', 'stack', 'view', '--json'], cwd=root)
    if result.returncode != 0:
        return set()
    try:
        return needs_rebase_branches(json.loads(result.stdout))
    except ValueError:
        return set()


def _build(args: argparse.Namespace) -> tuple[list[DormancyVerdict], list[DormancyVerdict], str | None]:
    active, warning = _resolve_active(args.sessions)
    facts = gather(args.root, active=active)
    now = args.now if args.now is not None else time.time()
    idle_seconds = args.idle_minutes * 60
    verdicts = [classify(f, now=now, idle_seconds=idle_seconds) for f in facts]
    needs_rebase = _resolve_needs_rebase(args.gh_stack_json, args.root)
    return verdicts, restack_candidates(verdicts, needs_rebase), warning


def _cmd_detect(args: argparse.Namespace) -> int:
    verdicts, candidates, warning = _build(args)
    if warning:
        print(f'hygiene: WARN: {warning}', file=sys.stderr)
    payload = {
        'verdicts': [dataclasses.asdict(v) for v in verdicts],
        'candidates': [{'branch': c.branch, 'worktree': c.path} for c in candidates],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _sync_command(worktree: str) -> str:
    return f'cd {worktree} && gh stack sync --prune'


def _cmd_sweep(args: argparse.Namespace) -> int:
    apply = args.apply or os.environ.get('MM_STACK_SWEEP_APPLY') == '1'
    _, candidates, warning = _build(args)
    if warning:
        print(f'hygiene: WARN: {warning}', file=sys.stderr)
    mode = 'APPLY' if apply else 'OBSERVE'
    print(f'hygiene: sweep mode={mode} candidates={len(candidates)}')
    for candidate in candidates:
        command = _sync_command(candidate.path)
        print(
            f'hygiene: {mode} branch={candidate.branch} worktree={candidate.path} '
            f'why=dormant:all-quiet cmd="{command}"'
        )
        if apply:
            # The ONLY force-pushing call site in this file. Unreachable unless
            # --apply / MM_STACK_SWEEP_APPLY=1 flipped `apply` True above.
            result = _run(['gh', 'stack', 'sync', '--prune'], cwd=candidate.path)
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            if result.returncode != 0:
                print(
                    f'hygiene: APPLY branch={candidate.branch} sync exited {result.returncode} '
                    '(gh stack sync restores branches + aborts safely on conflict)',
                    file=sys.stderr,
                )
    if not apply:
        print('hygiene: OBSERVE mode — force-pushed nothing. Pass --apply or set '
              'MM_STACK_SWEEP_APPLY=1 to act (see the observe->active note in this script).')
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--root', default=str(Path.cwd()), help='repo root whose worktrees to scan')
    parser.add_argument('--sessions', help='JSON file of live omnigent session workspaces')
    parser.add_argument('--idle-minutes', type=float, default=_DEFAULT_IDLE_MINUTES)
    parser.add_argument('--gh-stack-json', help='captured `gh stack view --json` (else run it)')
    parser.add_argument('--now', type=float, default=None, help='override now() epoch (tests)')


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    detect = sub.add_parser('detect', help='classify worktrees and print restack candidates')
    _add_common(detect)
    detect.set_defaults(func=_cmd_detect)
    sweep = sub.add_parser('sweep', help='observe-mode restack sweep (force-pushes nothing unless --apply)')
    _add_common(sweep)
    sweep.add_argument('--apply', action='store_true', help='DANGER: actually run gh stack sync --prune')
    sweep.set_defaults(func=_cmd_sweep)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())


# ==============================================================================
# observe -> active flip (one change) and the omnigent scheduled-task recipe
# ==============================================================================
#
# This sweep ships INERT. To make it act, after a human has validated the
# dormancy behavior end-to-end (design Phase 5), run the scheduled sweep WITH the
# flag — nothing in this file changes:
#
#     scripts/stack_hygiene.py sweep --apply --root <repo> --sessions <sessions.json>
#   or set MM_STACK_SWEEP_APPLY=1 in the task environment.
#
# Registering the hourly omnigent task is a POST-MERGE step the orchestrator runs
# by hand (this PR registers nothing). The task body must gather live sessions via
# the omnigent MCP layer, because this script never calls omnigent itself:
#
#   1. sessions = sys_session_list() -> for each, sys_session_get_info(id).workspace
#   2. write the workspaces to a JSON file (a list of {"workspace": "<path>"} or paths)
#   3. run:  scripts/stack_hygiene.py sweep --root <repo> --sessions <that file>
#      (OBSERVE first; add --apply only once trusted)
#
# sys_scheduled_task_create args (RRULE fires at most hourly; runs on the owner's
# online host at fire time):
#
#   sys_scheduled_task_create(
#       name="make-magic stack hygiene sweep",
#       rrule="FREQ=HOURLY;INTERVAL=1",
#       approval_policy="never",      # unattended
#       sandbox_mode="workspace-write",
#       prompt=<the 3-step task body above, OBSERVE mode>,
#   )
#
# A copy of this recipe lives alongside the plan at
# ~/.claude/plans/trippersham/make-magic/2026-11-20-release-prs-and-stack-hygiene/
#   subagents/l3-hygiene/omnigent-sweep-task.md
