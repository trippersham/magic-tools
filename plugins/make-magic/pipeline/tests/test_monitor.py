"""Unit tests for the intervening resource + JVM monitor (P5.1).

Deterministic: RAM/disk/CPU/age probes and the owned-procs source are injected, so
threshold + reaper behavior is exercised without touching the host or a real JVM. The
two reaper tests that DO spawn real processes only ever spawn (and only ever kill)
test-owned children in their OWN session — never a broad kill.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from pipeline.sim.monitor import MonitorConfig, ResourceMonitor

# --------------------------------------------------------------------------- #
# Intervention: sustained breach → pause + ALARM; recovery clears both.
# --------------------------------------------------------------------------- #


def test_sustained_ram_breach_sets_pause_and_alarm(tmp_path: Path) -> None:
    cfg = MonitorConfig(state_dir=tmp_path, breach_samples=3, ram_alarm_gib=2.0, disk_alarm_gib=5.0)
    m = ResourceMonitor(
        cfg,
        ram_probe=lambda: 1.0,  # below the 2 GiB floor every sample.
        disk_probe=lambda p: 50.0,
        owned_procs=lambda: [],
    )
    # Two breaches is not yet the window → no pause.
    m.sample_once()
    m.sample_once()
    assert m.poll_pause() is False
    assert m.read_alarm() is None
    # Third consecutive breach crosses the window → pause + ALARM.
    m.sample_once()
    assert m.poll_pause() is True
    alarm = m.read_alarm()
    assert alarm is not None and alarm['reason'] == 'ram'


def test_single_spike_does_not_trip(tmp_path: Path) -> None:
    breached = {'n': 0}

    def ram() -> float:
        breached['n'] += 1
        return 1.0 if breached['n'] == 2 else 9.0  # one lone breach in the middle.

    m = ResourceMonitor(
        MonitorConfig(state_dir=tmp_path, breach_samples=3),
        ram_probe=ram,
        disk_probe=lambda p: 50.0,
        owned_procs=lambda: [],
    )
    for _ in range(5):
        m.sample_once()
    assert m.poll_pause() is False


def test_recovery_clears_pause_and_alarm(tmp_path: Path) -> None:
    ram_vals = iter([1.0, 1.0, 1.0, 9.0])

    m = ResourceMonitor(
        MonitorConfig(state_dir=tmp_path, breach_samples=3),
        ram_probe=lambda: next(ram_vals),
        disk_probe=lambda p: 50.0,
        owned_procs=lambda: [],
    )
    m.sample_once()
    m.sample_once()
    m.sample_once()
    assert m.poll_pause() is True
    m.sample_once()  # clean sample → recover.
    assert m.poll_pause() is False
    assert m.read_alarm() is None
    actions = [i['action'] for i in m.ledger['interventions']]
    assert actions == ['pause', 'resume']


def test_disk_breach_reason(tmp_path: Path) -> None:
    m = ResourceMonitor(
        MonitorConfig(state_dir=tmp_path, breach_samples=1, disk_alarm_gib=5.0),
        ram_probe=lambda: 32.0,
        disk_probe=lambda p: 1.0,
        owned_procs=lambda: [],
    )
    m.sample_once()
    alarm = m.read_alarm()
    assert alarm is not None and alarm['reason'] == 'disk'


# --------------------------------------------------------------------------- #
# Restartable ledger.
# --------------------------------------------------------------------------- #


def test_ledger_resumes_after_restart(tmp_path: Path) -> None:
    m1 = ResourceMonitor(
        MonitorConfig(state_dir=tmp_path, breach_samples=1),
        ram_probe=lambda: 1.0,
        disk_probe=lambda p: 50.0,
        owned_procs=lambda: [],
    )
    m1.sample_once()
    m1.sample_once()
    assert len(m1.ledger['samples']) == 2

    # A brand-new monitor over the same state dir (a "restart") resumes the record and
    # the in-effect pause.
    m2 = ResourceMonitor(
        MonitorConfig(state_dir=tmp_path, breach_samples=1),
        ram_probe=lambda: 9.0,
        disk_probe=lambda p: 50.0,
        owned_procs=lambda: [],
    )
    assert len(m2.ledger['samples']) == 2  # prior history loaded.
    assert m2.poll_pause() is True  # pause flag survived the restart.


# --------------------------------------------------------------------------- #
# Reaper — injected CPU/age (deterministic), no real processes.
# --------------------------------------------------------------------------- #


class _FakeProc:
    def __init__(self, pid: int, alive: bool = True) -> None:
        self.pid = pid
        self._alive = alive

    def poll(self) -> int | None:
        return None if self._alive else 0


def test_reaper_kills_sustained_stall_spares_busy(tmp_path: Path) -> None:
    idle = _FakeProc(101)
    busy = _FakeProc(202)
    killed: list[int] = []

    m = ResourceMonitor(
        MonitorConfig(state_dir=tmp_path, stall_samples=3, stall_cpu_pct=1.0, breach_samples=999),
        ram_probe=lambda: 32.0,
        disk_probe=lambda p: 50.0,
        owned_procs=lambda: [idle, busy],
        cpu_of=lambda pid: 0.0 if pid == 101 else 95.0,
        age_of=lambda pid: 10.0,
        kill=lambda proc: killed.append(proc.pid),
    )
    m.sample_once()
    m.sample_once()
    assert killed == []  # two low samples < the 3-sample stall window.
    m.sample_once()
    assert killed == [101]  # the sustained-idle one reaped; the busy one spared.
    reasons = {r['pid']: r['reason'] for r in m.ledger['reaps']}
    assert reasons == {101: 'stall'}


def test_reaper_kills_hang_by_age(tmp_path: Path) -> None:
    old = _FakeProc(303)
    killed: list[int] = []
    m = ResourceMonitor(
        MonitorConfig(state_dir=tmp_path, hang_ceiling_s=100.0, breach_samples=999),
        ram_probe=lambda: 32.0,
        disk_probe=lambda p: 50.0,
        owned_procs=lambda: [old],
        cpu_of=lambda pid: 90.0,  # busy, but too OLD.
        age_of=lambda pid: 500.0,
        kill=lambda proc: killed.append(proc.pid),
    )
    m.sample_once()
    assert killed == [303]
    assert m.ledger['reaps'][0]['reason'] == 'hang'


def test_reaper_kills_orphan_not_in_active_set(tmp_path: Path) -> None:
    owned = _FakeProc(404)
    killed: list[int] = []
    m = ResourceMonitor(
        MonitorConfig(state_dir=tmp_path, breach_samples=999),
        ram_probe=lambda: 32.0,
        disk_probe=lambda p: 50.0,
        owned_procs=lambda: [owned],
        active_pids=lambda: set(),  # the batch no longer counts 404 as active.
        cpu_of=lambda pid: 90.0,
        age_of=lambda pid: 5.0,
        kill=lambda proc: killed.append(proc.pid),
    )
    m.sample_once()
    assert killed == [404]
    assert m.ledger['reaps'][0]['reason'] == 'orphan'


def test_reaper_stall_counter_resets_on_activity(tmp_path: Path) -> None:
    proc = _FakeProc(505)
    killed: list[int] = []
    cpu = iter([0.0, 0.0, 90.0, 0.0, 0.0])  # activity in the middle resets the tally.

    m = ResourceMonitor(
        MonitorConfig(state_dir=tmp_path, stall_samples=3, breach_samples=999),
        ram_probe=lambda: 32.0,
        disk_probe=lambda p: 50.0,
        owned_procs=lambda: [proc],
        cpu_of=lambda pid: next(cpu),
        age_of=lambda pid: 5.0,
        kill=lambda proc: killed.append(proc.pid),
    )
    for _ in range(5):
        m.sample_once()
    assert killed == []  # never 3 consecutive idle samples.


def test_reaper_skips_dead_owned_proc(tmp_path: Path) -> None:
    dead = _FakeProc(606, alive=False)
    killed: list[int] = []
    m = ResourceMonitor(
        MonitorConfig(state_dir=tmp_path, hang_ceiling_s=1.0, breach_samples=999),
        ram_probe=lambda: 32.0,
        disk_probe=lambda p: 50.0,
        owned_procs=lambda: [dead],
        cpu_of=lambda pid: 0.0,
        age_of=lambda pid: 9999.0,
        kill=lambda proc: killed.append(proc.pid),
    )
    m.sample_once()
    assert killed == []  # a proc that already exited is never re-killed.


# --------------------------------------------------------------------------- #
# Reaper against a REAL self-spawned child (own-lineage discipline, real kill).
# --------------------------------------------------------------------------- #


def test_reaper_kills_real_idle_child_spares_busy(tmp_path: Path) -> None:
    """Spawns a genuinely-idle child (``sleep``) and a genuinely-busy one (a spin loop),
    both in THIS test's own session, and proves the reaper kills the idle one via the real
    ``_kill_process_group`` path while sparing the busy one. Only test-owned pids ever die."""
    idle = subprocess.Popen(['sleep', '60'], start_new_session=True)
    busy = subprocess.Popen(
        [sys.executable, '-c', 'import time\nwhile True: time.sleep(0)'],
        start_new_session=True,
    )
    try:
        # Real CPU readings via the default ps-based probes; stall in one sample.
        m = ResourceMonitor(
            MonitorConfig(state_dir=tmp_path, stall_samples=1, stall_cpu_pct=1.0, breach_samples=999),
            ram_probe=lambda: 32.0,
            disk_probe=lambda p: 50.0,
            owned_procs=lambda: [idle, busy],
        )
        time.sleep(0.5)  # let the busy child accumulate CPU% before we sample.
        m.sample_once()
        # The idle child is reaped (its group SIGKILLed); the busy one survives.
        assert idle.wait(timeout=5) is not None
        assert busy.poll() is None
        assert any(r['pid'] == idle.pid and r['reason'] == 'stall' for r in m.ledger['reaps'])
    finally:
        for p in (idle, busy):
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:
                pass


def test_owned_procs_defaults_to_active_procs_registry(tmp_path: Path) -> None:
    """The reaper's default lineage source IS the runner's own live-JVM registry — the
    only set it may touch. No broad-kill path exists."""
    from pipeline.sim import runner

    m = ResourceMonitor(MonitorConfig(state_dir=tmp_path))
    # Default owned-procs provider reads the process-global registry, empty here.
    assert list(m._owned_procs()) == list(runner._ACTIVE_PROCS)


def test_start_stop_runs_loop(tmp_path: Path) -> None:
    samples = {'n': 0}

    def ram() -> float:
        samples['n'] += 1
        return 32.0

    m = ResourceMonitor(
        MonitorConfig(state_dir=tmp_path, sample_interval_s=0.05, breach_samples=999),
        ram_probe=ram,
        disk_probe=lambda p: 50.0,
        owned_procs=lambda: [],
    )
    m.start()
    time.sleep(0.3)
    m.stop(timeout=2)
    assert samples['n'] >= 1
    assert len(m.ledger['samples']) >= 1
