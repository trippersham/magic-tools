"""The backend-agnostic simulation-engine seam (Forge / XMage behind one type).

The sim core (:mod:`pipeline.sim.core`) currently binds directly to Forge — it
imports :func:`pipeline.sim.runner.run_matchup` and threads a concrete
:class:`~pipeline.sim.forge_runtime.ForgeInstall`. This module introduces the
abstraction that a later task rewires the core onto: a :class:`SimEngine`
Protocol every backend implements, a shared :class:`EngineCapabilities`
descriptor so downstream code can branch on what a backend can/can't measure
(hand visibility, counter metrics, how a kill is attributed), and a tiny
module-level registry.

Nothing here runs a JVM or touches the network. It is a pure vocabulary +
registry; the concrete engines (and the core rewiring) land in later tasks. The
value types deliberately REUSE — never redefine — the real telemetry/result
types: :class:`~pipeline.sim.runner.MatchResult` is imported and returned as-is.

Design mirrors :mod:`pipeline.sim.forge_runtime`:

  * :class:`EngineUnavailableError` mirrors
    :class:`~pipeline.sim.forge_runtime.ForgeUnavailableError` — an actionable,
    never-crash-the-caller signal that a backend can't be reached.
  * :meth:`SimEngine.resolve` unifies that module's ``resolve`` (read-only) and
    ``ensure`` (fetch-at-runtime) behind a single ``provision`` flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from pipeline.sim.runner import MatchResult

__all__ = (
    'EngineCapabilities',
    'EngineInstall',
    'EngineUnavailableError',
    'SimEngine',
    'available_engines',
    'get_engine',
    'register_engine',
)


class EngineUnavailableError(RuntimeError):
    """A simulation backend could not be resolved (and, for a non-provisioning
    :meth:`SimEngine.resolve`, none was fetched).

    Mirrors :class:`~pipeline.sim.forge_runtime.ForgeUnavailableError`: the
    caller is NEVER crashed by a missing backend — an engine raises this with an
    actionable message (how to point at / provision the install) instead of
    leaking a raw ``FileNotFoundError`` / JVM failure.
    """


@dataclass(frozen=True)
class EngineCapabilities:
    """What a backend can (and cannot) measure — so downstream code branches on
    facts rather than hard-coding Forge's behaviour.

    * ``has_hand_visibility`` — the backend's log exposes hidden-zone (hand)
      information a metric could read.
    * ``has_counter_metrics`` — countermagic / stack interaction is observable in
      the log (Forge's AI effectively never counters; XMage's does).
    * ``expected_nondecisive_rate`` — the fraction of games expected to end
      without a clean decisive result (draws / clock exhaustion), so an
      aggregate can flag an anomalously high rate.
    * ``reliability_note`` — a human-readable caveat surfaced next to results.
    * ``kill_attribution`` — how the backend names the killer: ``'named'`` (Forge
      logs the specific source) vs ``'combat_generic'`` (XMage logs only a
      generic ``combat`` source). Downstream MUST NOT read a ``combat_generic``
      backend's kill source as a specific card.
    """

    has_hand_visibility: bool
    has_counter_metrics: bool
    expected_nondecisive_rate: float
    reliability_note: str
    kill_attribution: Literal['named', 'combat_generic']


@dataclass(frozen=True)
class EngineInstall:
    """A resolved, launchable backend install — the seam's opaque install token.

    ``version`` is the backend version folded into cache keys (mirrors
    :data:`pipeline.sim.forge_runtime.FORGE_VERSION`). ``handle`` is a
    backend-private object the engine uses internally to actually launch a run
    (e.g. a :class:`~pipeline.sim.forge_runtime.ForgeInstall`); callers treat it
    as opaque and pass it straight back to :meth:`SimEngine.run_matchup`. Kept
    minimal on purpose — an engine composes its concrete install inside ``handle``
    rather than this type growing backend-specific fields.
    """

    version: str
    handle: Any


@runtime_checkable
class SimEngine(Protocol):
    """A backend that runs AI-vs-AI matchups and reports its capabilities.

    ``runtime_checkable`` so the registry / callers can ``isinstance``-verify a
    duck-typed engine. Every method is backend-agnostic; the concrete Forge and
    XMage engines implement it in later tasks.
    """

    @property
    def name(self) -> str:
        """The backend's stable id — ``'forge'`` or ``'xmage'``."""
        ...

    def capabilities(self) -> EngineCapabilities:
        """This backend's measurement capabilities (see :class:`EngineCapabilities`)."""
        ...

    def resolve(self, *, provision: bool, data_dir: Path | None = None) -> EngineInstall:
        """Resolve a launchable install; NEVER crash the caller on absence.

        ``provision=False`` is read-only (locate an existing install); ``True``
        may fetch/provision it. ``data_dir`` overrides the cache root the backend
        resolves/fetches into (``None`` = the default data dir); the offline test
        suite passes a tmp path here for isolation, mirroring
        :func:`pipeline.sim.forge_runtime.resolve`'s ``data_dir`` kwarg. Raises
        :class:`EngineUnavailableError` — with an actionable message — when the
        backend cannot be made available.
        """
        ...

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
        """Run ``n`` games of ``deck_a`` (slot 1) vs ``deck_b`` (slot 2).

        ``deck_a`` / ``deck_b`` are ``(name, dck_text)`` pairs (as the Forge
        runner already consumes). ``install`` is a token from :meth:`resolve`.
        ``timeout_s`` is the external per-run JVM kill budget (``None`` = the
        engine's own default); it preserves the wall-clock kill-switch the real
        :func:`pipeline.sim.runner.run_matchup` exposes, and lets a caller widen
        it for slower formats (e.g. Commander). Returns the real
        :class:`~pipeline.sim.runner.MatchResult` (imported, not redefined).
        """
        ...

    def replay(self, matchup_key: str, game_idx: int) -> str:
        """Return the stored verbose log for one game of a prior matchup (forensic).

        ``matchup_key`` addresses a previously-run matchup; ``game_idx`` selects
        the game within it. Used to re-inspect a decision after the fact without
        re-running the JVM.
        """
        ...


# --------------------------------------------------------------------------- #
# registry — a plain module-level dict + functions (matches the repo's style)
# --------------------------------------------------------------------------- #

#: name -> engine. A plain dict keyed by :attr:`SimEngine.name`; a re-register
#: under the same name overwrites (last registration wins).
_REGISTRY: dict[str, SimEngine] = {}


def register_engine(engine: SimEngine) -> None:
    """Register ``engine`` under its :attr:`~SimEngine.name` (last write wins)."""
    _REGISTRY[engine.name] = engine


def get_engine(name: str) -> SimEngine:
    """Return the engine registered as ``name``.

    Raises :class:`KeyError` with a message naming the unknown key AND the known
    engines, so an unknown-name failure is self-explaining rather than a bare
    ``KeyError('name')``.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ', '.join(sorted(_REGISTRY)) or '(none registered)'
        raise KeyError(f'unknown sim engine {name!r}; known engines: {known}') from None


def available_engines() -> list[str]:
    """The registered engine names, sorted for a stable listing."""
    return sorted(_REGISTRY)
