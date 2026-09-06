"""Intervening resource + JVM monitor for the whole-corpus sim batch (P5.1).

A SEPARATE, restartable watchdog that runs ALONGSIDE a governed corpus batch and
INTERVENES on sustained resource pressure — the safety infra the whole-corpus
XMage run leans on. It is deliberately decoupled from :mod:`pipeline.sim.governor`
(which owns admission + the emergency in-process sampler for ONE run): the monitor
is a standalone loop the batch harness (P5.3) launches next to itself, polls for a
pause signal, and reads an ALARM file from — and which survives a restart by
resuming its ledger.

What it does every :attr:`MonitorConfig.sample_interval_s`:

  * **Watches** free RAM (``vm_stat`` on macOS / ``/proc/meminfo`` on Linux, via
    :func:`pipeline.sim.governor.free_ram_gib`) and free disk (``df`` via
    :func:`pipeline.sim.governor.free_disk_gib`). A sample below **RAM 2 GiB** or
    **disk 5 GiB** (both configurable) is a BREACH.
  * **Intervenes** on SUSTAINED pressure — ``breach_samples`` consecutive breaches
    (a ~10-min window, not a single spike) — by raising a cooperative **pause
    flag** (a file the batch harness polls) and writing an **ALARM file** the batch
    and orchestrator read. On the first clean sample it clears both (recovery).
  * **Reaps** stalled / hung / orphaned JVMs — but ONLY within its OWN lineage
    (the batch's spawned matchup subprocesses tracked in
    :data:`pipeline.sim.runner._ACTIVE_PROCS`). A JVM is reap-eligible iff it is an
    ORPHAN (pid ∉ the batch's active-set, when one is supplied), a HANG (age past
    a per-game ceiling), or a STALL (CPU ≈ 0 for ``stall_samples`` consecutive
    samples). Reap = SIGKILL of that process group via
    :func:`pipeline.sim.runner._kill_process_group`. It NEVER runs a broad
    ``pkill java`` — sibling worktrees/agents may run their own JVMs, so the
    reaper only ever touches Popen handles this process spawned.
  * **Records** an atomic (temp + :func:`os.replace`) restartable ledger of the
    sample history, interventions, and reaps. On restart it re-reads the ledger and
    the pause-flag file and resumes cleanly.

The harness contract (start / stop / poll pause / read alarm) is the public API:
:meth:`ResourceMonitor.start`, :meth:`ResourceMonitor.stop`,
:meth:`ResourceMonitor.poll_pause`, :meth:`ResourceMonitor.read_alarm`. All the
probes (RAM / disk / CPU / age / owned-procs / active-set) are injectable so the
thresholds, breach window, and reaper are unit-testable without touching the host.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pipeline.sim import runner
from pipeline.sim.governor import free_disk_gib, free_ram_gib

__all__ = (
    'MonitorConfig',
    'ResourceMonitor',
    'ps_age_s',
    'ps_cpu_pct',
)


class _Killable(Protocol):
    """The bit of :class:`subprocess.Popen` the reaper needs: a pid + a liveness poll."""

    pid: int

    def poll(self) -> int | None: ...


def ps_cpu_pct(pid: int) -> float | None:
    """Instantaneous CPU% of ``pid`` via ``ps`` (STDLIB, no psutil); ``None`` on any error.

    ``None`` means "unknown" and the reaper treats it as NOT-a-stall (conservative:
    never reap on a failed read). A live idle ``sleep`` reads ~0.0; a spinning process
    reads high.
    """
    return _ps_field(pid, '%cpu')


def ps_age_s(pid: int) -> float | None:
    """Elapsed wall-clock seconds since ``pid`` started via ``ps -o etimes`` (BSD + GNU);
    ``None`` on any error (→ reaper skips the hang check for that pid)."""
    return _ps_field(pid, 'etimes')


def _ps_field(pid: int, field_name: str) -> float | None:
    try:
        out = subprocess.run(
            ['ps', '-o', f'{field_name}=', '-p', str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except OSError:
        return None
    if not out:
        return None
    try:
        return float(out)
    except ValueError:
        return None


@dataclass
class MonitorConfig:
    """Knobs for :class:`ResourceMonitor`. All defaults are the pre-decided safe values.

    ``breach_samples`` / ``stall_samples`` are COUNTS of consecutive samples, so the
    sustained window in wall-clock is ``samples * sample_interval_s`` — the defaults
    (13 @ 45 s ≈ 10 min) match the ~10-min window the spec calls for. ``hang_ceiling_s``
    is a per-game wall-clock ceiling above which a JVM is presumed hung.
    """

    sample_interval_s: float = 45.0
    #: ALARM floors (sustained breach → pause + ALARM file). Match the corpus-run spec.
    ram_alarm_gib: float = 2.0
    disk_alarm_gib: float = 5.0
    #: Consecutive breach samples before intervening (window, not a single spike).
    breach_samples: int = 13
    #: A JVM whose CPU stays at/below this for ``stall_samples`` in a row is STALLED.
    stall_cpu_pct: float = 1.0
    stall_samples: int = 13
    #: A JVM older than this (wall-clock seconds) is a HANG regardless of CPU.
    hang_ceiling_s: float = 1800.0
    #: Volume to check for free disk (``None`` = cwd's volume).
    disk_path: Path | None = None
    #: Where the ledger / PAUSE / ALARM files live (``None`` = ``<data_dir>/sim/monitor``).
    state_dir: Path | None = None


_LEDGER_NAME = 'ledger.json'
_PAUSE_NAME = 'PAUSE'
_ALARM_NAME = 'ALARM'


def _default_state_dir() -> Path:
    """``<data_dir>/sim/monitor`` (honoring ``MAKE_MAGIC_DATA_DIR``), OS-temp fallback.

    Mirrors :func:`pipeline.sim.runner.staging_root`'s degrade-never-crash discipline so
    the monitor's own state location can never be the thing that breaks a run.
    """
    try:
        from pipeline.store.paths import StorePaths

        return StorePaths.resolve().data_dir / 'sim' / 'monitor'
    except Exception:
        import tempfile

        return Path(tempfile.gettempdir()) / 'make-magic-sim-monitor'


class ResourceMonitor:
    """A restartable, intervening resource + JVM watchdog for a corpus sim batch.

    Construct with a :class:`MonitorConfig` (and optional injected probes), then either
    drive it one cycle at a time via :meth:`sample_once` (tests) or run it in the
    background via :meth:`start` / :meth:`stop`. The batch harness polls
    :meth:`poll_pause` before admitting new matchup work and reads :meth:`read_alarm`
    for the current pressure state.

    Own-lineage reaper discipline: ``owned_procs`` defaults to
    :data:`pipeline.sim.runner._ACTIVE_PROCS` — the exact set of matchup subprocesses
    THIS process spawned (each a real :class:`subprocess.Popen` handle). The reaper only
    ever iterates that set and SIGKILLs by process group; it holds no ``pkill`` path, so
    a JVM launched by another worktree/agent is never a candidate.
    """

    def __init__(
        self,
        config: MonitorConfig | None = None,
        *,
        ram_probe: Callable[[], float] | None = None,
        disk_probe: Callable[[Path | None], float] | None = None,
        cpu_of: Callable[[int], float | None] | None = None,
        age_of: Callable[[int], float | None] | None = None,
        owned_procs: Callable[[], Iterable[_Killable]] | None = None,
        active_pids: Callable[[], set[int] | None] | None = None,
        kill: Callable[[_Killable], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config or MonitorConfig()
        self._ram_probe = ram_probe or free_ram_gib
        self._disk_probe = disk_probe or free_disk_gib
        self._cpu_of = cpu_of or ps_cpu_pct
        self._age_of = age_of or ps_age_s
        # Default owned-lineage source: the runner's process-global registry of live
        # matchup JVMs — the ONLY set the reaper is ever allowed to touch.
        self._owned_procs = owned_procs or (lambda: list(runner._ACTIVE_PROCS))
        self._active_pids = active_pids or (lambda: None)
        self._kill = kill or runner._kill_process_group
        self._clock = clock or time.time

        self.state_dir = self.config.state_dir or _default_state_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.state_dir / _LEDGER_NAME
        self.pause_path = self.state_dir / _PAUSE_NAME
        self.alarm_path = self.state_dir / _ALARM_NAME

        self._breach_count = 0
        self._stall_counts: dict[int, int] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # Restart resume: re-read a prior ledger + the pause flag so a restarted monitor
        # continues the same record and does not lose an in-effect pause.
        self._ledger: dict[str, list[dict[str, Any]]] = self._load_ledger()
        self._paused = self.pause_path.exists()

    # ---- restartable ledger --------------------------------------------- #

    def _load_ledger(self) -> dict[str, list[dict[str, Any]]]:
        try:
            data = json.loads(self.ledger_path.read_text())
        except (OSError, ValueError):
            data = {}
        return {
            'samples': list(data.get('samples', [])),
            'interventions': list(data.get('interventions', [])),
            'reaps': list(data.get('reaps', [])),
        }

    def _persist(self) -> None:
        # Atomic temp + replace so a crash mid-write never truncates the ledger a
        # restart resumes from.
        tmp = self.state_dir / f'.{_LEDGER_NAME}.{os.getpid()}.tmp'
        tmp.write_text(json.dumps(self._ledger))
        os.replace(tmp, self.ledger_path)

    @property
    def ledger(self) -> dict[str, list[dict[str, Any]]]:
        """The in-memory ledger (samples / interventions / reaps)."""
        return self._ledger

    # ---- harness-facing API --------------------------------------------- #

    def poll_pause(self) -> bool:
        """Read the cooperative pause flag (file presence) — the batch calls this before
        admitting new matchup work. File-based so a SEPARATE poller process sees it too."""
        return self.pause_path.exists()

    def read_alarm(self) -> dict[str, Any] | None:
        """The current ALARM payload (reason + tightest sample) or ``None`` when clear."""
        try:
            return json.loads(self.alarm_path.read_text())
        except (OSError, ValueError):
            return None

    def start(self) -> None:
        """Launch the monitor loop on a background daemon thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='resource-monitor', daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float | None = None) -> None:
        """Signal the loop to end and join it, then flush a final ledger."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout if timeout is not None else self.config.sample_interval_s + 5.0)
            self._thread = None
        with self._lock:
            self._persist()

    def _run(self) -> None:
        while not self._stop.wait(self.config.sample_interval_s):
            try:
                self.sample_once()
            except Exception:  # a watchdog must never die on a transient probe error.
                continue

    # ---- one monitoring cycle ------------------------------------------- #

    def sample_once(self) -> dict[str, Any]:
        """Run ONE cycle: sample resources, update the sustained-breach intervention, reap
        eligible JVMs, and atomically persist the ledger. Returns the recorded sample."""
        now = self._clock()
        ram = self._ram_probe()
        disk = self._disk_probe(self.config.disk_path)
        sample = {'ts': now, 'free_ram_gib': ram, 'free_disk_gib': disk}
        with self._lock:
            self._ledger['samples'].append(sample)
            self._update_intervention(now, ram, disk)
            for reap in self._reap(now):
                self._ledger['reaps'].append(reap)
            self._persist()
        return sample

    def _update_intervention(self, now: float, ram: float, disk: float) -> None:
        breach = ram < self.config.ram_alarm_gib or disk < self.config.disk_alarm_gib
        if breach:
            self._breach_count += 1
        else:
            self._breach_count = 0

        if self._breach_count >= self.config.breach_samples:
            if not self._paused:
                self._paused = True
                self.pause_path.write_text('1')
                reason = 'ram' if ram < self.config.ram_alarm_gib else 'disk'
                payload = {
                    'ts': now,
                    'reason': reason,
                    'free_ram_gib': ram,
                    'free_disk_gib': disk,
                    'breach_count': self._breach_count,
                }
                self.alarm_path.write_text(json.dumps(payload))
                self._ledger['interventions'].append({'action': 'pause', **payload})
        elif self._paused:
            # First clean (non-breach) sample → recover: clear the pause flag + ALARM.
            self._paused = False
            self.pause_path.unlink(missing_ok=True)
            self.alarm_path.unlink(missing_ok=True)
            self._ledger['interventions'].append(
                {'action': 'resume', 'ts': now, 'free_ram_gib': ram, 'free_disk_gib': disk}
            )

    def _reap(self, now: float) -> list[dict[str, Any]]:
        """Reap stalled / hung / orphaned JVMs within OWN lineage only; return reap records.

        Iterates ONLY ``self._owned_procs()`` (the batch's own spawned Popen handles) and
        SIGKILLs by process group. There is no broad-kill path here — a JVM from another
        worktree is never in this set, so it is never touched (the load-bearing guardrail).
        """
        active = self._active_pids()
        owned = list(self._owned_procs())
        reaped: list[dict[str, Any]] = []
        live_pids: set[int] = set()
        for proc in owned:
            pid = proc.pid
            live_pids.add(pid)
            if proc.poll() is not None:  # already exited — drop any stall tally.
                self._stall_counts.pop(pid, None)
                continue
            reason = self._reap_reason(pid, active)
            if reason is not None:
                try:
                    self._kill(proc)  # type: ignore[arg-type]  # _kill accepts the _Killable protocol
                except Exception:  # a kill failure is best-effort; record nothing.
                    continue
                self._stall_counts.pop(pid, None)
                reaped.append({'ts': now, 'pid': pid, 'reason': reason})
        # Forget stall tallies for pids no longer owned (avoid unbounded growth).
        for pid in list(self._stall_counts):
            if pid not in live_pids:
                self._stall_counts.pop(pid, None)
        return reaped

    def _reap_reason(self, pid: int, active: set[int] | None) -> str | None:
        # ORPHAN: an owned JVM the batch no longer counts as active (pid ∉ active-set).
        if active is not None and pid not in active:
            return 'orphan'
        # HANG: past the per-game wall-clock ceiling regardless of CPU.
        age = self._age_of(pid)
        if age is not None and age > self.config.hang_ceiling_s:
            return 'hang'
        # STALL: CPU ≈ 0 sustained over N consecutive samples (a single idle blip is
        # not enough — a JVM between games can read low for one sample).
        cpu = self._cpu_of(pid)
        if cpu is not None and cpu <= self.config.stall_cpu_pct:
            self._stall_counts[pid] = self._stall_counts.get(pid, 0) + 1
            if self._stall_counts[pid] >= self.config.stall_samples:
                return 'stall'
        else:
            self._stall_counts[pid] = 0
        return None
