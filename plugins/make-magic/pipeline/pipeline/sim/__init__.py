"""Sim — AI-vs-AI matchup simulation via headless MTG Forge.

Two building blocks (Phase 2): :mod:`pipeline.sim.forge_runtime` locates (or
fetches) a launchable Forge install, and :mod:`pipeline.sim.runner` runs ONE
matchup and tallies the result. Higher layers (telemetry, governor, cache,
core, CLI) build on top of these later.

    from pipeline.sim import ensure, run_matchup, deck_to_dck
    install = ensure()
    result = run_matchup(install, ('A', dck_a), ('B', dck_b), n=10, seed=42)
"""

from __future__ import annotations

from pipeline.sim.core import (
    Comparison,
    MatchOutcome,
    OpponentResult,
    SimResult,
    TelemetryProfile,
    compare,
    run_cached_matchups,
    simulate,
    wilson_ci,
)
from pipeline.sim.forge_runtime import (
    ForgeInstall,
    ForgeUnavailableError,
    ensure,
    resolve,
)
from pipeline.sim.gauntlet import GauntletDeck, resolve_gauntlet
from pipeline.sim.governor import (
    Governor,
    MatchFailure,
    MatchSpec,
    PoolResult,
    derive_pool_size,
    run_matchups,
)
from pipeline.sim.runner import (
    ForgeError,
    GameOutcome,
    MatchResult,
    deck_to_dck,
    parse_match_log,
    run_matchup,
)
from pipeline.sim.telemetry import (
    GameFeatures,
    extract_game_features,
    extract_match_features,
    split_games,
)

__all__ = (
    'Comparison',
    'ForgeError',
    'ForgeInstall',
    'ForgeUnavailableError',
    'GameFeatures',
    'GameOutcome',
    'GauntletDeck',
    'Governor',
    'MatchFailure',
    'MatchOutcome',
    'MatchResult',
    'MatchSpec',
    'OpponentResult',
    'PoolResult',
    'SimResult',
    'TelemetryProfile',
    'compare',
    'deck_to_dck',
    'derive_pool_size',
    'ensure',
    'extract_game_features',
    'extract_match_features',
    'parse_match_log',
    'resolve',
    'resolve_gauntlet',
    'run_cached_matchups',
    'run_matchup',
    'run_matchups',
    'simulate',
    'split_games',
    'wilson_ci',
)
