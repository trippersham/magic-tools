"""Phase A2.5 — resource governance: stable pool sizing + IMMEDIATE disk floors.

Two failures a real corpus hit that this module fixes:

* **A pool that ratcheted down under pressure and never recovered.** The old sampler could shrink
  the effective concurrency on a transient dip and leave it shrunk for the rest of the run.
  :func:`stable_pool_size` derives the size ONCE from injectable probes and returns it — the simd
  engine sizes its pool once at boot and never resizes, so there is no ratchet.
* **A 13-sample / 10-minute breach window that let disk fill before it reacted.** A missing-Java
  crash-loop can fill a disk in seconds; a sustained-window watchdog reacts far too late. The
  :class:`DiskGovernor` applies IMMEDIATE soft/hard floors on a single sample: below the SOFT
  floor it PAUSES admission (back-pressure, recoverable); below the HARD floor it HALTS — new
  admission stops AND in-flight workers are reaped — so the run exits fast and RESUMABLE (its
  committed state is in ``ops.duckdb``) instead of wedging paused forever or crashing the host.

Probes + reaper + clock are injectable so the floors are unit-testable without touching the host.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from pipeline.sim.governor import derive_pool_size, free_disk_gib
from pipeline.sim.runner import kill_active_matchup_processes

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

__all__ = ('Admission', 'DiskGovernor', 'stable_pool_size')

#: Immediate disk floors (GiB). SOFT = pause admission; HARD = halt + reap. Chosen well above the
#: point where a card-DB copy (~a few GiB) could no-space a worker mid-write.
DEFAULT_DISK_SOFT_FLOOR_GIB = 10.0
DEFAULT_DISK_HARD_FLOOR_GIB = 5.0


def stable_pool_size(
    *,
    hard_cap: int | None = None,
    per_jvm_gib: float | None = None,
    cores: int | None = None,
    free_mem_gib: float | None = None,
) -> int:
    """The pool size, derived ONCE and never ratcheted down.

    A thin, intent-named wrapper over :func:`pipeline.sim.governor.derive_pool_size`: the simd
    engine calls this exactly once at boot and holds the result for the whole run, so a transient
    RAM dip mid-run can never shrink (and permanently strand) the pool. Passing ``None`` for a
    knob defers to ``derive_pool_size``'s own default.
    """
    kwargs: dict[str, object] = {}
    if hard_cap is not None:
        kwargs['hard_cap'] = hard_cap
    if per_jvm_gib is not None:
        kwargs['per_jvm_gib'] = per_jvm_gib
    if cores is not None:
        kwargs['cores'] = cores
    if free_mem_gib is not None:
        kwargs['free_mem_gib'] = free_mem_gib
    return derive_pool_size(**kwargs)  # type: ignore[arg-type]


class Admission(enum.Enum):
    """The admission decision for one check of the disk floors."""

    OK = 'ok'  # above the soft floor — admit new work.
    PAUSE = 'pause'  # below soft, above hard — back off (recoverable).
    HALT = 'halt'  # below hard — stop admitting AND reap in-flight (resumable exit).


class DiskGovernor:
    """Immediate soft/hard disk-floor admission control (single-sample, no sustained window)."""

    def __init__(
        self,
        *,
        soft_floor_gib: float = DEFAULT_DISK_SOFT_FLOOR_GIB,
        hard_floor_gib: float = DEFAULT_DISK_HARD_FLOOR_GIB,
        disk_probe: Callable[[Path | None], float] | None = None,
        disk_path: Path | None = None,
        reaper: Callable[[], int] | None = None,
    ) -> None:
        if hard_floor_gib > soft_floor_gib:
            msg = f'hard_floor_gib ({hard_floor_gib}) must be <= soft_floor_gib ({soft_floor_gib})'
            raise ValueError(msg)
        self._soft = soft_floor_gib
        self._hard = hard_floor_gib
        self._probe = disk_probe or free_disk_gib
        self._disk_path = disk_path
        self._reaper = reaper or kill_active_matchup_processes
        self._halted = False

    @property
    def halted(self) -> bool:
        """True once a HARD-floor breach has halted the run (terminal — a resumable exit)."""
        return self._halted

    def check(self) -> Admission:
        """Sample free disk ONCE and decide admission; on a HARD breach, reap in-flight workers.

        Once halted the governor STAYS halted (returns :attr:`Admission.HALT`) — the run is
        exiting resumably, not oscillating. The reaper (default
        :func:`~pipeline.sim.runner.kill_active_matchup_processes`) fires exactly once on the
        transition into HALT so in-flight JVMs stop consuming the disk they are exhausting.
        """
        if self._halted:
            return Admission.HALT
        free = self._probe(self._disk_path)
        if free < self._hard:
            self._halted = True
            self._reaper()
            return Admission.HALT
        if free < self._soft:
            return Admission.PAUSE
        return Admission.OK
