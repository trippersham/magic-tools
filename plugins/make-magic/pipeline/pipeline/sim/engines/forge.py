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

from typing import TYPE_CHECKING

from pipeline.sim import forge_runtime, runner
from pipeline.sim.engine import (
    EngineCapabilities,
    EngineInstall,
    EngineUnavailableError,
    register_engine,
)
from pipeline.sim.forge_runtime import FORGE_VERSION, ForgeInstall, ForgeUnavailableError

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

#: Capabilities describing the LIVE sim-AI harness reality (the runner now launches
#: ``org.makemagic.simai.SimAIMatch -sim 1`` — Forge's depth-3 simulation AI with the
#: real-time HANDLOG stream). The HANDLOG exposes player-1's hand + stack casts, so
#: hand-visibility and counter (stack-interaction) metrics are observable; the sim AI
#: is ~12% fragile (NPE / sim-timeout / clock-out → nondecisive). It still names the
#: kill source in its verbose log. (The HANDLOG piloting-metric WIRING is task 1.6b;
#: this only reflects the flags now that the sim AI actually runs.)
_FORGE_CAPABILITIES = EngineCapabilities(
    has_hand_visibility=True,
    has_counter_metrics=True,
    expected_nondecisive_rate=0.05,
    reliability_note=(
        'Forge built-in simulation AI (AIOption.USE_SIMULATION, depth-3 lookahead) via the committed '
        'SimAIMatch harness, with a real-time HANDLOG decision stream. At the 90s per-game draw clock a '
        'small fraction of games (~5%) still end nondecisively — a sim-AI NPE, or a genuinely stalled '
        'board that runs out the clock (counted as a draw, never a fabricated win). Kill source is named '
        'in the verbose log.'
    ),
    kill_attribution='named',
)


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
        return EngineInstall(version=FORGE_VERSION, handle=handle)

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
        """Delegate to :func:`pipeline.sim.runner.run_matchup` (byte-identical).

        Unwraps ``install.handle`` (a :class:`~pipeline.sim.forge_runtime.ForgeInstall`)
        and calls the unchanged runner; ``timeout_s=None`` uses the runner's own
        default so the call matches the pre-seam behaviour exactly.
        """
        handle: ForgeInstall = install.handle
        return runner.run_matchup(
            handle,
            deck_a,
            deck_b,
            n=n,
            seed=seed,
            fmt=fmt,
            timeout_s=_DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s,
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
