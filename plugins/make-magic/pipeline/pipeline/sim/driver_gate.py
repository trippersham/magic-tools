"""Phase 5 -- the per-deck **dual-mode solo** driver ship gate for the in-search quad.

A quad driver authored by :mod:`pipeline.sim.driver_authoring` (``render_quad_driver`` over a
:class:`~pipeline.sim.driver_authoring.QuadSpec`) is only trusted once it clears two gates
against the real XMage dist jar:

  1. **Compile gate** (:func:`compile_quad_driver`): ECJ-compile the rendered ``Driver.java``
     against the SHA-pinned dist jar on a plain JRE (no JDK/``javac`` — that dependency is gone;
     see :mod:`pipeline.sim.driver_compile`). A compile failure raises
     :class:`~pipeline.sim.driver_compile.DriverCompileError` with structured diagnostics.
  2. **Behavioral gate** (:func:`gate_driver`) over the Phase-1 SOLO own-turn goldfish clock,
     routed by the quad's shape (the presence of a macro):

     * **Registration is universal.** Both modes require the seam's ``DRIVER_REGISTERED`` line
       in the driven output — the P3 fail-loud already raises if a requested driver never
       registers, so this is belt-and-suspenders.
     * **Proactive (has a macro):** PASS iff it registers **and** the frozen dist's
       ``MACRO_FIRE_REAL`` marker appears >=1x (the macro **really executed** to win on the
       real game in ``act()``) **and** the driven own-turn clock is **never-slower** than a
       throwaway pure-CP7 baseline (``medianKillsOwn`` <= baseline + tolerance). The emitter's
       ``DRIVER_MACRO_FIRED`` marker (printed at the top of the macro's ``apply()``) is parsed
       too, but it only proves **reachability**: the seam calls ``apply()`` on THROWAWAY search
       copies at every node where P holds AND in real ``act()``, so its presence means the macro
       is reachable in search — NOT that it executed to win. Only ``MACRO_FIRE_REAL`` (emitted
       exclusively from the real ``act()`` commit path in patch 0003) proves true execution, so
       it — not reachability — is the ship gate.
     * **Reactive (Φ-only, no macro):** PASS iff it registers **and** it meets the
       **never-worse-solo floor** (same never-slower check). There is **no macro-fire
       requirement** — a passive solo goldfish gives a reactive deck nothing to react to. The
       defended-opponent lens is **opt-in** (``defended_lens=``), never a ship blocker.

The own-turn clock keeps the ±2-turn tolerance (:data:`_DEFAULT_TOLERANCE`): XMage has no
reproducible seed, so the driven + baseline medians are two independent noisy samples that
jitter run-to-run; a strict ``<=`` would coin-flip at small ``games`` while a driver that is
*meaningfully* slower (or the ``-1.0`` no-kill sentinel ⇒ ``+inf``) still fails.

Pass ⇒ stamp ``meta.json`` (``gates_passed=True`` + ``gate_mode`` + the compared medians) so
:func:`~pipeline.sim.drivers.driver_valid` turns True. Fail ⇒ stamp nothing and return a
structured :class:`GateResult` naming the failed check, so an authoring loop can act on it.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from pipeline.decks.version import version
from pipeline.sim import driver_compile, drivers
from pipeline.sim.driver_authoring import (
    DRIVER_MACRO_FIRED_MARKER,
    DRIVER_STEER_FIRED_MARKER,
    check_quad_guardrails,
    driver_fqcn,
    render_quad_driver,
)

if TYPE_CHECKING:
    from pipeline.contracts import Deck
    from pipeline.sim.driver_authoring import QuadSpec
    from pipeline.sim.engine import EngineInstall
    from pipeline.sim.engines.xmage import GoldfishResult

__all__ = (
    'DRIVER_REGISTERED_MARKER',
    'MACRO_FIRE_REAL_MARKER',
    'GateResult',
    'compile_quad_driver',
    'gate_driver',
)

#: The seam's registration marker: ``XMageBatch`` prints ``DRIVER_REGISTERED fqcn=... playerId=...``
#: on stdout the instant it reflectively invokes a driver's ``register(UUID)`` on PlayerA. Its
#: presence in the driven output is the universal "the quad wired itself" signal both modes assert.
DRIVER_REGISTERED_MARKER = 'DRIVER_REGISTERED'

#: The frozen dist's REAL-fire marker: patch 0003's ``ComputerPlayer6.act()`` prints
#: ``MACRO_FIRE_REAL name=<getName()> turn=<n>`` on stderr the instant it COMMITS the registered
#: combo macro on the REAL game (not a search copy). This is true execution — the proactive ship
#: gate keys on it, NOT on the emitter's ``DRIVER_MACRO_FIRED`` (which the seam also prints from
#: apply() on throwaway search copies, so that one only proves REACHABILITY).
MACRO_FIRE_REAL_MARKER = 'MACRO_FIRE_REAL'

#: Hard floor on the gate's per-run game count. The never-slower check compares two seedless,
#: independently-jittery medians; below this count the check flakes (n=8 flaked, n=15 was stable),
#: and a silently-underpowered gate is worse than a loud stop. Both modes are jitter-sensitive.
_MIN_GATE_GAMES = 12

#: Own-turn-kill slack allowed on the never-slower check. XMage has no reproducible seed, so the
#: driven + baseline solo medians are two independent noisy samples that jitter ~±1 own-turn
#: between runs. 2 turns of slack makes a neutral driver pass deterministically while a driver
#: that is genuinely slower (or that stops killing → the +inf sentinel) still fails.
_DEFAULT_TOLERANCE = 2.0


@dataclass(frozen=True)
class GateResult:
    """The dual-mode gate's verdict for one quad driver.

    ``passed`` is the mode-appropriate conjunction. ``mode`` is ``'proactive'`` or
    ``'reactive'`` (routed by whether the quad carries a macro). ``reason`` names the failed
    check (empty on pass). ``registered`` / ``macro_fired`` / ``macro_reachable`` /
    ``steer_fired`` record which markers were seen in the driven output. ``macro_fired`` = the
    macro **really executed** to win (the frozen dist's ``MACRO_FIRE_REAL``, emitted only from the
    real ``act()`` commit) — what the proactive gate keys on. ``macro_reachable`` = the emitter's
    ``DRIVER_MACRO_FIRED`` was seen (apply() was entered) — but the seam runs apply() on THROWAWAY
    search copies as well as in real act(), so this only proves reachability, NOT execution.
    ``markers_seen`` is the set of those marker names for a loop to introspect. ``driven_median``
    / ``baseline_median`` are the raw ``medianKillsOwn`` values compared (``-1.0`` = the harness
    "never killed" sentinel).
    """

    passed: bool
    mode: str
    reason: str = ''
    registered: bool = False
    macro_fired: bool = False
    macro_reachable: bool = False
    steer_fired: bool = False
    markers_seen: frozenset[str] = frozenset()
    driven_median: float | None = None
    baseline_median: float | None = None
    fqcn: str = ''
    extra: dict[str, object] = field(default_factory=dict)


class _GoldfishEngine(Protocol):
    """The slice of :class:`~pipeline.sim.engines.xmage.XMageEngine` the gate needs -- a
    Protocol so tests can inject a fake with no JVM."""

    def goldfish_output(
        self,
        deck_a: tuple[str, str],
        *,
        games: int,
        install: EngineInstall,
        driver: tuple[str, str] | None = ...,
    ) -> tuple[GoldfishResult, str]: ...

    def goldfish(
        self,
        deck_a: tuple[str, str],
        *,
        games: int,
        install: EngineInstall,
        driver: tuple[str, str] | None = ...,
    ) -> GoldfishResult: ...


def _default_engine() -> _GoldfishEngine:
    """The registered XMage engine singleton (the real solo runner)."""
    from pipeline.sim.engine import get_engine

    return get_engine('xmage')  # type: ignore[return-value]


def compile_quad_driver(
    deck: Deck,
    spec: QuadSpec,
    *,
    data_dir: str | os.PathLike[str] | None = None,
) -> tuple[str, str]:
    """Render + guardrail-check + ECJ-compile ``deck``'s quad; return the injection tuple.

    Renders ``spec`` into ``Driver.java`` (:func:`~pipeline.sim.driver_authoring.render_quad_driver`),
    runs the AC8 do-not-own static screen (:func:`~pipeline.sim.driver_authoring.check_quad_guardrails`),
    writes the source into the deck's driver dir, and ECJ-compiles it against the effective dist
    jar via :func:`~pipeline.sim.driver_compile.compile_for_injection` (JRE-only — no ``javac``).
    The compiled class tree (which ECJ writes into a shared ``(dist SHA, source hash)`` cache dir)
    is then published into the deck's own ``classes_dir`` — so the gate AND the production run
    path (:mod:`pipeline.sim.driver_compare`) both inject the SAME per-deck dir keyed by
    ``deck.uuid``. Returns the ``(classes_dir, fqcn)`` tuple the XMage engine's ``driver=`` takes.

    A guardrail violation raises :class:`~pipeline.sim.driver_authoring.GuardrailViolation`; a
    compile failure raises :class:`~pipeline.sim.driver_compile.DriverCompileError` with the
    parsed diagnostics. NO meta is stamped here (the behavioral gate owns the ``gates_passed``
    stamp).
    """
    import shutil
    from pathlib import Path

    source = render_quad_driver(deck, spec)
    check_quad_guardrails(source)  # AC8: raises on a do-not-own violation before we compile.

    ddir = drivers.driver_dir(deck, data_dir=data_dir)
    ddir.mkdir(parents=True, exist_ok=True)
    src_path = ddir / 'Driver.java'
    Path(src_path).write_text(source, encoding='utf-8')

    fqcn = driver_fqcn(deck)
    cache_dir, _ = driver_compile.compile_for_injection(str(src_path), fqcn, data_dir=data_dir)

    # Publish the compiled class tree into the per-deck classes dir the run/gate path injects.
    classes = drivers.classes_dir(deck, data_dir=data_dir)
    shutil.rmtree(classes, ignore_errors=True)  # no stale .class survives a re-author.
    shutil.copytree(cache_dir, classes)
    return str(classes), fqcn


def _kill_metric(median_kills_own: float) -> float:
    """Map a ``medianKillsOwn`` to a comparable "faster is smaller" metric.

    A real kill turn compares directly (turn 6 beats turn 8). The harness ``-1.0`` "never
    killed" sentinel (and any negative) becomes ``+inf`` so a no-kill run is strictly the worst
    -- a driver that stops killing can never pass the never-slower check against a baseline that
    does kill.
    """
    return math.inf if median_kills_own < 0 else median_kills_own


def gate_driver(
    deck: Deck,
    deck_ref: tuple[str, str],
    *,
    spec: QuadSpec,
    install: EngineInstall,
    games: int,
    tolerance: float = _DEFAULT_TOLERANCE,
    engine: _GoldfishEngine | None = None,
    data_dir: str | os.PathLike[str] | None = None,
    defended_lens: bool = False,
) -> GateResult:
    """Run the dual-mode behavioral gate on ``deck``'s compiled quad; stamp on pass.

    ``spec`` routes the mode: a quad WITH a macro is **proactive**, a Φ-only quad is
    **reactive**. ``deck_ref`` is the ``(name, forge_dck_text)`` tuple the solo ``goldfish``
    path consumes (kept explicit so the gate does not couple to the deck exporter / network).
    Runs the DRIVEN solo (markers + median) then a throwaway DRIVERLESS CP7 baseline (median)
    over ``games`` games each, then:

      * requires the ``DRIVER_REGISTERED`` marker (both modes);
      * PROACTIVE also requires ``MACRO_FIRE_REAL`` >=1x (the macro REALLY executed to win in the
        real ``act()`` — NOT merely reachable in search) AND the never-slower own-turn check;
      * REACTIVE requires ONLY the never-worse-solo floor (the same never-slower check) -- no
        macro-fire; ``defended_lens=True`` is an opt-in note, never a blocker.

    ``games`` must be >= :data:`_MIN_GATE_GAMES`; a smaller count raises ``ValueError`` (loud) —
    both modes' never-slower check is seedless-jitter-sensitive and flakes when underpowered.

    On pass, stamps ``meta.json`` (``gates_passed=True``, ``gate_mode``, the compared medians in
    ``extra``) so :func:`~pipeline.sim.drivers.driver_valid` turns True. On fail, stamps nothing
    and returns a failing :class:`GateResult` naming the check.
    """
    if games < _MIN_GATE_GAMES:
        raise ValueError(
            f'gate_driver requires games >= {_MIN_GATE_GAMES} (got {games}): the never-slower '
            'check compares two seedless, independently-jittery medians and flakes when '
            'underpowered — a silently-underpowered gate is worse than a hard stop'
        )
    eng = engine if engine is not None else _default_engine()
    classes = drivers.classes_dir(deck, data_dir=data_dir)
    fqcn = driver_fqcn(deck)
    driver = (str(classes), fqcn)

    is_proactive = spec.macro is not None
    mode = 'proactive' if is_proactive else 'reactive'

    driven, output = eng.goldfish_output(deck_ref, games=games, install=install, driver=driver)
    baseline = eng.goldfish(deck_ref, games=games, install=install, driver=None)

    registered = DRIVER_REGISTERED_MARKER in output
    # macro_fired = REAL execution (act() commit); macro_reachable = apply() entered (search
    # copy OR real). Only the former proves the deterministic win actually ran to win.
    macro_fired = MACRO_FIRE_REAL_MARKER in output
    macro_reachable = DRIVER_MACRO_FIRED_MARKER in output
    steer_fired = DRIVER_STEER_FIRED_MARKER in output
    markers_seen = frozenset(
        m
        for m, seen in (
            (DRIVER_REGISTERED_MARKER, registered),
            (MACRO_FIRE_REAL_MARKER, macro_fired),
            (DRIVER_MACRO_FIRED_MARKER, macro_reachable),
            (DRIVER_STEER_FIRED_MARKER, steer_fired),
        )
        if seen
    )

    driven_median = driven.median_kills_own
    baseline_median = baseline.median_kills_own
    never_slower = _kill_metric(driven_median) <= _kill_metric(baseline_median) + tolerance

    def _fail(reason: str) -> GateResult:
        return GateResult(
            passed=False,
            mode=mode,
            reason=reason,
            registered=registered,
            macro_fired=macro_fired,
            macro_reachable=macro_reachable,
            steer_fired=steer_fired,
            markers_seen=markers_seen,
            driven_median=driven_median,
            baseline_median=baseline_median,
            fqcn=fqcn,
        )

    if not registered:
        return _fail(
            f'driver never emitted {DRIVER_REGISTERED_MARKER} over {games} solo games '
            '(the quad did not register on PlayerA)'
        )
    if is_proactive and not macro_fired:
        reach = (
            f' — the macro was REACHABLE ({DRIVER_MACRO_FIRED_MARKER} seen in search) but never '
            'committed on the real game'
            if macro_reachable
            else ''
        )
        return _fail(
            f'proactive quad never emitted {MACRO_FIRE_REAL_MARKER} over {games} solo games '
            f'(the deterministic win never REALLY executed in act(){reach})'
        )
    if not never_slower:
        floor = 'never-worse-solo floor' if not is_proactive else 'never-slower-than-CP7 check'
        return _fail(
            f'driven medianKillsOwn={driven_median} is WORSE than the CP7 baseline '
            f'{baseline_median} + tolerance {tolerance} ({floor}: a quad must never '
            'meaningfully slow the deck down)'
        )

    extra: dict[str, object] = {
        'gate_games': games,
        'gate_tolerance': tolerance,
        'gate_driven_median_kills_own': driven_median,
        'gate_baseline_median_kills_own': baseline_median,
        'gate_defended_lens': defended_lens,
        'gate_markers_seen': sorted(markers_seen),
    }
    drivers.write_meta(
        deck,
        drivers.DriverMeta(
            deck_version=version(deck),
            harness_version=drivers.harness_version(data_dir=data_dir),
            fqcn=fqcn,
            gates_passed=True,
            gate_mode=mode,
            extra=extra,
        ),
        data_dir=data_dir,
    )
    return GateResult(
        passed=True,
        mode=mode,
        reason='',
        registered=True,
        macro_fired=macro_fired,
        macro_reachable=macro_reachable,
        steer_fired=steer_fired,
        markers_seen=markers_seen,
        driven_median=driven_median,
        baseline_median=baseline_median,
        fqcn=fqcn,
        extra=extra,
    )
