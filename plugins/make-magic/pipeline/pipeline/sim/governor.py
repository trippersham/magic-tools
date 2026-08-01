"""Resource-safety concurrency governor for parallel Forge sim JVMs.

Runs MANY matchups across a bounded, resource-safe worker pool so a batch can
never exhaust the machine. Ported from the live-tested prototype
``~/mtg-sim-lab/empirical2/governor.py`` with all ``psutil`` usage converted to
STDLIB-only resource detection.

Guarantees (enforced, not hoped):

  * **Pool size is derived at runtime, never hardcoded** —
    ``max(1, min(hard_cap, cores - 2, free_mem // per_jvm_budget))`` via
    :func:`derive_pool_size`. Defaults ``hard_cap=6``, ``per_jvm_gib=2.0``.
  * **Memory/disk-aware admission** — before spawning EACH worker, free RAM and
    free disk are re-checked against floors; below a floor the governor backs off
    (sleeps) rather than spawn. Persistent starvation aborts with a partial
    :class:`PoolResult` instead of spinning forever.
  * **~5 s staggered starts** — simultaneous card-DB loads thrash the disk, so
    admissions are spaced ``stagger_s`` apart (empirical).
  * **Bounded concurrency** — a :class:`~concurrent.futures.ThreadPoolExecutor`
    sized to ``pool`` plus an admission semaphore means at most ``pool`` JVMs
    ever run at once. Each worker thread just waits on its
    :func:`~pipeline.sim.runner.run_matchup` subprocess (which already applies
    ``-Xmx2g``, the headless flag, and an external timeout + kill).
  * **Backpressure + cleanup** — a failed/timed-out matchup becomes a recorded
    :class:`MatchFailure`, not a crash; on abort no new work is admitted and
    in-flight subprocesses finish (or are killed by their own external timeout).

Resource detection is STDLIB only: ``os.cpu_count()`` for cores,
``shutil.disk_usage()`` for free disk, and platform calls for free RAM —
``vm_stat`` on macOS, ``/proc/meminfo`` on Linux, conservative fallback (assume
tight) elsewhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.sim.forge_runtime import ForgeInstall
from pipeline.sim.runner import MatchResult, run_matchup

__all__ = (
    'DEFAULT_HARD_CAP',
    'DEFAULT_PER_JVM_GIB',
    'Governor',
    'MatchFailure',
    'MatchSpec',
    'PoolResult',
    'derive_pool_size',
    'free_disk_gib',
    'free_ram_gib',
    'run_matchups',
)

#: META-SAFETY ceiling on concurrent JVMs for any batch (never exceeded).
DEFAULT_HARD_CAP = 6
#: Per-JVM memory budget (GiB): ``-Xmx2g`` plus RSS overhead, rounded to ~2 GiB.
DEFAULT_PER_JVM_GIB = 2.0
#: Cores held back for the OS + the governor thread itself.
_CORE_HEADROOM = 2
#: Bytes-per-GiB.
_GIB = 1 << 30
#: macOS ``vm_stat`` page size (bytes).
_PAGE = 16384


# --------------------------------------------------------------------------- #
# Resource detection — STDLIB only (no psutil).
# --------------------------------------------------------------------------- #


def free_ram_gib() -> float:
    """Best-effort reclaimable free RAM in GiB, STDLIB only.

    macOS: sums ``Pages free + inactive + speculative`` from ``vm_stat`` (these
    are reclaimable under pressure). Linux: ``MemAvailable`` from
    ``/proc/meminfo``. On any read failure returns ``0.0`` so admission treats
    the host as tight and backs off (conservative fallback).
    """
    try:
        if sys.platform == 'darwin':
            out = subprocess.run(
                ['vm_stat'], capture_output=True, text=True, timeout=5, check=False
            ).stdout
            pages: dict[str, int] = {}
            for line in out.splitlines():
                if ':' in line:
                    key, val = line.split(':', 1)
                    val = val.strip().rstrip('.')
                    if val.isdigit():
                        pages[key.strip()] = int(val)
            page_count = (
                pages.get('Pages free', 0)
                + pages.get('Pages inactive', 0)
                + pages.get('Pages speculative', 0)
            )
            return page_count * _PAGE / _GIB
        if sys.platform.startswith('linux'):
            for line in Path('/proc/meminfo').read_text().splitlines():
                if line.startswith('MemAvailable'):
                    return int(line.split()[1]) * 1024 / _GIB
    except Exception:  # any read failure -> conservative "tight" fallback.
        return 0.0
    return 0.0


def free_disk_gib(path: Path | None = None) -> float:
    """Free disk (GiB) on the volume holding ``path`` (cwd if ``None``); STDLIB.

    Returns ``0.0`` if the path can't be stat'd so admission treats disk as tight.
    """
    target = path if path is not None else Path.cwd()
    try:
        return shutil.disk_usage(target).free / _GIB
    except Exception:
        return 0.0


def _cores() -> int:
    """Logical CPU count (>= 1); ``os.cpu_count()`` with a conservative fallback."""
    return os.cpu_count() or 4


# --------------------------------------------------------------------------- #
# Pool sizing — pure, injectable for testing.
# --------------------------------------------------------------------------- #


def derive_pool_size(
    *,
    hard_cap: int = DEFAULT_HARD_CAP,
    per_jvm_gib: float = DEFAULT_PER_JVM_GIB,
    cores: int | None = None,
    free_mem_gib: float | None = None,
) -> int:
    """Return the runtime-safe pool size (number of concurrent JVMs).

    ``pool = max(1, min(hard_cap, cores - 2, free_mem_gib // per_jvm_gib))``.

    ``cores`` / ``free_mem_gib`` are injectable so the math is unit-testable
    without touching the host; when ``None`` they are read live via
    :func:`_cores` / :func:`free_ram_gib`. The ``>= 1`` floor guarantees at least
    one JVM even on a starved host (a single ``-Xmx2g`` run is always attempted).
    """
    core_count = cores if cores is not None else _cores()
    mem = free_mem_gib if free_mem_gib is not None else free_ram_gib()

    by_cores = core_count - _CORE_HEADROOM
    by_mem = int(mem // per_jvm_gib) if per_jvm_gib > 0 else hard_cap
    return max(1, min(hard_cap, by_cores, by_mem))


# --------------------------------------------------------------------------- #
# Value types.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MatchSpec:
    """One matchup to run: two ``(name, dck_text)`` decks + games + seed + format.

    ``deck_a`` / ``deck_b`` are already-rendered ``.dck`` pairs (as
    :func:`~pipeline.sim.runner.run_matchup` consumes). ``seed`` should differ
    across specs so parallel workers don't replay identical games; callers can
    also let :func:`run_matchups` apply a per-worker seed offset.
    """

    deck_a: tuple[str, str]
    deck_b: tuple[str, str]
    n: int
    seed: int
    fmt: str = 'constructed'


@dataclass(frozen=True)
class MatchFailure:
    """A matchup that did not produce a usable result — recorded, not raised.

    ``error`` is the stringified exception (``ForgeError`` on deck-load/timeout,
    or any other failure a worker surfaced).
    """

    spec: MatchSpec
    error: str


@dataclass(frozen=True)
class PoolResult:
    """The outcome of a governed batch: results + failures + safety metadata.

    ``results`` holds every :class:`~pipeline.sim.runner.MatchResult` that parsed;
    ``failures`` holds every :class:`MatchFailure`. ``pool_size`` is the derived
    (or caller-pinned) concurrency ceiling; ``max_concurrent`` is the observed
    peak in-flight JVMs (<= ``pool_size``). ``aborted`` is set when persistent
    resource starvation stopped admission before all work ran. The
    ``min_free_*`` / snapshot fields record the tightest resources seen.
    """

    pool_size: int
    results: list[MatchResult]
    failures: list[MatchFailure]
    max_concurrent: int
    aborted: bool
    min_free_ram_gib: float
    min_free_disk_gib: float
    snapshots: list[dict[str, float]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# The governor.
# --------------------------------------------------------------------------- #


@dataclass
class Governor:
    """Runs a batch of :class:`MatchSpec` across a bounded, resource-safe pool.

    Prefer the :func:`run_matchups` convenience wrapper; construct a ``Governor``
    directly only to tweak the knobs below. All defaults are the pre-decided
    resource-safe values.
    """

    pool_size: int | None = None
    hard_cap: int = DEFAULT_HARD_CAP
    per_jvm_gib: float = DEFAULT_PER_JVM_GIB
    ram_floor_gib: float = 2.0
    disk_floor_gib: float = 1.0
    stagger_s: float = 5.0
    seed_offset: int = 0
    disk_path: Path | None = None
    #: Bound the admission back-off loop so a persistently starved host aborts
    #: (returns a partial result) instead of spinning forever.
    max_admission_backoffs: int = 240

    def run(self, install: ForgeInstall, specs: list[MatchSpec]) -> PoolResult:
        """Execute ``specs`` and return a :class:`PoolResult`.

        Derives the pool size (unless pinned), then admits work one spec at a
        time: each admission re-checks the RAM/disk floors (backing off while
        below), honors the ~5 s stagger, and hands the spec to a bounded
        :class:`~concurrent.futures.ThreadPoolExecutor`. A per-worker
        semaphore + the executor size together cap concurrent JVMs at
        ``pool_size``.
        """
        pool = self.pool_size or derive_pool_size(
            hard_cap=self.hard_cap, per_jvm_gib=self.per_jvm_gib
        )

        results: list[MatchResult] = []
        failures: list[MatchFailure] = []
        snapshots: list[dict[str, float]] = []
        min_ram = float('inf')
        min_disk = float('inf')

        # Concurrency accounting — the hard invariant this whole module exists for.
        lock = threading.Lock()
        in_flight = 0
        max_concurrent = 0
        # Admission gate: never let more than `pool` tasks be in-flight, even
        # across the stagger loop. Acquired before submit, released on completion.
        slots = threading.Semaphore(pool)

        aborted = False

        def _record_resources() -> tuple[float, float]:
            nonlocal min_ram, min_disk
            ram = free_ram_gib()
            disk = free_disk_gib(self.disk_path)
            min_ram = min(min_ram, ram)
            min_disk = min(min_disk, disk)
            snapshots.append({'free_ram_gib': ram, 'free_disk_gib': disk})
            return ram, disk

        def _worker(spec: MatchSpec) -> None:
            nonlocal in_flight, max_concurrent
            with lock:
                in_flight += 1
                max_concurrent = max(max_concurrent, in_flight)
            try:
                result = run_matchup(
                    install,
                    spec.deck_a,
                    spec.deck_b,
                    n=spec.n,
                    seed=spec.seed + self.seed_offset,
                    fmt=spec.fmt,
                )
                with lock:
                    results.append(result)
            except Exception as exc:  # a failed matchup is recorded, never fatal.
                with lock:
                    failures.append(MatchFailure(spec=spec, error=str(exc)))
            finally:
                with lock:
                    in_flight -= 1
                slots.release()

        pending = list(specs)
        futures: list[Future[None]] = []
        last_spawn = 0.0

        with ThreadPoolExecutor(max_workers=pool) as executor:
            while pending:
                # Wait for a free concurrency slot (blocks -> never over-admit).
                slots.acquire()

                # Memory/disk-aware admission: re-check floors before EACH spawn.
                backoffs = 0
                while True:
                    ram, disk = _record_resources()
                    if ram >= self.ram_floor_gib and disk >= self.disk_floor_gib:
                        break
                    backoffs += 1
                    if backoffs >= self.max_admission_backoffs:
                        aborted = True
                        break
                    time.sleep(1.0)  # back off; do NOT spawn while starved.

                if aborted:
                    slots.release()  # hand the slot back; we're not admitting.
                    break

                # ~5 s staggered starts (skip the delay for the very first spawn).
                if last_spawn and self.stagger_s > 0:
                    elapsed = time.monotonic() - last_spawn
                    wait = self.stagger_s - elapsed
                    if wait > 0:
                        time.sleep(wait)

                spec = pending.pop(0)
                futures.append(executor.submit(_worker, spec))
                last_spawn = time.monotonic()

            # Drain: wait for in-flight workers to finish (or self-kill on timeout).
            for fut in futures:
                fut.result()

        return PoolResult(
            pool_size=pool,
            results=results,
            failures=failures,
            max_concurrent=max_concurrent,
            aborted=aborted,
            min_free_ram_gib=0.0 if min_ram == float('inf') else min_ram,
            min_free_disk_gib=0.0 if min_disk == float('inf') else min_disk,
            snapshots=snapshots,
        )


def run_matchups(
    install: ForgeInstall,
    specs: list[MatchSpec],
    *,
    pool_size: int | None = None,
    hard_cap: int = DEFAULT_HARD_CAP,
    per_jvm_gib: float = DEFAULT_PER_JVM_GIB,
    ram_floor_gib: float = 2.0,
    disk_floor_gib: float = 1.0,
    stagger_s: float = 5.0,
    seed_offset: int = 0,
    disk_path: Path | None = None,
    max_admission_backoffs: int = 240,
) -> PoolResult:
    """Run ``specs`` across a bounded, resource-safe pool (convenience wrapper).

    Constructs a :class:`Governor` with the given knobs and runs it. ``pool_size``
    ``None`` derives the size at runtime; pinning it (e.g. the gated Forge test)
    caps concurrency exactly. See :class:`Governor.run` for the admission and
    concurrency guarantees.
    """
    governor = Governor(
        pool_size=pool_size,
        hard_cap=hard_cap,
        per_jvm_gib=per_jvm_gib,
        ram_floor_gib=ram_floor_gib,
        disk_floor_gib=disk_floor_gib,
        stagger_s=stagger_s,
        seed_offset=seed_offset,
        disk_path=disk_path,
        max_admission_backoffs=max_admission_backoffs,
    )
    return governor.run(install, specs)
