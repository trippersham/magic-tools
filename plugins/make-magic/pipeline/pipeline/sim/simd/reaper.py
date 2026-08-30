"""Phase A2.5 — singleton launch guard + crash-safe orphan-tree reaper (the rae pattern).

Two guarantees, both built on :mod:`pipeline.sim.runner`'s process primitives:

* **Singleton.** A simd run takes an ``fcntl.flock(LOCK_EX | LOCK_NB)`` on a lock file. A SECOND
  concurrent simd launch cannot acquire it and REFUSES (raises :class:`AlreadyRunning`) rather
  than running two daemons against the same ops store / staging root.
* **Crash-safe orphan reaping.** The daemon makes itself a process-group leader (``os.setpgrp``)
  and writes a **pidfile** recording its pid == pgid. If it is SIGKILLed (OOM-killer, power loss,
  a crash), its JVM worker children survive as orphans holding full heaps. The NEXT simd startup
  reads the stale pidfile and, if that pid is dead but its process group still has members,
  ``killpg(SIGTERM → grace → SIGKILL)``\\s the WHOLE tree (daemon shell + every JVM worker) before
  it starts — so a crashed predecessor never leaks its process tree into the next run.

The flock is advisory + tied to the fd's lifetime: it releases automatically when the process
exits (even on SIGKILL), so a crashed owner never wedges the lock for its successor. Because the
next launch reaps the prior tree by pgid via the pidfile, the two mechanisms compose: the flock
prevents CONCURRENT owners; the pidfile reaps a DEAD owner's orphans.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ('AlreadyRunning', 'SingletonLock', 'reap_orphan_tree', 'write_pidfile')


class AlreadyRunning(RuntimeError):
    """Another simd daemon already holds the singleton lock — this launch refuses."""


@dataclass
class _PidRecord:
    pid: int
    pgid: int


class SingletonLock:
    """An ``flock``-based single-instance guard. Use as a context manager.

    ``acquire`` takes a non-blocking exclusive lock on ``path``; a second holder raises
    :class:`AlreadyRunning`. The lock is held for the fd's lifetime and released on ``release``
    or process exit (SIGKILL included), so a crash never strands it.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> SingletonLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise AlreadyRunning(
                    f'another simd daemon already holds {self._path} — refusing a second launch.'
                ) from exc
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None

    def __enter__(self) -> SingletonLock:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


def write_pidfile(path: str | os.PathLike[str], *, setpgrp: bool = True) -> _PidRecord:
    """Become a process-group leader and record ``pid``/``pgid`` to ``path`` (atomically).

    With ``setpgrp`` (production default) the caller calls :func:`os.setpgrp`, so its pid IS its
    pgid and a later ``killpg`` on that pgid reaps the caller AND every worker it spawned with
    ``start_new_session``/inherited group. Tests pass ``setpgrp=False`` to avoid detaching the
    test runner's own group.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if setpgrp:
        with contextlib.suppress(OSError):
            os.setpgrp()
    pid = os.getpid()
    pgid = os.getpgrp()
    tmp = p.with_suffix(p.suffix + f'.{pid}.tmp')
    tmp.write_text(f'{pid} {pgid}\n')
    os.replace(tmp, p)
    return _PidRecord(pid=pid, pgid=pgid)


def _read_pidfile(path: Path) -> _PidRecord | None:
    try:
        raw = path.read_text().split()
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        pid = int(raw[0])
        pgid = int(raw[1]) if len(raw) > 1 else pid
    except ValueError:
        return None
    return _PidRecord(pid=pid, pgid=pgid)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _pgid_has_members(pgid: int) -> bool:
    """True if killpg(pgid, 0) finds at least one live process in the group."""
    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def reap_orphan_tree(
    pidfile: str | os.PathLike[str],
    *,
    grace_s: float = 3.0,
    poll_s: float = 0.05,
    killpg: Callable[[int, int], None] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> int | None:
    """Reap a crashed predecessor's process tree from ``pidfile``; return the reaped pgid or ``None``.

    Reads the stale pidfile. If the recorded LEADER pid is still alive the owner is healthy — do
    NOT reap (the flock, not this, guards against a live concurrent owner) and return ``None``.
    If the leader is dead but its process GROUP still has members, they are orphans: send
    ``SIGTERM`` to the group, wait up to ``grace_s`` for it to drain, then ``SIGKILL`` any
    survivors. Returns the pgid that was reaped, or ``None`` when there was nothing to reap. The
    pidfile is removed once handled. Never raises — reaping must not break a startup.
    """
    do_killpg = killpg or os.killpg
    do_sleep = sleep or time.sleep
    now = clock or time.monotonic
    p = Path(pidfile)
    rec = _read_pidfile(p)
    if rec is None:
        return None
    if _pid_alive(rec.pid):
        return None  # a live leader — the flock owns concurrency, not us.
    if not _pgid_has_members(rec.pgid):
        p.unlink(missing_ok=True)  # leader dead, no orphans — just clear the stale file.
        return None

    # Orphaned tree: TERM, drain-with-grace, then KILL survivors.
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        do_killpg(rec.pgid, signal.SIGTERM)
    deadline = now() + grace_s
    while now() < deadline and _pgid_has_members(rec.pgid):
        do_sleep(poll_s)
    if _pgid_has_members(rec.pgid):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            do_killpg(rec.pgid, signal.SIGKILL)
    p.unlink(missing_ok=True)
    return rec.pgid
