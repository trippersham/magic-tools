"""Resource-safety concurrency governor for parallel Forge sim JVMs.

Runs MANY matchups across a bounded, resource-safe worker pool so a batch can
never exhaust the machine. Ported from a live-tested prototype and hardened to
STDLIB-only resource detection (no ``psutil`` dependency).

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
from typing import TYPE_CHECKING

from pipeline.sim.runner import MatchResult, kill_active_matchup_processes

if TYPE_CHECKING:
    from pipeline.sim.engine import EngineInstall, SimEngine

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

#: META-SAFETY ceiling on concurrent JVMs for any batch (never exceeded, even on a
#: big machine). 6 is empirical: beyond ~6 simultaneous Forge card-DB loads the
#: shared I/O + memory pressure thrash and per-JVM start time balloons (3.8s solo →
#: ~13.7s at 6-up), erasing the parallelism gain. The runtime pool is
#: ``min(this, cores-2, free_ram/per_jvm)`` — usually smaller than this cap.
DEFAULT_HARD_CAP = 6
#: Per-JVM memory budget (GiB): ``-Xmx2g`` plus RSS overhead, rounded to ~2 GiB.
DEFAULT_PER_JVM_GIB = 2.0
#: Cores held back for the OS + the governor thread itself.
_CORE_HEADROOM = 2
#: Slack added to the sampler's join timeout at drain. A single sample can block for
#: up to the ``vm_stat`` subprocess timeout (5 s) plus the disk stat, so joining with
#: only ``sampler_interval_s`` (2 s) could return while the sampler is still mid-sample
#: — a stale sampler that keeps recording snapshots into the NEXT run's window (``ab``
#: / ``both`` run governors back-to-back). Cover the worst-case sample cost.
_SAMPLER_JOIN_SLACK_S = 6.0
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
            out = subprocess.run(['vm_stat'], capture_output=True, text=True, timeout=5, check=False).stdout
            pages: dict[str, int] = {}
            for line in out.splitlines():
                if ':' in line:
                    key, val = line.split(':', 1)
                    val = val.strip().rstrip('.')
                    if val.isdigit():
                        pages[key.strip()] = int(val)
            page_count = pages.get('Pages free', 0) + pages.get('Pages inactive', 0) + pages.get('Pages speculative', 0)
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

    A not-yet-created target (e.g. a staging dir made lazily on first run) still
    lives on a real volume, so walk up to the nearest EXISTING ancestor before
    stat'ing — otherwise ``disk_usage`` raises on the missing path and we'd read
    ``0.0``, wrongly tripping the admission floor before any run has staged anything.
    Returns ``0.0`` only if even that can't be stat'd, so admission treats a truly
    unreadable volume as tight.
    """
    target = path if path is not None else Path.cwd()
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        return shutil.disk_usage(target).free / _GIB
    except OSError:
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
    #: Optional ``(classes_dir, fqcn)`` per-deck driver for PlayerA (``deck_a``). Only
    #: the XMage engine consumes it; ``None`` keeps ``run_matchup`` driverless (the
    #: kwarg is not even passed), so a Forge spec is byte-identical to before.
    driver: tuple[str, str] | None = None


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
    ``failures`` holds every :class:`MatchFailure`. ``pairs`` binds each result
    to the EXACT :class:`MatchSpec` that produced it — deck names are NOT unique
    across specs, so callers that need to attribute results (e.g. to a cache
    key) must pair by spec, never by name. ``pool_size`` is the derived
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
    pairs: list[tuple[MatchSpec, MatchResult]] = field(default_factory=list)


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
    #: An ENGINE-imposed hard ceiling on concurrent JVMs, clamped onto the RAM-derived
    #: pool. Used by XMage to SERIALIZE (cap=1) on a non-COW staging volume, where each
    #: per-run private-db copy is a full ~266 MB real copy — a parallel pool would burn
    #: pool x db-size of real disk (the reboot-class exhaustion, #61). ``None`` = no cap.
    max_concurrency: int | None = None
    #: EMERGENCY floors (below the admission floors): a CONTINUOUS background sampler
    #: re-checks free RAM/disk every :attr:`sampler_interval_s` DURING the run — not
    #: just at admission — so a starvation that develops while ``pool`` workers are all
    #: in-flight (no admission happening) is detected. ``emergency_breaches_to_abort``
    #: consecutive breaches ABORT the batch: admission stops AND the in-flight JVMs are
    #: SIGKILLed via :func:`~pipeline.sim.runner.kill_active_matchup_processes` — the
    #: running workers are the pressure, so stopping admission alone would leave them
    #: draining (each only bounded by its own per-matchup external timeout, minutes
    #: long) while the host keeps starving. Killing them relieves the pressure now;
    #: each killed matchup is recorded as a failure (the batch is aborting regardless).
    emergency_ram_gib: float = 1.0
    emergency_disk_gib: float = 0.5
    sampler_interval_s: float = 2.0
    emergency_breaches_to_abort: int = 3
    #: Bound the admission back-off loop so a persistently starved host aborts
    #: (returns a partial result) instead of spinning forever.
    max_admission_backoffs: int = 240

    def run(self, engine: SimEngine, install: EngineInstall, specs: list[MatchSpec]) -> PoolResult:
        """Execute ``specs`` and return a :class:`PoolResult`.

        Derives the pool size (unless pinned), then admits work one spec at a
        time: each admission re-checks the RAM/disk floors (backing off while
        below), honors the ~5 s stagger, and hands the spec to a bounded
        :class:`~concurrent.futures.ThreadPoolExecutor` that dispatches each spec
        to ``engine.run_matchup`` with the resolved ``install``. A per-worker
        semaphore + the executor size together cap concurrent JVMs at
        ``pool_size``.
        """
        # `is not None` (not truthiness): a pinned pool_size of 0 must NOT silently
        # re-derive, and a pinned negative must NOT reach Semaphore(-1) /
        # ThreadPoolExecutor(max_workers<=0) (a ValueError crash). Clamp to >=1 — a
        # batch always runs at least one JVM. Derive lazily (only when unpinned) so a
        # pinned pool doesn't waste a resource probe.
        if self.pool_size is not None:
            pool = max(1, self.pool_size)
        else:
            pool = max(1, derive_pool_size(hard_cap=self.hard_cap, per_jvm_gib=self.per_jvm_gib))
        # An engine's per-batch safety ceiling (e.g. XMage serializing on a non-COW
        # staging volume) clamps the RAM-derived pool — never raises it. `max(1, ...)`
        # floors it so an engine returning 0 can't wedge the pool below one JVM.
        if self.max_concurrency is not None:
            pool = max(1, min(pool, self.max_concurrency))

        results: list[MatchResult] = []
        failures: list[MatchFailure] = []
        pairs: list[tuple[MatchSpec, MatchResult]] = []
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
        emergency = threading.Event()  # set by the sampler on sustained starvation.
        stop_sampler = threading.Event()  # set at drain to end the sampler cleanly.

        def _record_resources() -> tuple[float, float]:
            # Called from BOTH the admission loop and the sampler thread — guard the
            # shared min/snapshot state under the same lock the workers use.
            nonlocal min_ram, min_disk
            ram = free_ram_gib()
            disk = free_disk_gib(self.disk_path)
            with lock:
                min_ram = min(min_ram, ram)
                min_disk = min(min_disk, disk)
                snapshots.append({'free_ram_gib': ram, 'free_disk_gib': disk})
            return ram, disk

        def _sampler() -> None:
            # Continuously monitor DURING the run (not just at admission). N consecutive
            # emergency-floor breaches -> abort: signal the admission loop to stop AND
            # SIGKILL the in-flight JVMs (they are the pressure; waiting for their
            # per-matchup timeouts would leave the host starving for minutes).
            breaches = 0
            while not stop_sampler.wait(self.sampler_interval_s):
                ram, disk = _record_resources()
                if ram < self.emergency_ram_gib or disk < self.emergency_disk_gib:
                    breaches += 1
                    if breaches >= self.emergency_breaches_to_abort:
                        emergency.set()
                        kill_active_matchup_processes()  # relieve pressure now, not at timeout.
                        return
                else:
                    breaches = 0

        def _worker(spec: MatchSpec) -> None:
            nonlocal in_flight, max_concurrent
            with lock:
                in_flight += 1
                max_concurrent = max(max_concurrent, in_flight)
            try:
                # Only pass ``driver`` when the spec carries one — a driverless spec
                # (every Forge spec, and undriven XMage) calls run_matchup with the
                # exact prior signature (Forge's run_matchup has no ``driver`` kwarg).
                driver_kw = {'driver': spec.driver} if spec.driver is not None else {}
                result = engine.run_matchup(
                    spec.deck_a,
                    spec.deck_b,
                    n=spec.n,
                    seed=spec.seed + self.seed_offset,
                    fmt=spec.fmt,
                    install=install,
                    **driver_kw,
                )
                with lock:
                    results.append(result)
                    pairs.append((spec, result))
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

        sampler = threading.Thread(target=_sampler, name='governor-sampler', daemon=True)
        sampler.start()

        with ThreadPoolExecutor(max_workers=pool) as executor:
            while pending:
                # Wait for a free concurrency slot (blocks -> never over-admit).
                slots.acquire()

                # A sustained emergency (the continuous sampler) stops admission: no
                # new work, in-flight drains. Distinct from a per-spawn floor back-off.
                if emergency.is_set():
                    aborted = True
                    slots.release()
                    break

                # ~5 s staggered starts FIRST (skip the delay for the very first spawn).
                # The stagger sleep must come BEFORE the floor re-check, not after — else
                # the admitting resource reading is up to stagger_s stale by the time the
                # JVM actually launches (the in-flight JVMs keep eating RAM/disk during
                # the sleep), and we'd admit straight through the floor we just "checked".
                if last_spawn and self.stagger_s > 0:
                    elapsed = time.monotonic() - last_spawn
                    wait = self.stagger_s - elapsed
                    if wait > 0:
                        time.sleep(wait)

                # The stagger sleep may have straddled an emergency — re-check before spawn.
                if emergency.is_set():
                    aborted = True
                    slots.release()
                    break

                # Memory/disk-aware admission: re-check floors IMMEDIATELY before the
                # spawn (no intervening sleep), so the reading that authorizes the JVM is
                # fresh at launch — the per-spawn guarantee this module advertises.
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

                spec = pending.pop(0)
                futures.append(executor.submit(_worker, spec))
                last_spawn = time.monotonic()

            # Drain: wait for in-flight workers to finish (or self-kill on timeout).
            for fut in futures:
                fut.result()

        # End the continuous sampler now that no work remains.
        stop_sampler.set()
        sampler.join(timeout=self.sampler_interval_s + _SAMPLER_JOIN_SLACK_S)
        if emergency.is_set():
            aborted = True

        return PoolResult(
            pool_size=pool,
            results=results,
            failures=failures,
            max_concurrent=max_concurrent,
            aborted=aborted,
            min_free_ram_gib=0.0 if min_ram == float('inf') else min_ram,
            min_free_disk_gib=0.0 if min_disk == float('inf') else min_disk,
            snapshots=snapshots,
            pairs=pairs,
        )


