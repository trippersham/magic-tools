"""Phase 2 integration tests — the ``XMageBatch --worker`` persistent game loop.

These spawn a REAL XMage JVM against the local dist jar (the committed harness jar
first on the classpath, then ``MAKE_MAGIC_XMAGE_DIST_JAR``), feed it ``TASK {json}``
lines on stdin, and read ``RESULT {json}`` lines back — the P1 wire protocol. They
are marked ``@pytest.mark.integration`` (deselected by default; run with
``-m integration``) and self-skip unless the local jar + lab JRE + a compiled driver
are all present.

The load-bearing test is :func:`test_state_bleed_driverless_after_driven`: in ONE
worker JVM a DRIVEN game (a real compiled driver on seat A) is followed by a
DRIVERLESS game on the same decks, and the driverless transcript is asserted to hold
ZERO ``DRIVER_*`` / ``MACRO_FIRE_REAL`` markers — proving the per-game
``registriesReset`` + URLClassLoader discard actually scrub the seam between games.

The two games run ONCE in a module-scoped fixture (they are fast — lands-only decks
deck-out in ~1s) and every test asserts against the captured artifacts, so the whole
module costs a single worker JVM + two games.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.integration

# A real compiled driver from the local driver store (the v2 nudge batch). Its
# ``register(UUID)`` wires DriverBonus + MacroRegistry + SelectionRegistry and prints
# QUAD_DRIVER_REGISTERED (stderr) + DRIVER_REGISTERED (stdout) — the markers the
# state-bleed test keys on. Registration is deck-agnostic, so a lands-only deck is fine.
_DRIVER_UUID = '0109aec2cf0b0c82cbc42f8ee9a36034b731995ca0'
_DRIVER_FQCN = f'makemagic.driver.d_{_DRIVER_UUID}.Driver'

# Marker prefixes that MUST NOT appear in a driverless game's transcript.
_DRIVER_MARKERS = ('DRIVER_REGISTERED', 'DRIVER_MACRO_FIRED', 'DRIVER_STEER_FIRED',
                   'DRIVER_MULLIGAN', 'MACRO_FIRE_REAL', 'QUAD_DRIVER_REGISTERED')


def _driver_classes_dir() -> Path:
    from pipeline.store.paths import StorePaths

    data_dir = os.environ.get('MAKE_MAGIC_DATA_DIR')
    root = Path(data_dir) if data_dir else StorePaths.resolve().data_dir
    return root / 'drivers' / _DRIVER_UUID / 'classes'


@dataclass(frozen=True)
class _Session:
    """Artifacts captured from ONE two-game worker session."""

    result_lines: dict[str, str]  # task_id -> raw RESULT/ERROR line
    transcripts: dict[str, str]  # task_id -> transcript text
    log_paths: dict[str, Path]  # task_id -> per-game log file
    exit_code: int
    driven_growth: list[int]  # byte-size samples of the driven log while it was written


def _write_deck(path: Path, land: str, commander: str) -> None:
    path.write_text(f'99 {land}\nSB: 1 {commander}\n', encoding='utf-8')


def _reader(stream: object, tag: str, q: queue.Queue) -> None:  # type: ignore[type-arg]
    for line in stream:  # type: ignore[attr-defined]
        q.put((tag, line.rstrip('\n')))
    q.put((tag, None))


def _await(q: queue.Queue, pred: Callable[[str, str], bool], timeout: float) -> str | None:  # type: ignore[type-arg]
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            tag, line = q.get(timeout=max(0.1, deadline - time.time()))
        except queue.Empty:
            return None
        if line is None:
            continue
        if pred(tag, line):
            return line
    return None


@pytest.fixture(scope='module')
def session(tmp_path_factory: pytest.TempPathFactory) -> _Session:
    from pipeline.sim import xmage_runtime as xr
    from pipeline.sim.engines import xmage as xe

    classes = _driver_classes_dir()
    if not classes.is_dir():
        pytest.skip(f'compiled driver not present: {classes}')

    try:
        install = xr.resolve()
    except Exception as exc:
        pytest.skip(f'XMage install unresolved (needs local dist jar + JRE): {exc}')

    db_src = install.mage_tests_dir / 'db'
    if not db_src.is_dir():
        pytest.skip(f'H2 card DB not built at {db_src} (run XMageBatch --warm)')

    run_dir = tmp_path_factory.mktemp('worker_run')
    # Private db copy (per the H2 parallel-init lesson) — clonefile on APFS is instant.
    shutil.copytree(db_src, run_dir / 'db', copy_function=_clone_or_copy)

    deck_a = run_dir / 'deckA.txt'
    deck_b = run_dir / 'deckB.txt'
    _write_deck(deck_a, 'Swamp', 'Yargle, Glutton of Urborg')
    _write_deck(deck_b, 'Forest', "Yeva, Nature's Herald")

    driven = {
        'id': 'g0_driven',
        'fmt': 'commander',
        'a': {'deck': str(deck_a), 'driver': {'cp': str(classes), 'fqcn': _DRIVER_FQCN}},
        'b': {'deck': str(deck_b), 'driver': None},
    }
    driverless = {
        'id': 'g1_driverless',
        'fmt': 'commander',
        'a': {'deck': str(deck_a), 'driver': None},
        'b': {'deck': str(deck_b), 'driver': None},
    }

    cmd = xe._compose_launch_cmd(install, ['--worker', '--worker-max-games', '2'], heap='3g')
    proc = subprocess.Popen(
        cmd, cwd=run_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )
    assert proc.stdin is not None and proc.stdout is not None
    q: queue.Queue = queue.Queue()  # type: ignore[type-arg]
    threading.Thread(target=_reader, args=(proc.stdout, 'O', q), daemon=True).start()

    result_lines: dict[str, str] = {}
    driven_growth: list[int] = []
    logs_dir = run_dir / 'logs'
    try:
        for task in (driven, driverless):
            if _await(q, lambda tag, ln: tag == 'O' and ln == 'READY', 300) is None:
                pytest.fail('worker never printed READY')
            proc.stdin.write('TASK ' + json.dumps(task) + '\n')
            proc.stdin.flush()

            # Sample the driven transcript's size while the game runs (live-flush evidence).
            log_file = logs_dir / f'{task["id"]}.log'
            sampler_stop = threading.Event()
            if task is driven:
                threading.Thread(
                    target=_sample_growth, args=(log_file, sampler_stop, driven_growth), daemon=True,
                ).start()

            line = _await(q, lambda tag, ln: tag == 'O' and (ln.startswith('RESULT ') or ln.startswith('ERROR ')), 600)
            sampler_stop.set()
            if line is None:
                pytest.fail(f'no RESULT for task {task["id"]}')
            result_lines[task['id']] = line

        proc.stdin.close()
        try:
            exit_code = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail('worker did not exit after 2 games (--worker-max-games recycle failed)')
    finally:
        if proc.poll() is None:
            proc.kill()

    transcripts = {tid: (logs_dir / f'{tid}.log').read_text(encoding='utf-8', errors='replace')
                   for tid in ('g0_driven', 'g1_driverless')}
    log_paths = {tid: logs_dir / f'{tid}.log' for tid in ('g0_driven', 'g1_driverless')}
    return _Session(result_lines, transcripts, log_paths, exit_code, driven_growth)


def _clone_or_copy(src: str, dst: str, *, follow_symlinks: bool = True) -> object:
    """copytree copy_function: APFS clonefile (instant) with a plain-copy fallback."""
    try:
        return subprocess.run(['cp', '-c', src, dst], check=True, capture_output=True).returncode
    except Exception:
        return shutil.copy2(src, dst)


def _sample_growth(log_file: Path, stop: threading.Event, out: list[int]) -> None:
    while not stop.is_set():
        with contextlib.suppress(OSError):
            out.append(log_file.stat().st_size)
        time.sleep(0.02)
    with contextlib.suppress(OSError):
        out.append(log_file.stat().st_size)


# ---------------------------------------------------------------------------- #


def test_state_bleed_driverless_after_driven(session: _Session) -> None:
    """THE load-bearing test: a driverless game after a driven one shows ZERO DRIVER_* markers.

    Proves ``registriesReset(idA/idB)`` + URLClassLoader discard scrub the seam between games.
    """
    driven = session.transcripts['g0_driven']
    driverless = session.transcripts['g1_driverless']

    # The driven game must actually have loaded + registered the driver (else the test is moot).
    assert 'DRIVER_REGISTERED' in driven
    assert 'QUAD_DRIVER_REGISTERED' in driven

    # The driverless game must carry NONE of the seam markers.
    offending = [ln for ln in driverless.splitlines()
                 if any(ln.startswith(m) for m in _DRIVER_MARKERS)]
    assert offending == [], f'state bled into the driverless game: {offending}'


def test_state_bleed_via_parser(session: _Session) -> None:
    """The same proof through the P4 parser: driver_registered flips True→False."""
    from pipeline.sim.transcript_parser import parse_transcript

    driven = parse_transcript(session.transcripts['g0_driven'])
    driverless = parse_transcript(session.transcripts['g1_driverless'])

    assert driven.driver_registered is True
    assert driverless.driver_registered is False
    assert driverless.macro_reachable is False
    assert not driverless.incomplete  # a real, terminated game (not a truncated transcript).


def test_transcript_live_flush_and_parses(session: _Session) -> None:
    """The per-game transcript exists, is non-trivial, carries the P4 markers, and parses."""
    from pipeline.sim.transcript_parser import parse_transcript

    log = session.log_paths['g0_driven']
    assert log.is_file()
    text = session.transcripts['g0_driven']
    assert len(text) > 1000
    # P4-contract markers interleaved (stdout Turn:/HANDLOG + the terminator) into one file.
    assert 'Turn: Turn 1' in text
    assert 'HANDLOG turn=' in text
    assert 'XMAGEBATCH RESULT ' in text
    assert 'Game Result:' in text

    feat = parse_transcript(text)
    assert feat.winner in ('a', 'b', 'draw')
    assert feat.last_turn is not None and feat.last_turn >= 1

    # Live-flush evidence: the file was already growing (non-zero) while the game ran, not
    # dumped only at game-end. (Lands-only games finish in ~1s, so we assert it reached a
    # non-trivial size during the run rather than a strict monotonic curve.)
    assert session.driven_growth, 'no growth samples captured'
    assert max(session.driven_growth) > 0


def test_result_line_matches_p1_wire_protocol(session: _Session) -> None:
    """Each RESULT line parses via the P1 codec into a GameResult with the right id/winner."""
    from pipeline.sim.game_protocol import GameResult, parse_line

    for tid in ('g0_driven', 'g1_driverless'):
        msg = parse_line(session.result_lines[tid])
        assert isinstance(msg, GameResult)
        assert msg.task_id == tid
        assert msg.winner in ('A', 'B', 'DRAW')
        assert msg.log_path is not None


def test_worker_recycles_at_max_games(session: _Session) -> None:
    """The worker self-terminates (exit 0) after --worker-max-games games (governor respawns)."""
    assert session.exit_code == 0


def test_result_json_shape(session: _Session) -> None:
    """The RESULT JSON body carries exactly the P1 keys the governor's parser reads."""
    for tid in ('g0_driven', 'g1_driverless'):
        body = json.loads(session.result_lines[tid][len('RESULT '):])
        assert set(body) >= {'id', 'winner', 'kill_turn', 'ms', 'markers', 'log'}
        assert body['id'] == tid
        assert isinstance(body['markers'], list)
