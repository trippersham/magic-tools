"""@integration — a REAL ``--worker`` JVM emits an ``end_cause=`` marker in its RESULT line.

Drives the ACTUAL XMage JVM (no mock), so it is ``@integration`` and skips cleanly unless a harness
jar REBUILT FROM THIS WORKTREE (one whose ``XMageBatch`` derives the terminal cause) is on the
classpath, plus a runnable JRE + a warmed H2 card DB. The classifier + wire-parse are covered
offline by ``test_terminal_cause``; this proves the marker actually reaches the wire from a live
game.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from pipeline.sim.game_protocol import GameResult, parse_line


def _reader(stream: object, q: queue.Queue) -> None:  # type: ignore[type-arg]
    for line in stream:  # type: ignore[attr-defined]
        q.put(line.rstrip('\n'))
    q.put(None)


def _await(q: queue.Queue, pred: Callable[[str], bool], timeout: float) -> str | None:  # type: ignore[type-arg]
    import time

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


def _harness_has_end_cause(jar: Path) -> bool:
    """True iff ``jar`` bundles the terminal-cause derivation (rebuilt from this worktree)."""
    if not jar.is_file():
        return False
    try:
        with zipfile.ZipFile(jar) as zf:
            data = zf.read('org/makemagic/xmage/XMageBatch.class')
    except (zipfile.BadZipFile, KeyError):
        return False
    return b'end_cause=' in data and b'deriveEndCause' in data


def _resolve_or_skip():  # type: ignore[no-untyped-def]
    """Resolve a rebuilt-from-this-worktree install + runnable JRE + H2 DB, or skip."""
    from pipeline.sim import xmage_runtime as xr

    try:
        install = xr.resolve()
    except Exception as exc:
        pytest.skip(f'XMage install unresolved: {exc}')

    first = install.classpath.split(os.pathsep)[0]
    if not _harness_has_end_cause(Path(first)):
        pytest.skip('harness jar does not derive end_cause — rebuild the jar from this worktree.')
    try:
        subprocess.run([str(install.java), '-version'], capture_output=True, check=True)
    except Exception as exc:
        pytest.skip(f'no runnable JRE: {exc}')

    db_src = install.mage_tests_dir / 'db'
    if not (db_src.is_dir() and any(db_src.glob('*.mv.db'))):
        pytest.skip(f'H2 card DB not built at {db_src}')
    return install, db_src


def _run_one_worker_game(install, db_src: Path, run_dir: Path, task: dict) -> GameResult:  # type: ignore[no-untyped-def]
    """Drive ONE real worker game to its terminal RESULT and parse it. Fails on ERROR/timeout."""
    from pipeline.sim.engines import xmage as xe

    run_dir.mkdir(exist_ok=True)
    if not (run_dir / 'db').exists():
        shutil.copytree(db_src, run_dir / 'db')

    cmd = xe._compose_launch_cmd(install, ['--worker', '--worker-max-games', '1'], heap='3g')  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        cmd, cwd=run_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, env=dict(os.environ),
    )
    assert proc.stdin is not None and proc.stdout is not None
    q: queue.Queue = queue.Queue()  # type: ignore[type-arg]
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()
    try:
        assert _await(q, lambda ln: ln == 'READY', 600) is not None, 'no READY'
        proc.stdin.write('TASK ' + json.dumps(task) + '\n')
        proc.stdin.flush()
        line = _await(q, lambda ln: ln.startswith(('RESULT ', 'ERROR ')), 600)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert line is not None and line.startswith('RESULT '), f'expected terminal RESULT, got: {line}'
    msg = parse_line(line)
    assert isinstance(msg, GameResult)
    return msg


@pytest.mark.integration
def test_worker_lethal_damage_cause(tmp_path: Path) -> None:
    """@integration — a real worker game that ENDS BY LETHAL COMBAT/BURN DAMAGE must emit
    ``end_cause=lethal_damage``.

    The RED this pins: before the ``causeOfLoss`` reorder, XMage 1.4.60 marks EVERY loser as
    ``hasLeft()`` (SBA loss → ``lostForced`` enqueues the loser via ``setConcedingPlayer`` →
    ``checkConcede`` → ``leave()`` sets ``left=true``), so the concede-first branch shadowed the
    real terminal cause and this decisive damage kill was mislabeled ``concede``. Seat A is a
    mono-red aggro pile; seat B durdles on 60 Mountains and cannot block — A reliably reduces B to
    <= 0 life (a ``lethal_damage`` loss with a credited winner), never a draw or a deckout.
    """
    install, db_src = _resolve_or_skip()
    run_dir = tmp_path / 'run'
    run_dir.mkdir()

    aggro = run_dir / 'aggro.txt'
    aggro.write_text('16 Mountain\n22 Goblin Guide\n22 Lightning Bolt\n', encoding='utf-8')
    durdle = run_dir / 'durdle.txt'
    durdle.write_text('60 Mountain\n', encoding='utf-8')

    task = {
        'id': 'lethal-1', 'fmt': 'constructed',
        'a': {'deck': str(aggro), 'driver': None},
        'b': {'deck': str(durdle), 'driver': None},
    }
    msg = _run_one_worker_game(install, db_src, run_dir, task)
    assert msg.reason != 'timeout', f'game timed out, not a decisive kill: {msg.markers}'
    assert msg.winner == 'A', f'aggro seat should win by damage, got winner={msg.winner}: {msg.markers}'
    assert msg.end_cause == 'lethal_damage', (
        f'a damage kill must be lethal_damage, not {msg.end_cause!r} '
        f'(concede-first shadowing regressed?): {msg.markers}'
    )


@pytest.mark.integration
def test_worker_turn_cap_draw_cause(tmp_path: Path) -> None:
    """@integration — a mono-swamp mirror that grinds to the turn cap with no win-con emits
    ``end_cause=draw_game`` (no winner), unaffected by the concede reorder."""
    install, db_src = _resolve_or_skip()
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    deck = run_dir / 'swamp.txt'
    deck.write_text('99 Swamp\nSB: 1 Yargle, Glutton of Urborg\n', encoding='utf-8')
    task = {
        'id': 'draw-1', 'fmt': 'commander',
        'a': {'deck': str(deck), 'driver': None},
        'b': {'deck': str(deck), 'driver': None},
    }
    msg = _run_one_worker_game(install, db_src, run_dir, task)
    if msg.reason == 'timeout':
        pytest.skip('mono-swamp mirror hit the wall-clock deadline, not the turn cap')
    assert msg.end_cause == 'draw_game', f'a no-win-con mirror must draw: {msg.end_cause!r} {msg.markers}'


@pytest.mark.integration
def test_worker_emits_end_cause_marker(tmp_path: Path) -> None:
    from pipeline.sim import xmage_runtime as xr
    from pipeline.sim.engines import xmage as xe

    try:
        install = xr.resolve()
    except Exception as exc:
        pytest.skip(f'XMage install unresolved: {exc}')

    first = install.classpath.split(os.pathsep)[0]
    if not _harness_has_end_cause(Path(first)):
        pytest.skip('harness jar does not derive end_cause — rebuild the jar from this worktree.')
    try:
        subprocess.run([str(install.java), '-version'], capture_output=True, check=True)
    except Exception as exc:
        pytest.skip(f'no runnable JRE: {exc}')

    db_src = install.mage_tests_dir / 'db'
    if not (db_src.is_dir() and any(db_src.glob('*.mv.db'))):
        pytest.skip(f'H2 card DB not built at {db_src}')

    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    shutil.copytree(db_src, run_dir / 'db')
    # A tiny mono-swamp deck for both seats — a real (if degenerate) commander game that resolves.
    deck = run_dir / 'quick.txt'
    deck.write_text('99 Swamp\nSB: 1 Yargle, Glutton of Urborg\n', encoding='utf-8')

    cmd = xe._compose_launch_cmd(install, ['--worker', '--worker-max-games', '1'], heap='3g')  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        cmd, cwd=run_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, env=dict(os.environ),
    )
    assert proc.stdin is not None and proc.stdout is not None
    q: queue.Queue = queue.Queue()  # type: ignore[type-arg]
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()

    task = {
        'id': 'end-cause-1', 'fmt': 'commander',
        'a': {'deck': str(deck), 'driver': None},
        'b': {'deck': str(deck), 'driver': None},
    }
    try:
        assert _await(q, lambda ln: ln == 'READY', 600) is not None, 'no READY'
        proc.stdin.write('TASK ' + json.dumps(task) + '\n')
        proc.stdin.flush()
        line = _await(q, lambda ln: ln.startswith(('RESULT ', 'ERROR ')), 600)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert line is not None and line.startswith('RESULT '), f'expected terminal RESULT, got: {line}'
    msg = parse_line(line)
    assert isinstance(msg, GameResult)
    assert any(m.startswith('end_cause=') for m in msg.markers), msg.markers
    assert msg.end_cause is not None, msg
    # The cause must be one the classifier understands (never a bare/empty marker).
    assert msg.end_cause in {
        'lethal_damage', 'commander_damage', 'draw_empty_library', 'poison',
        'state_loss', 'draw_game', 'timeout', 'concede', 'unknown',
    }, msg.end_cause