def run_matchups(
    engine: SimEngine,
    install: EngineInstall,
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
    max_concurrency: int | None = None,
    emergency_ram_gib: float = 1.0,
    emergency_disk_gib: float = 0.5,
    sampler_interval_s: float = 2.0,
    emergency_breaches_to_abort: int = 3,
    max_admission_backoffs: int = 240,
) -> PoolResult:
    """Run ``specs`` across a bounded, resource-safe pool (convenience wrapper).

    Constructs a :class:`Governor` with the given knobs and runs it, dispatching
    each spec to ``engine.run_matchup`` with the resolved ``install``.
    ``pool_size`` ``None`` derives the size at runtime; pinning it (e.g. the gated
    Forge test) caps concurrency exactly. ``max_concurrency`` is an engine-imposed
    ceiling clamped onto the derived pool (XMage's non-COW serialize). See
    :class:`Governor.run` for the admission and concurrency guarantees.
    """
    governor = Governor(
        pool_size=pool_size,
        max_concurrency=max_concurrency,
        hard_cap=hard_cap,
        per_jvm_gib=per_jvm_gib,
        ram_floor_gib=ram_floor_gib,
        disk_floor_gib=disk_floor_gib,
        stagger_s=stagger_s,
        seed_offset=seed_offset,
        disk_path=disk_path,
        emergency_ram_gib=emergency_ram_gib,
        emergency_disk_gib=emergency_disk_gib,
        sampler_interval_s=sampler_interval_s,
        emergency_breaches_to_abort=emergency_breaches_to_abort,
        max_admission_backoffs=max_admission_backoffs,
    )
    return governor.run(engine, install, specs)
