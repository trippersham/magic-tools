"""@integration — a REAL ``--worker`` JVM honors the TASK's ``starter`` and records it.

Drives the ACTUAL XMage JVM (no mock), so it is ``@integration`` and skips cleanly unless a harness
jar REBUILT FROM THIS WORKTREE (one whose ``XMageBatch`` reads ``task.starter`` + emits a
``starter=`` marker) is on the classpath, plus a runnable JRE + a warmed H2 card DB. The task
builder / protocol / aggregation are covered offline; this proves the CHOSEN player actually takes
turn 1 in a live game and the RESULT carries the matching ``starter=`` marker.
"""

from __future__ import annotations

import json
import os
import queue
import re
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


def _harness_has_starter(jar: Path) -> bool:
    """True iff ``jar`` bundles the starter-selection code (rebuilt from this worktree)."""
    if not jar.is_file():
        return False
    try:
        with zipfile.ZipFile(jar) as zf:
            data = zf.read('org/makemagic/xmage/XMageBatch.class')
    except (zipfile.BadZipFile, KeyError):
        return False
    return b'starter=' in data


# Turn-boundary line the harness emits (MakeMagicHooks.onGameLog): "Turn: Turn N (Ai(k)-<name>)".
# The turn-1 active player logs its NAME as "?" (an XMage early-hook quirk before the name resolves),
# so the FIRST reliably-named turn is turn 2 — which is the SECOND player, i.e. the OPPOSITE of the
# starter. We therefore derive the starter as the seat NOT named on the turn-2 line.
_TURN_RE = re.compile(r'^Turn: Turn (\d+) \(Ai\(\d\)-(Player[AB])\)')


def _turn2_line_and_player(transcript: Path) -> tuple[str, str] | None:
    """Return (raw turn-2 line, seat named on it). Turn 2's active player is the SECOND player."""
    for raw in transcript.read_text(encoding='utf-8', errors='replace').splitlines():
        m = _TURN_RE.match(raw)
        if m and m.group(1) == '2':
            return raw, m.group(2)
    return None


def _starter_from_transcript(transcript: Path) -> tuple[str | None, str]:
    """Derive who took turn 1 (PlayerA/PlayerB) + the quoted turn-2 line, from the transcript."""
    found = _turn2_line_and_player(transcript)
    if found is None:
        return None, ''
    line, second = found
    starter = 'PlayerA' if second == 'PlayerB' else 'PlayerB'
    return starter, line


def _run_one(install, run_dir: Path, deck: Path, starter: str):  # type: ignore[no-untyped-def]
    """Run ONE real worker game with ``starter`` and return (GameResult, first-turn seat)."""
    from pipeline.sim.engines import xmage as xe

    cmd = xe._compose_launch_cmd(install, ['--worker', '--worker-max-games', '1'], heap='3g')  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        cmd,
        cwd=run_dir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=dict(os.environ),
    )
    assert proc.stdin is not None and proc.stdout is not None
    q: queue.Queue = queue.Queue()  # type: ignore[type-arg]
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()

    task = {
        'id': f'starter-{starter}',
        'fmt': 'commander',
        'a': {'deck': str(deck), 'driver': None},
        'b': {'deck': str(deck), 'driver': None},
        'starter': starter,
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
    assert msg.log_path is not None
    return msg, _starter_from_transcript(Path(msg.log_path))


@pytest.mark.integration
def test_worker_honors_starter_and_records_it(tmp_path: Path) -> None:
    from pipeline.sim import xmage_runtime as xr

    try:
        install = xr.resolve()
    except Exception as exc:
        pytest.skip(f'XMage install unresolved: {exc}')

    first = install.classpath.split(os.pathsep)[0]
    if not _harness_has_starter(Path(first)):
        pytest.skip('harness jar has no starter selection — rebuild the jar from this worktree.')
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
    deck = run_dir / 'quick.txt'
    deck.write_text('99 Swamp\nSB: 1 Yargle, Glutton of Urborg\n', encoding='utf-8')

    res_a, (turn1_a, line_a) = _run_one(install, run_dir, deck, 'A')
    res_b, (turn1_b, line_b) = _run_one(install, run_dir, deck, 'B')

    # RESULT records who went first, matching the TASK.
    assert res_a.starter == 'A', res_a.markers
    assert res_b.starter == 'B', res_b.markers
    assert 'starter=A' in res_a.markers
    assert 'starter=B' in res_b.markers

    # The DIFFERENT seat actually took turn 1 in the live game (the transcript proves it — the
    # turn-2 line names the SECOND player, so the starter is the other seat).
    assert turn1_a == 'PlayerA', f'starter=A: turn 1 seat was {turn1_a!r} (turn-2 line: {line_a!r})'
    assert turn1_b == 'PlayerB', f'starter=B: turn 1 seat was {turn1_b!r} (turn-2 line: {line_b!r})'
    assert turn1_a != turn1_b
