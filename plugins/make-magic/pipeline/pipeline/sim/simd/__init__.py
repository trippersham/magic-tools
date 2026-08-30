"""The simd engine core: fair round-robin scheduling, per-cell attempt-cap + persistent
quarantine, and the crash-safe ``ops.duckdb`` operational store (Phase A2).

Built as NEW modules alongside the legacy ``game_queue`` / ``worker_pool`` stack; A3 routes the
live path through :func:`~pipeline.sim.simd.engine.run_games_simd` and retires the old code.
"""

from __future__ import annotations

from pipeline.sim.simd.engine import SimdRunResult, run_games_simd
from pipeline.sim.simd.ops_store import OpsStore
from pipeline.sim.simd.scheduler import SimdScheduler

__all__ = ('OpsStore', 'SimdRunResult', 'SimdScheduler', 'run_games_simd')
