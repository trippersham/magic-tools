"""Layer-2 (load-time) integration: a REAL ``--worker`` JVM must REFUSE a driver whose bytecode
references a forbidden terminal-state API, returning a terminal ``RESULT reason=driver-rejected``
(non-decisive, no crash-loop), while a clean driver still plays a game.

This drives the ACTUAL JVM (no mock), so it is ``@integration`` and skips cleanly unless a harness
jar REBUILT FROM THIS WORKTREE (one that bundles ``DriverClassLint`` + the load-time scan in
``XMageBatch.runWorkerGame``) sorts first on the classpath, plus a runnable JRE + a warmed H2 card
DB are present. Until the jar is rebuilt (``java/xmage-dist/build.sh``), it skips — the Python-side
scanner + logic are covered offline by ``test_driver_lint`` and ``test_driver_gate``.
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

_FIX = Path(__file__).parent / 'fixtures' / 'driver_lint'


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


def _harness_has_lint(jar: Path) -> bool:
    """True iff ``jar`` bundles the layer-2 scanner (i.e. it was rebuilt from this worktree)."""
    if not jar.is_file():
        return False
    try:
        with zipfile.ZipFile(jar) as zf:
            return 'org/makemagic/xmage/DriverClassLint.class' in zf.namelist()
    except zipfile.BadZipFile:
        return False


@pytest.mark.integration
@pytest.mark.parametrize(
    ('variant', 'driver_cls'),
    [
        # terminal-API fabrication (Player.lost / Game.setWinner)
        ('bad', 'BadDriver'),
        # zone-fabrication (moveCards / moveCardToExile*) — must be REFUSED at load time too.
        ('zonemove', 'ZoneMoveDriver'),
    ],
)
def test_worker_rejects_forbidden_driver(tmp_path: Path, variant: str, driver_cls: str) -> None:
    from pipeline.sim import xmage_runtime as xr
    from pipeline.sim.engines import xmage as xe

    try:
        install = xr.resolve()
    except Exception as exc:
        pytest.skip(f'XMage install unresolved: {exc}')

    first = install.classpath.split(os.pathsep)[0]
    if not _harness_has_lint(Path(first)):
        pytest.skip(
            'harness jar on the classpath does not bundle DriverClassLint — rebuild via '
            'java/xmage-dist/build.sh so the load-time scan is present.'
        )
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

    # Stage the forbidden fixture (terminal-API or zone-fabrication) as PlayerA's driver.
    bad_cp = run_dir / 'bad_driver'
    dest = bad_cp / 'org' / 'makemagic' / 'driver'
    dest.mkdir(parents=True)
    (dest / f'{driver_cls}.class').write_bytes(
        (_FIX / variant / 'org' / 'makemagic' / 'driver' / f'{driver_cls}.class').read_bytes()
    )

    cmd = xe._compose_launch_cmd(install, ['--worker', '--worker-max-games', '1'], heap='3g')  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        cmd, cwd=run_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, env=dict(os.environ),
    )
    assert proc.stdin is not None and proc.stdout is not None
    q: queue.Queue = queue.Queue()  # type: ignore[type-arg]
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()

    task = {
        'id': 'lint-reject-1', 'fmt': 'commander',
        'a': {'deck': str(deck), 'driver': {'cp': str(bad_cp), 'fqcn': f'org.makemagic.driver.{driver_cls}'}},
        'b': {'deck': str(deck), 'driver': None},
    }
    try:
        assert _await(q, lambda ln: ln == 'READY', 600) is not None, 'no READY'
        proc.stdin.write('TASK ' + json.dumps(task) + '\n')
        proc.stdin.flush()
        line = _await(q, lambda ln: ln.startswith(('RESULT ', 'ERROR ')), 300)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert line is not None and line.startswith('RESULT '), f'expected terminal RESULT, got: {line}'
    body = json.loads(line[len('RESULT '):])
    assert body['reason'] == 'driver-rejected', body
    assert body['winner'] == 'none', body
    assert any('forbidden' in m for m in body.get('markers', [])), body
