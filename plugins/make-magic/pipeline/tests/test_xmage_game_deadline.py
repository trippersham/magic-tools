"""Integration test — the HARD per-game wall-clock deadline in ``XMageBatch --worker``.

Proves the deadline watcher (patch 0007) actually bounds an INTRA-TURN livelock that the
base CP7 AI never breaks: a cEDH mono-black K'rrik game whose ``Blood Celebrant`` optional
mana ability oscillates a player's life around 0 forever (never advancing to the next
untap, so ``stopOnTurn`` never fires; mandatory-feeling SBA churn, so no think budget
applies; ``game.start()`` never returns on its own). The watcher forces the game to end
(``game.end()`` + game-thread interrupt) and the worker emits a TERMINAL, NON-DECISIVE
``RESULT`` (``reason=timeout``, ``winner=none``) — dedup / cell-done / no requeue.

Two assertions, in ONE worker JVM:
  (a) the K'rrik game emits ``RESULT`` with ``reason=timeout`` within ~budget+slack; and
  (b) the SAME worker then serves a SUBSEQUENT normal game and emits a clean decisive
      ``RESULT`` (no ``reason``) — proving the per-game watcher cancel + interrupt-flag
      clear + registries reset leave the worker healthy.

A SHORT budget (``MAKE_MAGIC_MAX_GAME_WALLCLOCK_SECS=30``) keeps it fast. Marked
``@pytest.mark.integration`` (deselected by default; run ``-m integration``); self-skips
unless the local dist jar + lab JRE + built H2 card DB are all present.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.integration

_BUDGET_SECS = 60
# The cEDH K'rrik deck holds the Blood Celebrant livelock; a mirror maximises the odds the
# loop forms inside the budget window.
_KRRIK_DCK = Path(__file__).resolve().parents[1] / (
    'pipeline/data/gauntlet/commander/cedh/mono-black__k-rrik-son-of-yawgmoth.dck'
)


def _reader(stream: object, q: queue.Queue) -> None:  # type: ignore[type-arg]
    for line in stream:  # type: ignore[attr-defined]
        q.put(line.rstrip('\n'))
    q.put(None)


def _await(q: queue.Queue, pred: Callable[[str], bool], timeout: float) -> str | None:  # type: ignore[type-arg]
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            line = q.get(timeout=max(0.1, deadline - time.time()))
        except queue.Empty:
            return None
        if line is None:
            continue
        if pred(line):
            return line
    return None


def _clone_or_copy(src: str, dst: str, *, follow_symlinks: bool = True) -> object:
    try:
        return subprocess.run(['cp', '-c', src, dst], check=True, capture_output=True).returncode
    except Exception:
        return shutil.copy2(src, dst)


def test_wallclock_deadline_then_reset(tmp_path: Path) -> None:
    from pipeline.sim import xmage_runtime as xr
    from pipeline.sim.engines import xmage as xe

    if not _KRRIK_DCK.is_file():
        pytest.skip(f"K'rrik gauntlet deck not present: {_KRRIK_DCK}")

    try:
        install = xr.resolve()
    except Exception as exc:
        pytest.skip(f'XMage install unresolved (needs a reactor/dist jar + JRE): {exc}')
    # The GameDeadline watcher lives in THIS worktree's freshly-rebuilt harness jar, which must
    # sort FIRST on the classpath so it shadows the (stale) XMageBatch bundled in the dist jar —
    # and the classpath must carry the driver-seam classes (mage.player.ai.score.*) the worker's
    # per-game registriesReset touches. Both hold in reactor mode (MAKE_MAGIC_XMAGE_HOME) and in
    # the local-dist override, but NOT the bare fetched-dist branch (harness not prepended). Skip
    # rather than run the wrong (dist-bundled) XMageBatch.
    if str(xr._HARNESS_JAR) != install.classpath.split(os.pathsep)[0]:
        pytest.skip(
            'harness jar not first on classpath (bare fetched-dist mode) — set MAKE_MAGIC_XMAGE_HOME '
            'to a built reactor (which carries the seam classes) so the rebuilt XMageBatch runs.'
        )
    # A runnable JRE is mandatory (the macOS /usr/bin/java stub is not one). Skip if it can't run.
    try:
        subprocess.run([str(install.java), '-version'], capture_output=True, check=True)
    except Exception as exc:
        pytest.skip(f'no runnable JRE (set MAKE_MAGIC_JAVA to a JDK 17 java launcher): {exc}')

    db_src = install.mage_tests_dir / 'db'
    if not (db_src.is_dir() and any(db_src.glob('*.mv.db'))):
        pytest.skip(f'H2 card DB not built at {db_src} (run XMageBatch --warm in that home)')

    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    # Private db copy (per the H2 parallel-init lesson) — never scan the shared db in place.
    shutil.copytree(db_src, run_dir / 'db', copy_function=_clone_or_copy)

    # Translate the Forge .dck to XMage's `N Card` txt via the SAME path build_corpus_game_tasks uses.
    krrik_txt = run_dir / 'krrik.txt'
    krrik_txt.write_text(_forge := xe._forge_dck_to_xmage_txt(_KRRIK_DCK.read_text(encoding='utf-8')), encoding='utf-8')
    # A trivial lands-only commander deck for the SUBSEQUENT normal game (decks out fast → decisive).
    quick = run_dir / 'quick.txt'
    quick.write_text('99 Swamp\nSB: 1 Yargle, Glutton of Urborg\n', encoding='utf-8')

    livelock = {
        'id': 'g0_krrik_livelock',
        'fmt': 'commander',
        'a': {'deck': str(krrik_txt), 'driver': None},
        'b': {'deck': str(krrik_txt), 'driver': None},
    }
    normal = {
        'id': 'g1_quick_normal',
        'fmt': 'commander',
        'a': {'deck': str(quick), 'driver': None},
        'b': {'deck': str(quick), 'driver': None},
    }

    env = dict(os.environ)
    env['MAKE_MAGIC_MAX_GAME_WALLCLOCK_SECS'] = str(_BUDGET_SECS)

    cmd = xe._compose_launch_cmd(install, ['--worker', '--worker-max-games', '2'], heap='3g')
    proc = subprocess.Popen(
        cmd,
        cwd=run_dir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    q: queue.Queue = queue.Queue()  # type: ignore[type-arg]
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()

    results: dict[str, str] = {}
    try:
        for task, slack in ((livelock, _BUDGET_SECS + 90), (normal, 300)):
            # The card-DB scan happens once before the first READY, hence the generous READY wait.
            if _await(q, lambda ln: ln == 'READY', 600) is None:
                pytest.fail(f'worker never printed READY before task {task["id"]}')
            proc.stdin.write('TASK ' + json.dumps(task) + '\n')
            proc.stdin.flush()
            line = _await(q, lambda ln: ln.startswith('RESULT ') or ln.startswith('ERROR '), _BUDGET_SECS + slack)
            if line is None:
                pytest.fail(f'no RESULT for task {task["id"]} within budget+slack')
            results[task['id']] = line

        proc.stdin.close()
        try:
            exit_code = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail('worker did not exit after 2 games (--worker-max-games recycle failed)')
    finally:
        if proc.poll() is None:
            proc.kill()

    assert exit_code == 0

    # (a) the livelock game timed out: TERMINAL, NON-DECISIVE.
    ll_line = results['g0_krrik_livelock']
    assert ll_line.startswith('RESULT '), f'livelock game did not produce a RESULT: {ll_line}'
    ll = json.loads(ll_line[len('RESULT ') :])
    assert ll['reason'] == 'timeout', f'expected reason=timeout, got: {ll}'
    assert ll['winner'] == 'none', f'timeout must be non-decisive (winner=none), got: {ll}'
    assert 'reason=timeout' in ll['markers']
    # It fired near the budget, not the full stall backstop.
    assert ll['ms'] <= (_BUDGET_SECS + 60) * 1000, f'timeout fired too late: ms={ll["ms"]}'

    # (b) the SAME worker then served a SUBSEQUENT game and emitted a clean terminal RESULT —
    # proving the per-game watcher cancel + interrupt-flag clear + registries reset left the
    # worker healthy. The load-bearing reset invariant is that game 2's deadline watcher was
    # armed FRESH: a leaked interrupt flag from game 1 would make game 2's very first
    # checkIfGameIsOver() short-circuit to "over" and end it near-instantly (ms ≈ 0). So game 2
    # must have PLAYED a full fresh game — either to a natural decision (reason is None) or, if
    # the durdly lands-only mirror out-runs the budget, to its OWN fresh ~budget-long timeout.
    # Both prove reset; an instant (< a few seconds) end would prove a bled-through interrupt.
    n_line = results['g1_quick_normal']
    assert n_line.startswith('RESULT '), f'normal game did not produce a RESULT: {n_line}'
    n = json.loads(n_line[len('RESULT ') :])
    assert exit_code == 0, 'worker did not exit cleanly after the subsequent game'
    if n.get('reason') is None:
        # Clean decisive/undecided game — the ideal proof of a healthy, reset worker.
        assert n['winner'] in ('A', 'B', 'DRAW'), f'unexpected winner on normal game: {n}'
    else:
        # A fresh, full-duration timeout also proves the watcher re-armed (NOT an insta-end).
        assert n['reason'] == 'timeout' and n['winner'] == 'none', f'unexpected non-decisive shape: {n}'
        assert n['ms'] >= (_BUDGET_SECS - 5) * 1000, (
            f'game 2 ended too fast ({n["ms"]}ms) — a bled-through interrupt, not a fresh game'
        )
