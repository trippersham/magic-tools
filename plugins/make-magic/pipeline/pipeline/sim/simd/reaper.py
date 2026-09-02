"""Phase A2.5 — singleton launch guard + crash-safe orphan-tree reaper (the rae pattern).

Two guarantees, both built on :mod:`pipeline.sim.runner`'s process primitives:

* **Singleton.** A simd run takes an ``fcntl.flock(LOCK_EX | LOCK_NB)`` on a lock file. A SECOND
  concurrent simd launch cannot acquire it and REFUSES (raises :class:`AlreadyRunning`) rather
  than running two daemons against the same ops store / staging root.
* **Crash-safe orphan reaping.** The daemon makes itself a process-group leader (``os.setpgrp``)
  and writes a **pidfile** recording its pid == pgid. Its JVM workers, however, are each spawned
  with ``start_new_session=True`` (so a stalled worker can be ``killpg``\\ed own-lineage without
  taking down its siblings) — which means every worker is its OWN process-group leader, NOT a
  member of the daemon's group. So ``killpg(leader_pgid)`` alone cannot reach them (F-2). Each
  worker therefore RECORDS its pgid to a ``<pidfile>.workers`` sidecar via
  :func:`record_worker_pgid` as it spawns. If the daemon is SIGKILLed (OOM-killer, power loss, a
  crash), its JVM workers survive as reparented orphans holding full heaps. The NEXT simd startup
  reads the stale pidfile, and — when the leader pid is dead — ``killpg(SIGTERM → grace →
  SIGKILL)``\\s the leader group AND every recorded worker group before it starts, so a crashed
  predecessor never leaks its worker tree into the next run (no reliance on slow stdin-EOF
  self-exit lagging to the game deadline).

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
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = (
    'AlreadyRunning',
    'SingletonLock',
    'proc_start_token',
    'reap_orphan_tree',
    'record_worker_pgid',
    'remove_worker_pgid',
    'write_pidfile',
)


class AlreadyRunning(RuntimeError):
    """Another simd daemon already holds the singleton lock — this launch refuses."""


@dataclass
class _PidRecord:
    pid: int
    pgid: int
    #: A per-PROCESS start-time identity token (``ps -o lstart`` sanitised) — distinguishes the
    #: recorded process from a LATER unrelated process that merely reused the same integer pid.
    #: ``None`` for a legacy pidfile that predates identity tokens (verification is then skipped).
    token: str | None = None
    #: The run nonce (a per-run id) — extra provenance recorded alongside the token.
    nonce: str | None = None


def proc_start_token(pid: int) -> str | None:
    """A stable per-process start-time identity token for ``pid``, or ``None`` if it cannot be read.

    Uses ``ps -p <pid> -o lstart=`` (the process START TIME) — an integer pid is reused freely by
    the OS, but a pid+start-time pair identifies ONE concrete process: a reaper can thus refuse to
    signal a pid whose recorded start time no longer matches (it was recycled). Whitespace is
    collapsed to ``_`` so the token is a single pidfile field. Never raises."""
    if pid <= 0:
        return None
    try:
        out = subprocess.run(
            ['ps', '-p', str(pid), '-o', 'lstart='],
            capture_output=True, text=True, timeout=5.0, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = out.stdout.strip()
    if not raw:
        return None
    return '_'.join(raw.split())


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


def write_pidfile(
    path: str | os.PathLike[str], *, setpgrp: bool = True, nonce: str | None = None
) -> _PidRecord:
    """Become a process-group leader and record ``pid pgid token nonce`` to ``path`` (atomically).

    With ``setpgrp`` (production default) the caller calls :func:`os.setpgrp`, so its pid IS its
    pgid and a later ``killpg`` on that pgid reaps the caller AND every worker it spawned with
    ``start_new_session``/inherited group. Tests pass ``setpgrp=False`` to avoid detaching the
    test runner's own group.

    FAIL LOUD: if ``os.setpgrp()`` raises, this RAISES rather than silently recording whatever
    group the process already belongs to — recording the inherited (caller's) group would let a
    later reaper ``killpg`` a broader group than this daemon actually owns. The ``token`` is a
    per-process start-time identity (see :func:`proc_start_token`) so a successor reaper can tell
    this concrete process apart from a later one that reused its integer pid.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if setpgrp:
        os.setpgrp()  # FAIL LOUD on error — do NOT suppress (Sol HIGH 3).
    pid = os.getpid()
    pgid = os.getpgrp()
    token = proc_start_token(pid) or '-'
    run_nonce = nonce or uuid.uuid4().hex
    tmp = p.with_suffix(p.suffix + f'.{pid}.tmp')
    tmp.write_text(f'{pid} {pgid} {token} {run_nonce}\n')
    os.replace(tmp, p)
    return _PidRecord(pid=pid, pgid=pgid, token=token, nonce=run_nonce)


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
    token = raw[2] if len(raw) > 2 and raw[2] != '-' else None
    nonce = raw[3] if len(raw) > 3 else None
    return _PidRecord(pid=pid, pgid=pgid, token=token, nonce=nonce)


def _worker_sidecar(pidfile: Path) -> Path:
    """The companion file recording spawned worker process-group ids for ``pidfile``."""
    return pidfile.with_suffix(pidfile.suffix + '.workers')


def record_worker_pgid(
    pidfile: str | os.PathLike[str], pgid: int, *, token: str | None = None
) -> None:
    """Append a spawned worker's process-group id (+ optional identity ``token``) to the sidecar.

    Workers spawn with ``start_new_session=True`` (each is its own group leader, so ``pgid`` equals
    the worker pid), which places them OUTSIDE the daemon's process group — a ``killpg`` on the
    leader pgid can never reach them. Recording each worker pgid here lets a successor's
    :func:`reap_orphan_tree` kill the whole worker tree of a crashed predecessor. When a ``token``
    (see :func:`proc_start_token`) is recorded, the reaper verifies it before signalling, so a
    reused worker pid is never mistaken for the original. The append is a single small line (atomic
    under POSIX ``O_APPEND``); never raises — recording must not break a spawn path.
    """
    p = _worker_sidecar(Path(pidfile))
    line = f'{pgid} {token}\n' if token else f'{pgid}\n'
    with contextlib.suppress(OSError):
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('a') as fh:
            fh.write(line)


def remove_worker_pgid(pidfile: str | os.PathLike[str], pgid: int) -> None:
    """Drop every sidecar entry for ``pgid`` — called when a worker RETIRES NORMALLY.

    A normally-retired worker is gone for good and its pid is free to be reused; leaving its group
    in the sidecar would let a later reaper target whatever unrelated process next holds that pid.
    Rewrites the sidecar without ``pgid``'s lines (atomic replace). Never raises."""
    sc = _worker_sidecar(Path(pidfile))
    with contextlib.suppress(OSError):
        records = _read_worker_records(Path(pidfile))
        kept = [(g, t) for (g, t) in records if g != pgid]
        if not kept:
            sc.unlink(missing_ok=True)
            return
        tmp = sc.with_suffix(sc.suffix + f'.{os.getpid()}.tmp')
        tmp.write_text(''.join(f'{g} {t}\n' if t else f'{g}\n' for g, t in kept))
        os.replace(tmp, sc)


def _read_worker_records(pidfile: Path) -> list[tuple[int, str | None]]:
    """Read the recorded ``(pgid, token)`` worker entries (per line; deduped by pgid, ``[]`` if absent)."""
    try:
        text = _worker_sidecar(pidfile).read_text()
    except OSError:
        return []
    seen: dict[int, str | None] = {}
    for raw_line in text.splitlines():
        parts = raw_line.split()
        if not parts:
            continue
        try:
            pgid = int(parts[0])
        except ValueError:
            continue
        token = parts[1] if len(parts) > 1 else None
        seen.setdefault(pgid, token)
    return list(seen.items())


def _read_worker_pgids(pidfile: Path) -> list[int]:
    """Read the recorded worker pgids from ``pidfile``'s sidecar (deduped, ``[]`` if absent)."""
    return [g for g, _t in _read_worker_records(pidfile)]


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
    If the leader is dead, its orphan tree spans the leader's OWN process group PLUS every worker
    group recorded in the ``<pidfile>.workers`` sidecar (workers run in their own sessions, so a
    ``killpg`` on the leader pgid alone would never reach them — F-2). Every such group with live
    members is sent ``SIGTERM``, given up to ``grace_s`` to drain, then ``SIGKILL``\\ed if it
    survives. Returns the leader pgid when anything was reaped, or ``None`` when there was nothing.
    The pidfile and its sidecar are removed once handled. Never raises — reaping must not break a
    startup.
    """
    do_killpg = killpg or os.killpg
    do_sleep = sleep or time.sleep
    now = clock or time.monotonic
    p = Path(pidfile)
    rec = _read_pidfile(p)
    if rec is None:
        return None

    # Identity gate (Sol HIGH 3): an integer pid alone cannot prove the recorded LEADER is still the
    # original process — the OS reuses pids. Verify pid AND start-time token:
    #   * pid alive + token matches (or legacy no-token)  → healthy live owner, DO NOT reap.
    #   * pid alive + token MISMATCH                       → the original leader is dead and its pid
    #     was recycled by an unrelated process — must NOT signal that stranger's group.
    #   * pid dead                                         → a genuine crashed predecessor; reap.
    leader_pid_reused = False
    if _pid_alive(rec.pid):
        if rec.token is None or proc_start_token(rec.pid) == rec.token:
            return None  # a live (verified, or unverifiable-legacy) leader — the flock owns this.
        leader_pid_reused = True  # alive, but a DIFFERENT process now holds the pid.

    # Leader is gone. The orphan tree = leader's own group (only if its pid was NOT recycled) + every
    # recorded worker group whose identity token still matches (workers live in their OWN sessions,
    # so the leader pgid does not reach them). Reap only groups that are BOTH identity-verified and
    # still have live members — never a reused pid.
    candidate_pgids: list[int] = [] if leader_pid_reused else [rec.pgid]
    for wpgid, wtoken in _read_worker_records(p):
        if wtoken is not None and proc_start_token(wpgid) != wtoken:
            continue  # a reused worker pid (token mismatch) — skip, never signal a stranger.
        candidate_pgids.append(wpgid)
    live_pgids = [g for g in dict.fromkeys(candidate_pgids) if _pgid_has_members(g)]
    if not live_pgids:
        p.unlink(missing_ok=True)  # leader dead, no orphans — just clear the stale files.
        _worker_sidecar(p).unlink(missing_ok=True)
        return None

    # Orphaned tree: TERM every group, drain-with-grace, then KILL survivors.
    for g in live_pgids:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            do_killpg(g, signal.SIGTERM)
    deadline = now() + grace_s
    while now() < deadline and any(_pgid_has_members(g) for g in live_pgids):
        do_sleep(poll_s)
    for g in live_pgids:
        if _pgid_has_members(g):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                do_killpg(g, signal.SIGKILL)
    p.unlink(missing_ok=True)
    _worker_sidecar(p).unlink(missing_ok=True)
    return rec.pgid
