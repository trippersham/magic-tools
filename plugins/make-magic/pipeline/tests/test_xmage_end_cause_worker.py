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
