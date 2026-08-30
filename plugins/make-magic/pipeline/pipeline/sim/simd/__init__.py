"""The simd engine core: fair round-robin scheduling, per-cell attempt-cap + persistent
quarantine, and the crash-safe ``ops.duckdb`` operational store (Phase A2).

Built as NEW modules alongside the legacy ``game_queue`` / ``worker_pool`` stack; A3 routes the
live path through :func:`~pipeline.sim.simd.engine.run_games_simd` and retires the old code.
"""

from __future__ import annotations

from pipeline.sim.simd.circuit_breaker import CrashLoopBreaker
from pipeline.sim.simd.engine import SimdRunResult, run_games_simd
from pipeline.sim.simd.governor import Admission, DiskGovernor, stable_pool_size
from pipeline.sim.simd.ops_store import OpsStore
from pipeline.sim.simd.preflight import BootFailure, preflight_java
from pipeline.sim.simd.reaper import AlreadyRunning, SingletonLock, reap_orphan_tree
from pipeline.sim.simd.scheduler import SimdScheduler

__all__ = (
    'Admission',
    'AlreadyRunning',
    'BootFailure',
    'CrashLoopBreaker',
    'DiskGovernor',
    'OpsStore',
    'SimdRunResult',
    'SimdScheduler',
    'SingletonLock',
    'preflight_java',
    'reap_orphan_tree',
    'run_games_simd',
    'stable_pool_size',
)
