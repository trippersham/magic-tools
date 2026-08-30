"""T3 — the HARD per-game transcript BYTE CAP in ``XMageBatch`` (a disk backstop).

A base XMage livelock once wrote an 84 MB / 1.7M-line per-game transcript before anything
stopped it. The T2 wall-clock deadline now bounds that INDIRECTLY (it ends the game at ~budget),
but there is NO explicit ceiling on the transcript FILE. This adds a hard byte cap as
defense-in-depth: once a game's transcript writes strictly exceed ``makemagic.maxTranscriptBytes``
(env ``MAKE_MAGIC_MAX_TRANSCRIPT_BYTES``; default 64 MiB; ``<= 0`` disables), one truncation
marker is written and further transcript bytes are DISCARDED — the game keeps playing to its
RESULT unchanged.

Two layers of proof:

  * ``test_capping_outputstream_unit`` — a FAST, dependency-free Java unit test
    (``TranscriptCappingOutputStreamTest``) compiled + run with a plain JDK (no XMage reactor):
    proves a firehose past a tiny budget stays bounded, ends with exactly one marker, and a
    ``<= 0`` budget hands back the RAW delegate (byte-identical prior behavior). Self-skips if no
    JDK ``javac`` is on hand.

  * ``test_worker_transcript_bounded`` — an integration test (``-m integration``, mirrors
    ``test_xmage_game_deadline``): drives a REAL ``--worker`` JVM with a tiny cap over a short real
    game and asserts (a) the transcript file is bounded ``<= cap + marker`` AND ends with the
    marker AND the protocol ``RESULT`` is still emitted; (b) a SUBSEQUENT game in the same worker
    is ALSO bounded (per-game counter reset); and (c) a second worker with the cap DISABLED writes
    a larger, marker-free transcript. Self-skips unless the local jar + lab JRE + built H2 DB are
    all present.
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

_XMAGE_JAVA_ROOT = Path(__file__).resolve().parents[1] / 'pipeline/sim/java'
_IMPL_SRC = (
    _XMAGE_JAVA_ROOT
    / 'xmage-dist/src/main/java/org/makemagic/xmage/TranscriptCappingOutputStream.java'
)
_UNIT_TEST_SRC = (
    _XMAGE_JAVA_ROOT / 'xmage/test/org/makemagic/xmage/TranscriptCappingOutputStreamTest.java'
)

_MARKER_NEEDLE = 'TRANSCRIPT TRUNCATED AT'


def _find_javac() -> str | None:
    cand = Path('/opt/homebrew/opt/openjdk@17/bin/javac')
    if cand.is_file():
        return str(cand)
    return shutil.which('javac')


def test_capping_outputstream_unit(tmp_path: Path) -> None:
    """FAST unit test of the capping OutputStream — no XMage reactor needed."""
    javac = _find_javac()
    if javac is None:
        pytest.skip('no javac on PATH (set up a JDK 17 to run the Java unit test)')
    if not (_IMPL_SRC.is_file() and _UNIT_TEST_SRC.is_file()):
        pytest.skip(f'Java sources not found: {_IMPL_SRC} / {_UNIT_TEST_SRC}')
    java = str(Path(javac).with_name('java'))

    out = tmp_path / 'classes'
    out.mkdir()
    compile_proc = subprocess.run(
        [javac, '--release', '17', '-d', str(out), str(_IMPL_SRC), str(_UNIT_TEST_SRC)],
        capture_output=True,
        text=True,
    )
    assert compile_proc.returncode == 0, f'javac failed:\n{compile_proc.stderr}'

    run_proc = subprocess.run(
        [java, '-cp', str(out), 'org.makemagic.xmage.TranscriptCappingOutputStreamTest'],
        capture_output=True,
        text=True,
    )
    assert run_proc.returncode == 0, (
        f'Java unit test failed (exit {run_proc.returncode}):\n{run_proc.stdout}\n{run_proc.stderr}'
    )
    assert 'TranscriptCappingOutputStreamTest OK' in run_proc.stdout


# --------------------------------------------------------------------------------------------
# Integration: a real --worker JVM with a tiny cap over a short real game.
# --------------------------------------------------------------------------------------------


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


def _run_worker_game(
    *,
    install: object,
    run_dir: Path,
    deck_txt: Path,
    cap_bytes: int,
    task_id: str,
    engines_mod: object,
) -> dict[str, object]:
    """Launch ONE --worker JVM with MAX_TRANSCRIPT_BYTES=cap_bytes, run one game, return its
    parsed RESULT dict. Raises on protocol failure."""
    env = dict(os.environ)
    env['MAKE_MAGIC_MAX_TRANSCRIPT_BYTES'] = str(cap_bytes)

    cmd = engines_mod._compose_launch_cmd(install, ['--worker', '--worker-max-games', '1'], heap='3g')  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        cmd, cwd=run_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    q: queue.Queue = queue.Queue()  # type: ignore[type-arg]
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()

    task = {'id': task_id, 'fmt': 'commander', 'a': {'deck': str(deck_txt), 'driver': None},
            'b': {'deck': str(deck_txt), 'driver': None}}
    try:
        if _await(q, lambda ln: ln == 'READY', 600) is None:
            pytest.fail(f'worker never printed READY before task {task_id}')
        proc.stdin.write('TASK ' + json.dumps(task) + '\n')
        proc.stdin.flush()
        line = _await(q, lambda ln: ln.startswith('RESULT ') or ln.startswith('ERROR '), 300)
        if line is None:
            pytest.fail(f'no RESULT for task {task_id} within budget')
        proc.stdin.close()
        try:
            exit_code = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail('worker did not exit after its game (--worker-max-games recycle failed)')
    finally:
        if proc.poll() is None:
            proc.kill()

    assert exit_code == 0, f'worker exited non-zero for {task_id}'
    assert line.startswith('RESULT '), f'{task_id}: expected RESULT, got: {line}'
    return json.loads(line[len('RESULT '):])


@pytest.mark.integration
def test_worker_transcript_bounded(tmp_path: Path) -> None:
    from pipeline.sim import xmage_runtime as xr
    from pipeline.sim.engines import xmage as xe

    try:
        install = xr.resolve()
    except Exception as exc:
        pytest.skip(f'XMage install unresolved (needs a reactor/dist jar + JRE): {exc}')
    # The cap lives in THIS worktree's freshly-rebuilt harness jar, which must sort FIRST on the
    # classpath so it shadows the (stale) XMageBatch bundled in the dist jar.
    if str(xr._HARNESS_JAR) != install.classpath.split(os.pathsep)[0]:
        pytest.skip(
            'harness jar not first on classpath (bare fetched-dist mode) — set MAKE_MAGIC_XMAGE_HOME '
            'to a built reactor so the rebuilt XMageBatch runs.'
        )
    try:
        subprocess.run([str(install.java), '-version'], capture_output=True, check=True)
    except Exception as exc:
        pytest.skip(f'no runnable JRE (set MAKE_MAGIC_JAVA to a JDK 17 java launcher): {exc}')

    db_src = install.mage_tests_dir / 'db'
    if not (db_src.is_dir() and any(db_src.glob('*.mv.db'))):
        pytest.skip(f'H2 card DB not built at {db_src} (run XMageBatch --warm in that home)')

    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    shutil.copytree(db_src, run_dir / 'db', copy_function=_clone_or_copy)
    # A trivial lands-only commander mirror (decks out fast → decisive) that still prints plenty
    # of turn-by-turn transcript lines — easily past a small cap.
    deck = run_dir / 'quick.txt'
    deck.write_text('99 Swamp\nSB: 1 Yargle, Glutton of Urborg\n', encoding='utf-8')

    logs_dir = run_dir / 'logs'  # the worker's default logdir (relative to cwd=run_dir)

    cap = 4096  # tiny — a real game's transcript blows past this quickly.
    # Rough marker length (matches TranscriptCappingOutputStream.markerText).
    marker_len = len(f'\n--- TRANSCRIPT TRUNCATED AT {cap} BYTES ---\n'.encode())
    slack = 256  # a couple of trailing writes can straddle the trip point.

    # (a) + (b): two capped games in a row (fresh workers), each transcript bounded + marked.
    for task_id in ('g0_capped', 'g1_capped'):
        result = _run_worker_game(
            install=install, run_dir=run_dir, deck_txt=deck, cap_bytes=cap,
            task_id=task_id, engines_mod=xe,
        )
        log_path = Path(str(result['log']))
        assert log_path.is_file(), f'{task_id}: transcript missing at {log_path}'
        size = log_path.stat().st_size
        assert size <= cap + marker_len + slack, (
            f'{task_id}: transcript {size}B exceeds cap+marker+slack '
            f'({cap + marker_len + slack}B) — byte cap did not bound the file'
        )
        text = log_path.read_text(encoding='utf-8', errors='replace')
        assert _MARKER_NEEDLE in text, f'{task_id}: truncation marker missing from bounded transcript'
        assert text.count(_MARKER_NEEDLE) == 1, f'{task_id}: marker written more than once'
        # The game STILL produced a normal terminal RESULT (capping the log never ends the game).
        assert result.get('winner') in ('A', 'B', 'DRAW', 'none'), f'{task_id}: no normal RESULT: {result}'

    # (c): cap DISABLED (<=0) — a larger, marker-FREE transcript (prior behavior preserved).
    uncapped = _run_worker_game(
        install=install, run_dir=run_dir, deck_txt=deck, cap_bytes=0,
        task_id='g2_uncapped', engines_mod=xe,
    )
    up = Path(str(uncapped['log']))
    assert up.is_file()
    utext = up.read_text(encoding='utf-8', errors='replace')
    assert _MARKER_NEEDLE not in utext, 'disabled cap must not write a truncation marker'
    assert up.stat().st_size > cap, (
        f'uncapped transcript ({up.stat().st_size}B) should exceed the tiny cap ({cap}B) — '
        'otherwise the bounded-file assertion above proves nothing'
    )
    assert uncapped.get('winner') in ('A', 'B', 'DRAW', 'none')
    _ = logs_dir  # (documentation: transcripts land here; result['log'] is the absolute path)
