"""Phase 5.0 — the persistent-worker BOOTSTRAP (per-worker COW db staging + exec the JVM).

:func:`resolve_worker_cmd` (in :mod:`pipeline.sim.driver_run`) returns a command that runs
THIS module as the worker the :class:`~pipeline.sim.worker_pool.WorkerPool` spawns. It is a
thin Python shim whose whole job is to give each long-lived ``XMageBatch --worker`` JVM a
PRIVATE, copy-on-write card-DB before handing the process off — then it ``exec``\\s the JVM in
place, so the WorkerPool's stdin/stdout pipes (the P1 wire protocol) flow straight to the JVM
with no proxying.

Why a shim instead of returning the bare ``java`` argv from ``resolve_worker_cmd``:

* The Java worker opens XMage's H2 card DB at ``./db`` relative to its **cwd** and — per the
  H2 parallel-init lesson (a private per-run db copy is the only reliable fix for the
  ``AUTO_SERVER`` open race) — each worker needs its OWN ``db/``.
* :func:`~pipeline.sim.game_queue.run_games` / :class:`WorkerPool` spawn every worker from the
  SAME shared argv with NO per-worker ``cwd`` (and do not thread ``env_for_worker``), so the
  governor cannot "stage one dir per worker and pass it" without changing the P1/P3 core.
* So the worker **self-stages**: each spawn of this shim mints its own staging dir under
  :func:`runner.staging_root`, COW-clones the canonical warm db into it (reusing
  :func:`~pipeline.sim.engines.xmage._stage_private_db`), ``chdir``\\s in, and ``exec``\\s the
  JVM. The clone happens ONCE per worker (at startup — the worker is long-lived across
  ``--worker-max-games`` games), not per game.

The canonical warm db (``<mage_tests_dir>/db``) must already exist — the caller runs a single
``XMageBatch --warm`` first (the cold ``CardScanner.scan`` is not concurrency-safe). The
staging dir is PID-prefixed (``xmage-worker-<pid>-``) so :func:`runner.reap_stale_staging`
sweeps it once the JVM exits (the ``exec`` keeps the pid, so a live worker's dir is never
reaped; a recycled/dead worker's is).

Transcripts: the JVM writes ``<logdir>/<task_id>.log``. ``--log-dir`` (absolute) points every
worker at ONE shared transcript dir so the governor can ingest them by ``task_id`` afterward;
absent, each worker uses its private ``<staging>/logs`` (still absolute in the RESULT line).
The dir is handed to the JVM via ``MAKE_MAGIC_WORKER_LOGDIR`` (the seam the P2 worker reads).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

__all__ = ('build_worker_java_argv', 'main')

#: The persistent worker recycles after this many games by default (the governor respawns a
#: fresh JVM — bounds metaspace/heap creep over a long corpus run).
DEFAULT_MAX_GAMES = 40


def build_worker_java_argv(install: object, *, max_games: int = DEFAULT_MAX_GAMES) -> list[str]:
    """The exact ``XMageBatch --worker`` JVM argv, reusing the engine's classpath/heap/JVM args.

    A PURE function (no spawn, no staging) so a unit test can assert the argv shape without a
    JVM. Delegates to :func:`pipeline.sim.engines.xmage._compose_launch_cmd` (the same
    classpath-first + ``-Xmx3g`` + headless-JVM construction the goldfish/match paths use), so
    the worker JVM is byte-identical to those except for the ``--worker`` args. The per-task
    driver classpath is NOT on this classpath — the worker loads each seat's driver per-game
    from the ``cp`` field over the wire (a fresh URLClassLoader), so the JVM classpath is just
    ``<harness_jar>:<dist_jar>``.
    """
    from pipeline.sim.engines import xmage as xe

    return xe._compose_launch_cmd(
        install,  # type: ignore[arg-type]
        ['--worker', '--worker-max-games', str(max_games)],
        heap=xe._XMAGE_HEAP,
    )


def main(argv: list[str] | None = None) -> None:
    """Stage a private COW db for THIS worker, then ``exec`` the ``XMageBatch --worker`` JVM.

    Never returns on success (``os.execvpe`` replaces the process image). On a staging/resolve
    failure it exits non-zero — the WorkerPool sees the death, requeues the in-flight task (if
    any), and respawns; the governor's retry cap stops a persistent poison from looping.
    """
    import tempfile

    from pipeline.sim import runner, xmage_runtime
    from pipeline.sim.engines import xmage as xe

    parser = argparse.ArgumentParser(prog='game-worker', description='Persistent XMage game worker (COW-staged).')
    parser.add_argument('--worker-max-games', type=int, default=DEFAULT_MAX_GAMES)
    parser.add_argument('--log-dir', default=None, help='Absolute shared transcript dir (default: <staging>/logs).')
    parser.add_argument('--data-dir', default=None, help='Data dir for install resolution (default: env/StorePaths).')
    parser.add_argument(
        '--decks-dir',
        default=None,
        help='A dir of staged deck files linked into the worker cwd so a task can reference a deck by '
        'its BARE basename (a filesystem-path deck_path would put a "/" in the task_id, which the P2 '
        "worker's <logdir>/<task_id>.log derivation cannot represent).",
    )
    args = parser.parse_args(argv)

    data_dir = args.data_dir or os.environ.get('MAKE_MAGIC_DATA_DIR')
    install = xmage_runtime.resolve(data_dir=data_dir)

    staging = runner.staging_root()
    staging.mkdir(parents=True, exist_ok=True)
    # PID-prefixed so reap_stale_staging sweeps a dead worker's dir (exec keeps the pid, so a
    # live worker is never reaped). One COW clone per worker — the long-lived JVM reuses it.
    run_dir = Path(tempfile.mkdtemp(prefix=f'xmage-worker-{os.getpid()}-', dir=staging))
    xe._stage_private_db(install, run_dir)

    # Link the staged decks into cwd so a task can name a deck by bare basename (no "/" → a
    # filename-safe task_id → a valid <logdir>/<task_id>.log). Symlink, falling back to copy.
    if args.decks_dir:
        import shutil

        for f in Path(args.decks_dir).iterdir():
            if not f.is_file():
                continue
            dst = run_dir / f.name
            try:
                os.symlink(f.resolve(), dst)
            except OSError:
                shutil.copy2(f, dst)

    log_dir = Path(args.log_dir) if args.log_dir else run_dir / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)

    cmd = build_worker_java_argv(install, max_games=args.worker_max_games)
    env = dict(os.environ)
    # The P2 worker reads its transcript dir from this env (or -Dmakemagic.worker.logdir). An
    # absolute path keeps the RESULT log paths resolvable regardless of the JVM's cwd.
    env['MAKE_MAGIC_WORKER_LOGDIR'] = str(log_dir.resolve())

    os.chdir(run_dir)  # the JVM opens its private ./db here.
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvpe(cmd[0], cmd, env)  # replace image; WorkerPool's stdin/stdout pipes carry through.


if __name__ == '__main__':
    main()
