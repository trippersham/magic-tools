"""Phase 5.0 unit tests — the persistent-worker command shape (no JVM spawned)."""

from __future__ import annotations

import sys

from pipeline.sim.driver_run import resolve_worker_cmd
from pipeline.sim.game_worker import DEFAULT_MAX_GAMES, build_worker_java_argv


def test_resolve_worker_cmd_shape() -> None:
    """resolve_worker_cmd returns the game_worker bootstrap argv (python -m ...), no JVM."""
    cmd = resolve_worker_cmd(max_games=500, log_dir='/tmp/logs', data_dir='/data')
    assert cmd[:3] == [sys.executable, '-m', 'pipeline.sim.game_worker']
    assert cmd[3:5] == ['--worker-max-games', '500']
    assert '--log-dir' in cmd and '--data-dir' in cmd
    # data-dir passed through verbatim; log-dir resolved to absolute.
    assert cmd[cmd.index('--data-dir') + 1] == '/data'
    assert cmd[cmd.index('--log-dir') + 1].endswith('/logs')


def test_resolve_worker_cmd_defaults() -> None:
    # 40-game default: long-lived CP7 workers decay (heap creep -> think-cap blowout ->
    # all-pass timeout games), empirically total by ~12h at the old 500; 40 recycles ~2-hourly.
    cmd = resolve_worker_cmd()
    assert cmd[3:5] == ['--worker-max-games', '40']
    assert '--log-dir' not in cmd and '--data-dir' not in cmd


def test_build_worker_java_argv_shape() -> None:
    """The staged JVM argv is well-formed: java, -Xmx3g, -cp <cp>, XMageBatch --worker …."""

    class _Install:
        java = '/opt/jre/bin/java'
        classpath = '/harness.jar:/dist.jar'

    argv = build_worker_java_argv(_Install(), max_games=250)
    assert argv[-4:] == ['org.makemagic.xmage.XMageBatch', '--worker', '--worker-max-games', '250']
    assert '-Xmx3g' in argv
    cp_i = argv.index('-cp')
    assert argv[cp_i + 1] == '/harness.jar:/dist.jar'
    assert '/opt/jre/bin/java' in argv


def test_default_max_games() -> None:
    # 40, not 500: decay-safe recycle cadence proven in the 2026-09 full-corpus run.
    assert DEFAULT_MAX_GAMES == 40
