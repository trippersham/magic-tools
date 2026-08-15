"""The Forge backend behind the :class:`~pipeline.sim.engine.SimEngine` seam.

This is a PROVISIONAL adapter: it satisfies the engine Protocol by DELEGATING to
today's stock-``sim`` path (:func:`pipeline.sim.forge_runtime.resolve` /
:func:`~pipeline.sim.forge_runtime.ensure` and
:func:`pipeline.sim.runner.run_matchup`) so behaviour stays byte-identical to the
pre-seam CLI. It does NOT yet launch the sim-AI harness or expose
hand-visibility / counter metrics — :meth:`ForgeEngine.capabilities` describes
today's stock-heuristic reality, and a later task flips those flags when the
harness lands.

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

#: The runner's own default external per-game kill budget (seconds); used when a
#: caller passes ``timeout_s=None`` so delegation stays byte-identical to a bare
#: :func:`pipeline.sim.runner.run_matchup` call.
_DEFAULT_TIMEOUT_S = 30

#: Provisional capabilities describing TODAY's stock-heuristic Forge sim (NOT the
#: later sim-AI harness). Forge's stock AI has no observable hand info and
#: effectively never counters; it names the kill source in its verbose log. A
#: later task flips ``has_hand_visibility`` / ``has_counter_metrics`` when the
#: harness + hand-visibility land.
_FORGE_CAPABILITIES = EngineCapabilities(
    has_hand_visibility=False,
    has_counter_metrics=False,
    expected_nondecisive_rate=0.05,
    reliability_note='stock heuristic AI; sim-AI harness + hand-visibility land in a later task',
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
        """The provisional stock-heuristic Forge capabilities (see module note)."""
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
