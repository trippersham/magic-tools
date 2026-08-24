"""The Forge backend behind the :class:`~pipeline.sim.engine.SimEngine` seam.

This adapter satisfies the engine Protocol by DELEGATING to
:func:`pipeline.sim.forge_runtime.resolve` / :func:`~pipeline.sim.forge_runtime.ensure`
and :func:`pipeline.sim.runner.run_matchup`. The runner now launches the committed
sim-AI HARNESS (``SimAIMatch -sim 1`` — Forge's real simulation AI) rather than the
stock heuristic ``sim`` verb, so :meth:`ForgeEngine.capabilities` reports sim-AI
reality (hand-visibility + counter metrics observable via the HANDLOG stream). Wiring
those HANDLOG piloting metrics into results is a later task; this exposes the flags.

The module registers a singleton :class:`ForgeEngine` at import
(:func:`~pipeline.sim.engine.register_engine`), so importing the sim package (which
imports :mod:`pipeline.sim.engines`) makes ``get_engine('forge')`` resolve.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import TYPE_CHECKING

from pipeline.sim import forge_runtime, runner
from pipeline.sim.engine import (
    EngineCapabilities,
    EngineInstall,
    EngineUnavailableError,
    register_engine,
)
from pipeline.sim.forge_runtime import FORGE_VERSION, ForgeInstall, ForgeUnavailableError
from pipeline.sim.runner import _HARNESS_JAR

if TYPE_CHECKING:
    from pathlib import Path

    from pipeline.sim.runner import MatchResult

__all__ = ('ForgeEngine',)

#: The per-game in-game draw-clock budget (``-c``, seconds) the engine passes to
#: :func:`pipeline.sim.runner.run_matchup`. This is NOT a subprocess timeout — it
#: is Forge's own clock that ENDS a running game as a draw when it fires, and a
#: clocked-out game is a fabricated-win landmine (see B1b in the runner). Real
#: sim-AI games take 23-38s+ empirically (the repo fixture ``handlog_azorius.log``:
#: 38541/23730/23075 ms), so 30 guaranteed clockouts in bulk (~40-60% of a run).
#: Raised to 90s — comfortably past the observed tail while staying under the
#: harness's own 120 default; the external kill budget
#: (``_JVM_LOAD_HEADROOM_S + n*timeout_s``) scales with it and stays sane
#: (e.g. n=2 → 120 + 180 = 300s wall, well inside a real batch).
_DEFAULT_TIMEOUT_S = 90

#: The commander (1v1 EDH) per-game draw clock. EDH games are LONG — at the 90s
#: constructed clock commander is ~100% non-decisive (every game clocks out), so a
#: commander run at the default learns nothing. Raised to 300s so games can actually
#: DECIDE (R2-4). Only the None-default is format-aware — an explicit ``timeout_s``
#: still wins. The external-kill budget stays sane: even at n=4 the wall bound is
#: ``_JVM_LOAD_HEADROOM_S + 4*300 = 1320s`` (~22 min), well inside a real batch.
_COMMANDER_TIMEOUT_S = 300

#: Capabilities describing the LIVE sim-AI harness reality (the runner now launches
#: ``org.makemagic.simai.SimAIMatch -sim 1`` — Forge's depth-3 simulation AI with the
#: real-time HANDLOG stream). The HANDLOG exposes player-1's hand + stack casts, so
#: hand-visibility and counter (stack-interaction) metrics are observable. It still
#: names the kill source in its verbose log. (The HANDLOG piloting-metric WIRING is
#: task 1.6b; this only reflects the flags now that the sim AI actually runs.)
#:
#: ``expected_nondecisive_rate`` is the EMPIRICAL constructed value (~0.30), not a
#: hopeful floor: real sim-AI runs are non-decisive roughly a third of the time (R2-2),
#: a mix of true clock-outs, marker-less fast forced-draws, and occasional NPEs — all
#: counted as draws, never fabricated wins. Commander runs much longer and is
#: lower-signal at any clock (see ``_COMMANDER_TIMEOUT_S`` / R2-4).
_FORGE_CAPABILITIES = EngineCapabilities(
    has_hand_visibility=True,
    has_counter_metrics=True,
    expected_nondecisive_rate=0.30,
    reliability_note=(
        'Forge built-in simulation AI (AIOption.USE_SIMULATION, depth-3 lookahead) via the committed '
        'SimAIMatch harness, with a real-time HANDLOG decision stream. Sim-AI games are frequently '
        'non-decisive — empirically ~30% at the 90s constructed draw clock: a MIX of true clock-outs '
        '(a stalled board runs out the clock), fast marker-less "forced draws" (Forge startGame returns '
        'without a game-over, printing a dual-win Game Outcome block with NO "Stopping slow match" '
        'marker), and occasional sim-AI NPEs. All are counted as draws, never fabricated wins. Kill '
        'source is named in the verbose log. Commander runs MUCH longer and is lower-signal even at the '
        'raised 300s clock (EDH games are long).'
    ),
    kill_attribution='named',
)


@lru_cache(maxsize=1)
def _harness_jarhash() -> str:
    """A stable short sha256 (first 10 hex) of the committed sim-AI harness jar.

    This binds the reported engine version to the ACTUAL harness build so any
    harness/AI change busts the content cache (a stock-heuristic row and a sim-AI
    row hash to different ``matchup_key``\\ s instead of colliding). Computed ONCE
    (:func:`~functools.lru_cache`) — the jar is small (~25 KB) and its bytes never
    change within a process. If the jar is missing at compute time (the separate
    1.6c packaging case), fall back to ``'unknown'`` rather than crashing the
    version lookup — a missing jar is a resolve/run problem surfaced elsewhere,
    not a reason to blow up the cache-key derivation.
    """
    try:
        digest = hashlib.sha256(_HARNESS_JAR.read_bytes()).hexdigest()
    except OSError:
        return 'unknown'
    return digest[:10]


def _engine_version() -> str:
    """The reported backend version: ``<FORGE_VERSION>+simai-<jarhash>``.

    Folds the sim-AI harness identity into :attr:`EngineInstall.version`, which
    flows unchanged through :mod:`pipeline.sim.core` into
    :func:`pipeline.sim.store.matchup_key` (already keyed on ``engine_version``) —
    so rebuilding the harness (new jar bytes → new hash) self-invalidates the
    cache. Missing jar → ``<FORGE_VERSION>+simai-unknown``.
    """
    return f'{FORGE_VERSION}+simai-{_harness_jarhash()}'


class ForgeEngine:
    """A :class:`~pipeline.sim.engine.SimEngine` that delegates to the Forge runner.

    Behaviour-preserving: :meth:`resolve` wraps
    :func:`pipeline.sim.forge_runtime.resolve` / ``ensure`` and :meth:`run_matchup`
    unwraps the install handle and calls the unchanged
    :func:`pipeline.sim.runner.run_matchup`, so a run through this engine is
    byte-identical to the pre-seam path.
    """

    name = 'forge'

    def capabilities(self) -> EngineCapabilities:
        """The live sim-AI harness capabilities (see module note)."""
        return _FORGE_CAPABILITIES

    def resolve(self, *, provision: bool, data_dir: Path | None = None) -> EngineInstall:
        """Resolve a Forge install via the runtime, wrapped in an :class:`EngineInstall`.

        ``provision`` picks fetch-on-miss (:func:`~pipeline.sim.forge_runtime.ensure`)
        vs read-only (:func:`~pipeline.sim.forge_runtime.resolve`); ``data_dir``
        threads through to the runtime's cache root. The one-time download notice
        UX is preserved by passing :func:`_forge_fetch_notice` as ``on_fetch``. A
        :class:`~pipeline.sim.forge_runtime.ForgeUnavailableError` is re-raised AS
        :class:`~pipeline.sim.engine.EngineUnavailableError` (message preserved) so
        callers get the seam's never-crash contract.
        """
        try:
            handle: ForgeInstall = (
                forge_runtime.ensure(data_dir=data_dir, on_fetch=_forge_fetch_notice)
                if provision
                else forge_runtime.resolve(data_dir=data_dir)
            )
        except ForgeUnavailableError as exc:
            raise EngineUnavailableError(str(exc)) from exc
        return EngineInstall(version=_engine_version(), handle=handle)

    def run_matchup(
        self,
        deck_a: tuple[str, str],
        deck_b: tuple[str, str],
        *,
        n: int,
        seed: int,
        fmt: str,
        install: EngineInstall,
        timeout_s: int | None = None,
    ) -> MatchResult:
        """Delegate to :func:`pipeline.sim.runner.run_matchup`.

        Unwraps ``install.handle`` (a :class:`~pipeline.sim.forge_runtime.ForgeInstall`)
        and calls the runner. When ``timeout_s`` is ``None`` the per-game draw clock
        is FORMAT-AWARE: commander (long EDH games) uses ``_COMMANDER_TIMEOUT_S``
        (300s) so games can decide rather than 100% clock out at 90s (R2-4); every
        other format uses ``_DEFAULT_TIMEOUT_S`` (90s). An explicit ``timeout_s``
        overrides both.
        """
        handle: ForgeInstall = install.handle
        if timeout_s is None:
            timeout_s = _COMMANDER_TIMEOUT_S if fmt == 'commander' else _DEFAULT_TIMEOUT_S
        return runner.run_matchup(
            handle,
            deck_a,
            deck_b,
            n=n,
            seed=seed,
            fmt=fmt,
            timeout_s=timeout_s,
        )

    def replay(self, matchup_key: str, game_idx: int) -> str:
        """Return the stored verbose log for one game of a prior matchup.

        Delegates to :func:`pipeline.sim.store.get_game_logs` (the same retrieval
        the ``log`` verb uses), returning the single game's log or ``''`` when no
        log is stored for that ``(matchup_key, game_idx)``.
        """
        from pipeline.sim.store import get_game_logs

        logs = get_game_logs(matchup_key, game_index=game_idx)
        return logs[0] if logs else ''


def _forge_fetch_notice() -> None:
    """One-time 'downloading Forge' notice (stderr) — mirrors the CLI's notice.

    Called once by :func:`~pipeline.sim.forge_runtime.ensure` right before the
    ~350 MB provision download, so a first run through the engine doesn't appear
    to hang.
    """
    import sys

    print(
        f'Forge not found locally — downloading Forge {FORGE_VERSION} + a JRE '
        '(~350MB, one-time; reused after). This may take a few minutes…',
        file=sys.stderr,
    )


register_engine(ForgeEngine())
