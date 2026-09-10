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
from pipeline.sim import driver_compile, driver_lint, drivers
from pipeline.sim.driver_authoring import (
    DRIVER_MACRO_FIRED_MARKER,
    DRIVER_MULLIGAN_MARKER,
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
    'MIN_GATE_GAMES',
    'MULLIGAN_FIRED_MARKER',
    'GateResult',
    'RegateResult',
    'compile_quad_driver',
    'gate_driver',
    'recompile_authored_driver',
    'regate_driver',
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

#: The mulligan slot's real-fire marker (the restored 5th dimension): the DIST itself (patch
#: ``0005`` ``ComputerPlayer.chooseMulligan``) prints ``DRIVER_MULLIGAN name=<seat> ship=<bool>``
#: on stderr the instant a REGISTERED seat makes its real pre-game keep/ship decision. An
#: unregistered seat is byte-identical to prior CP7 and never emits it, so its presence proves the
#: mulligan hook fired. Recorded in :attr:`GateResult.mulligan_fired` as an exercised-slot signal —
#: NOT a proactive PASS requirement on its own (a deck may own mulligan alongside a macro, or
#: mulligan-only), so a mulligan-owning quad's DRIVER_MULLIGAN firing is observed, not gated on.
MULLIGAN_FIRED_MARKER = DRIVER_MULLIGAN_MARKER

#: Hard floor on the gate's per-run game count. The never-slower check compares two seedless,
#: independently-jittery medians; below this count the check flakes and a silently-underpowered
#: gate is worse than a loud stop. Both modes are jitter-sensitive.
#:
#: WHY 20: the Phase 6.1 Jeleva canary showed the never-slower NUMERIC check false-FAILS at n=12
#: and n=15 for a deck whose driven own-turn clock coincides with the CP7 baseline mode (driven
#: ~12-14 vs a throwaway baseline jittering 11/13/14 → Δ crosses the ±2 tolerance on baseline
#: draw luck alone). The categorical signals (DRIVER_REGISTERED, MACRO_FIRE_REAL, DRIVER_MULLIGAN)
#: are robust at any n; only the point-median never-slower term coin-flips near driven≈baseline.
#: Re-gating the authored Jeleva quad at n=20 passed cleanly 3x in a row (driven/baseline =
#: 10/13, 13/13, 11/12) where n=12 and n=15 had each false-failed — so 20 is the empirically-
#: pinned floor at which the numeric term stops flaking for a driven≈baseline deck.
_MIN_GATE_GAMES = 20
#: Public alias of the gate's per-run game-count floor, so callers (the re-gate flow, the speed
#: router) can respect it without reaching for the private name.
MIN_GATE_GAMES = _MIN_GATE_GAMES

#: The richness stamp for a driver whose DRIVE-vs-THIN class could not be determined (a legacy
#: 'unknown'-mode re-gate with no observed macro fire). Mirrors ``drivers._LEGACY_DRIVER_CLASS``:
#: ``driver_is_drive`` reads it back as ``None``, so the router keeps the closed form.
_UNKNOWN_DRIVER_CLASS = 'unknown'

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
    ``mulligan_fired`` = the dist's ``chooseMulligan`` hook (patch 0005) consulted a registered
    mulligan steer on the real opening hand (the ``DRIVER_MULLIGAN`` marker) — an exercised-slot
    signal recorded for both modes, never a PASS requirement on its own.
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
    mulligan_fired: bool = False
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
        fmt: str = ...,
    ) -> tuple[GoldfishResult, str]: ...

    def goldfish(
        self,
        deck_a: tuple[str, str],
        *,
        games: int,
        install: EngineInstall,
        driver: tuple[str, str] | None = ...,
        fmt: str = ...,
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
    from pathlib import Path

    source = render_quad_driver(deck, spec)
    check_quad_guardrails(source)  # AC8: raises on a do-not-own violation before we compile.

    ddir = drivers.driver_dir(deck, data_dir=data_dir)
    ddir.mkdir(parents=True, exist_ok=True)
    src_path = ddir / 'Driver.java'
    Path(src_path).write_text(source, encoding='utf-8')

    fqcn = driver_fqcn(deck)
    cache_dir, _ = driver_compile.compile_for_injection(str(src_path), fqcn, data_dir=data_dir)
    return _publish_classes(deck, cache_dir, data_dir=data_dir), fqcn


def _publish_classes(
    deck: Deck,
    cache_dir: str,
    *,
    data_dir: str | os.PathLike[str] | None = None,
) -> str:
    """Publish an ECJ ``(dist SHA, source hash)`` cache tree into ``deck``'s per-deck classes dir.

    The single seam shared by the first compile (:func:`compile_quad_driver`) and the re-compile
    (:func:`recompile_authored_driver`): copy the compiled class tree into the ``classes_dir``
    keyed by ``deck.uuid`` that BOTH the gate and the production run path inject, wiping any stale
    ``.class`` first. Returns the published dir as a str."""
    import shutil

    classes = drivers.classes_dir(deck, data_dir=data_dir)
    shutil.rmtree(classes, ignore_errors=True)  # no stale .class survives a re-compile.
    shutil.copytree(cache_dir, classes)
    return str(classes)


def recompile_authored_driver(
    deck: Deck,
    fqcn: str,
    *,
    data_dir: str | os.PathLike[str] | None = None,
) -> tuple[str, str]:
    """Re-compile ``deck``'s ALREADY-AUTHORED ``Driver.java`` against the CURRENT dist jar.

    The re-gate counterpart of :func:`compile_quad_driver`: instead of re-rendering from a
    :class:`QuadSpec`, it takes the source already on disk in the deck's driver dir (written by
    the original :func:`compile_quad_driver`) and runs it back through the SAME ECJ path
    (:func:`~pipeline.sim.driver_compile.compile_for_injection`) against whatever dist jar is now
    effective — so a harness/jar change that invalidated the old bytecode produces fresh, current
    bytecode. The compiled tree is published into the per-deck ``classes_dir`` via
    :func:`_publish_classes`. Returns the ``(classes_dir, fqcn)`` injection tuple.

    A missing authored source raises ``FileNotFoundError``; a compile failure raises
    :class:`~pipeline.sim.driver_compile.DriverCompileError` (parsed diagnostics); a toolchain
    failure (no ECJ/JRE) raises :class:`~pipeline.sim.driver_compile.DriverCompileToolError`.
    """
    from pathlib import Path

    src_path = drivers.driver_dir(deck, data_dir=data_dir) / 'Driver.java'
    if not Path(src_path).is_file():
        raise FileNotFoundError(
            f'no authored Driver.java for deck {deck.uuid} at {src_path} — cannot re-compile a '
            'driver whose source was never persisted (author + compile it first)'
        )
    cache_dir, _ = driver_compile.compile_for_injection(str(src_path), fqcn, data_dir=data_dir)
    return _publish_classes(deck, cache_dir, data_dir=data_dir), fqcn


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
    spec: QuadSpec | None = None,
    mode: str | None = None,
    install: EngineInstall,
    games: int,
    tolerance: float = _DEFAULT_TOLERANCE,
    engine: _GoldfishEngine | None = None,
    data_dir: str | os.PathLike[str] | None = None,
    defended_lens: bool = False,
) -> GateResult:
    """Run the dual-mode behavioral gate on ``deck``'s compiled quad; stamp on pass.

    This is PURE MEASUREMENT — three signals, none of them a bracket/archetype/absolute-turn
    expectation:

      1. **capability** — does the quad do what its SHAPE claims? A quad WITH a macro must show
         ``MACRO_FIRE_REAL`` >=1x (the macro REALLY executed to win in the real ``act()`` — NOT
         merely reachable in search); a Φ-only quad requires no fire (a passive solo goldfish
         gives it nothing to react to). Both modes require the ``DRIVER_REGISTERED`` marker.
      2. **relative** — never-slower vs the SAME deck's bare-CP7 baseline
         (``medianKillsOwn`` <= baseline + :data:`_DEFAULT_TOLERANCE`). The bar is the deck's own
         driverless clock, run-matched; there is NO absolute turn number keyed to a bracket or
         archetype. A ``-1.0`` no-kill sentinel maps to ``+inf`` (:func:`_kill_metric`) so a
         non-killing driver always fails.
      3. **brick-cap validity** — the counted "kill" must be the DECK's own kill, not the passer
         opponent decking out / losing on a technicality at the turn cap (the Jeleva-freeze
         pathology). The harness clamps any such loss to ``maxTurn``, so a driven median AT the
         cap is a clamp artifact and FAILS. This reads the harness's own sentinel boundary, not
         an archetype clock.

    ``mode`` (``'proactive'``/``'reactive'``) is a CAPABILITY label derived purely from
    ``spec.macro is not None`` (macro-present vs Φ-only) — it is NOT a posture, bracket, or
    archetype tag; the strings are kept only for continuity of the meta stamp.

    ``deck_ref`` is the ``(name, forge_dck_text)`` tuple the solo ``goldfish`` path consumes
    (kept explicit so the gate does not couple to the deck exporter / network). Runs the DRIVEN
    solo (markers + median) then a throwaway DRIVERLESS CP7 baseline (median) over ``games``
    games each. ``defended_lens=True`` is an opt-in note, never a blocker.

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

    # CAPABILITY mode: normally derived from the quad SHAPE (a macro ⇒ proactive). The re-gate
    # flow has no live spec (it recompiles source already on disk) and instead passes the mode
    # recovered from the existing stamp; a legacy ``'unknown'`` stamp maps to the LENIENT reactive
    # gate (no macro-fire requirement) — we cannot prove a macro we never rendered, and demanding
    # a fire we can't justify would wrongly reject a still-good driver.
    # ``richness_known`` records whether we can decide DRIVE-vs-THIN from the input alone: an
    # authored spec tells us directly (a macro ⇒ drive, none ⇒ thin), and a re-gate carrying a
    # real 'proactive'/'reactive' stamp is authoritative too. A legacy 'unknown' stamp is NOT —
    # only an OBSERVED macro fire can prove drive; absent that, richness stays undetermined.
    if spec is not None:
        is_proactive = spec.macro is not None
        mode = 'proactive' if is_proactive else 'reactive'
        richness_known = True
    elif mode is not None:
        richness_known = mode in ('proactive', 'reactive')
        is_proactive = mode == 'proactive'
        mode = 'proactive' if is_proactive else 'reactive'
    else:
        raise ValueError('gate_driver requires exactly one of spec= or mode=')

    # LAYER 1 — forbidden-API bytecode scan (no-terminal-API rule). A driver may only enqueue
    # LEGAL game actions; every terminal state must come from the rules engine. Scan the
    # compiled .class constant pools for terminal / state-fabrication references BEFORE running
    # a single JVM game — a driver that calls Player.lost/won/... or Game.setWinner/end is
    # asserting a win it never played (the MACRO_FIRE_REAL→gameOver on turn 1 pathology). Terminal
    # APIs, concede, AND zone-fabrication (moveCard* on a mage/ owner) all FAIL hard — a driver acts
    # only through casts/activations/choices; the engine owns every terminal state and zone change.
    # (warn_findings is retained for API stability but no rule emits WARN any more.)
    lint = driver_lint.lint_driver_classes(classes)
    lint_warnings = [f.detail for f in lint.warn_findings]
    if not lint.ok:
        return GateResult(
            passed=False,
            mode=mode,
            reason=(
                f'driver bytecode references forbidden terminal-state API(s) — {lint.summary}. '
                'A driver may only enqueue LEGAL game actions; every terminal state must come '
                'from the rules engine (re-author the macro to play the line, not assert the win)'
            ),
            fqcn=fqcn,
            extra={'gate_lint_warnings': lint_warnings, 'gate_lint_fail': lint.summary},
        )

    # Commander decks run the commander-native solo goldfish (40 life + command zone) so the
    # commander is seated — a commander-dependent macro can never fire in the constructed goldfish.
    # The driven and baseline runs use the same fmt so the never-slower comparison is honest.
    fmt = 'commander' if deck.commanders else 'constructed'

    driven, output = eng.goldfish_output(deck_ref, games=games, install=install, driver=driver, fmt=fmt)
    baseline = eng.goldfish(deck_ref, games=games, install=install, driver=None, fmt=fmt)

    registered = DRIVER_REGISTERED_MARKER in output
    # macro_fired = REAL execution (act() commit); macro_reachable = apply() entered (search
    # copy OR real). Only the former proves the deterministic win actually ran to win.
    macro_fired = MACRO_FIRE_REAL_MARKER in output
    macro_reachable = DRIVER_MACRO_FIRED_MARKER in output
    steer_fired = DRIVER_STEER_FIRED_MARKER in output
    # mulligan_fired = the dist's chooseMulligan hook (patch 0005) consulted a registered
    # mulligan steer on the real opening hand. An exercised-slot signal (recorded), never a
    # PASS requirement on its own — a deck may own mulligan alongside a macro, or mulligan-only.
    mulligan_fired = MULLIGAN_FIRED_MARKER in output
    markers_seen = frozenset(
        m
        for m, seen in (
            (DRIVER_REGISTERED_MARKER, registered),
            (MACRO_FIRE_REAL_MARKER, macro_fired),
            (DRIVER_MACRO_FIRED_MARKER, macro_reachable),
            (DRIVER_STEER_FIRED_MARKER, steer_fired),
            (MULLIGAN_FIRED_MARKER, mulligan_fired),
        )
        if seen
    )

    driven_median = driven.median_kills_own
    baseline_median = baseline.median_kills_own
    never_slower = _kill_metric(driven_median) <= _kill_metric(baseline_median) + tolerance

    # BRICK-CAP VALIDITY (the Jeleva-freeze pathology). The solo harness counts a "kill"
    # whenever the passer opponent LOSES for ANY reason — a life-total kill OR the passer
    # decking out / losing on a state-based technicality — and CLAMPS that kill's own-turn
    # to maxTurn (`killTurn = min(ownEndTurn, maxTurn)` in XMageBatch.runSolo). So a driven
    # median sitting AT the cap is NOT the deck executing its own kill; it is the do-nothing
    # opponent falling over at the turn cap — a brick masquerading as a pass. Reject it. This
    # reads the harness's OWN sentinel boundary (maxTurn, straight off the summary line), NOT a
    # bracket/archetype/absolute-turn expectation: it asks "is this counted kill real or a
    # clamp artifact", never "did the deck meet an archetype clock". max_turn is None only for
    # pre-maxTurn harness output, where the guard is a documented no-op (cannot validate).
    driven_max_turn = driven.max_turn
    brick_capped = (
        driven_max_turn is not None
        and driven_median >= 0  # a real (non-sentinel) counted-kill median…
        and driven_median >= driven_max_turn  # …that sits at/beyond the brick cap = clamp artifact
    )

    def _fail(reason: str) -> GateResult:
        return GateResult(
            passed=False,
            mode=mode,
            reason=reason,
            registered=registered,
            macro_fired=macro_fired,
            macro_reachable=macro_reachable,
            steer_fired=steer_fired,
            mulligan_fired=mulligan_fired,
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
    if is_proactive and driven_median < 0:
        # LETHALITY INVARIANT (the cheating-driver fix). The macro COMMITTED (MACRO_FIRE_REAL) but
        # the driven goldfish never reduced the opponent to a real lethal — driven medianKillsOwn is
        # the -1 no-kill sentinel (the harness now counts a kill only on lifeB<=0, not a bare
        # engine hasLost()). A macro that "wins" with the opponent alive is a non-lethal engine
        # artifact, not the deck executing its line, so it must NOT pass — even when the baseline
        # also never kills (where the never-slower check below would pass +inf <= +inf + tolerance).
        return _fail(
            f'proactive quad fired {MACRO_FIRE_REAL_MARKER} but the driven goldfish never reduced '
            f'the opponent to LETHAL over {games} solo games (medianKillsOwn={driven_median}, the '
            'no-kill sentinel) — a non-lethal engine artifact, not a real kill (the deck must play '
            'the win, not have the engine adjudicate a win with the opponent alive)'
        )
    if not never_slower:
        floor = 'never-worse-solo floor' if not is_proactive else 'never-slower-than-CP7 check'
        return _fail(
            f'driven medianKillsOwn={driven_median} is WORSE than the CP7 baseline '
            f'{baseline_median} + tolerance {tolerance} ({floor}: a quad must never '
            'meaningfully slow the deck down)'
        )
    if brick_capped:
        return _fail(
            f'driven medianKillsOwn={driven_median} sits AT the brick cap '
            f'maxTurn={driven_max_turn} (brick-cap validity): the counted "kills" are '
            'clamp artifacts — the do-nothing passer opponent decked out / lost on a '
            'state-based technicality at the turn cap, NOT the deck executing its own '
            'kill. An opponent-deckout-at-cap "win" is a brick, not a pass'
        )

    extra: dict[str, object] = {
        'gate_games': games,
        'gate_tolerance': tolerance,
        'gate_driven_median_kills_own': driven_median,
        'gate_baseline_median_kills_own': baseline_median,
        'gate_defended_lens': defended_lens,
        'gate_mulligan_fired': mulligan_fired,
        'gate_markers_seen': sorted(markers_seen),
        'gate_lint_warnings': lint_warnings,
    }
    # DRIVE/THIN richness the Speed router reads: 'drive' runs a driven goldfish, 'thin' keeps the
    # closed form. A driver is 'drive' if it is proactive or its macro actually fired this run;
    # 'thin' only when richness is known (an authored spec, or a real 'proactive'/'reactive' stamp)
    # and no drive evidence appeared. When richness is unknown (a legacy 'unknown'-mode re-gate) and
    # no macro fired, stamp 'unknown' rather than guess 'thin' — the router then keeps the closed
    # form and recommends re-authoring instead of silently ruling out a driven goldfish.
    if is_proactive or macro_fired:
        driver_class = 'drive'
    elif richness_known:
        driver_class = 'thin'
    else:
        driver_class = _UNKNOWN_DRIVER_CLASS
    drivers.write_meta(
        deck,
        drivers.DriverMeta(
            deck_version=version(deck),
            harness_version=drivers.harness_version(data_dir=data_dir),
            fqcn=fqcn,
            gates_passed=True,
            gate_mode=mode,
            driver_class=driver_class,
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
        mulligan_fired=mulligan_fired,
        markers_seen=markers_seen,
        driven_median=driven_median,
        baseline_median=baseline_median,
        fqcn=fqcn,
        extra=extra,
    )


@dataclass(frozen=True)
class RegateResult:
    """The outcome of :func:`regate_driver` — a stale driver's recompile + re-gate attempt.

    ``ok`` True means the driver is now :func:`~pipeline.sim.drivers.driver_valid` again (the gate
    re-stamped a current ``meta.json``). ``ok`` False splits by ``outcome``:

      * ``'failed'`` — the recompile or the behavioral gate REJECTED the driver; the stamp was
        rewritten ``gates_passed=false`` (state → ``'broken'``) and the authored source was KEPT.
      * ``'environment'`` — the toolchain could not run (no ECJ / no JRE / no jar): the driver is
        NOT condemned (still ``'stale'``), the caller must fall back loudly naming the cause.
      * ``'absent'`` — there was no stamp/authored source to re-gate in the first place.

    ``reason`` names the failure (empty on ``ok``); ``gate`` carries the underlying
    :class:`GateResult` when the behavioral gate actually ran.
    """

    ok: bool
    outcome: str  #: 'regated' | 'failed' | 'environment' | 'absent'
    reason: str = ''
    gate: GateResult | None = None


#: Substrings that mark a compile "failure" as a MISSING toolchain (JRE/javac/ECJ) rather than a
#: genuine ECJ rejection of the driver source — the JVM/OS launcher noise emitted when no runtime is
#: reachable. Matched case-insensitively against a diagnostic-less DriverCompileError's stderr.
_TOOLCHAIN_ABSENT_MARKERS = (
    'unable to locate a java runtime',
    'no java runtime present',
    'unable to find any jvms',
    'java: command not found',
    'no such file or directory: java',
    'the operation could',  # macOS "The operation couldn't be completed" launcher failure.
)


def _is_toolchain_absence(exc: object) -> bool:
    """True if a :class:`DriverCompileError` is really a MISSING toolchain, not a source rejection.

    A genuine ECJ rejection parses at least one structured diagnostic. A missing JRE/javac instead
    yields a nonzero exit with ZERO parsed diagnostics and a launcher message on stderr (e.g. macOS's
    "Unable to locate a Java Runtime"). We treat only the diagnostic-less + known-marker case as an
    environment problem, so a real compile error is never silently excused.
    """
    result = getattr(exc, 'result', None)
    if result is None or getattr(result, 'diagnostics', None):
        return False
    stderr = (getattr(result, 'raw_stderr', '') or '').lower()
    return any(marker in stderr for marker in _TOOLCHAIN_ABSENT_MARKERS)


def _mark_broken(
    deck: Deck,
    meta: object,
    reason: str,
    *,
    data_dir: str | os.PathLike[str] | None,
) -> None:
    """Rewrite ``deck``'s stamp ``gates_passed=false`` (state → ``'broken'``), keeping the source.

    A re-gate that FAILS must not leave the old ``gates_passed=true`` stamp — a stale driver whose
    recompile/gate rejected it would otherwise keep reading as merely ``'stale'`` and be retried
    forever. We refresh the version stamps to CURRENT (so it is unambiguously "current harness,
    gate said no" — broken, not stale) and record the failure reason in ``extra``. The authored
    ``Driver.java`` is deliberately left on disk for inspection / a future re-author.
    """
    prior = getattr(meta, 'extra', {})
    extra = {**(prior if isinstance(prior, dict) else {}), 'regate_failure_reason': reason}
    drivers.write_meta(
        deck,
        drivers.DriverMeta(
            deck_version=version(deck),
            harness_version=drivers.harness_version(data_dir=data_dir),
            fqcn=getattr(meta, 'fqcn', '') or driver_fqcn(deck),
            gates_passed=False,
            gate_mode=getattr(meta, 'gate_mode', 'unknown'),
            extra=extra,
        ),
        data_dir=data_dir,
    )


def regate_driver(
    deck: Deck,
    deck_ref: tuple[str, str],
    *,
    install: EngineInstall,
    games: int,
    tolerance: float = _DEFAULT_TOLERANCE,
    engine: _GoldfishEngine | None = None,
    data_dir: str | os.PathLike[str] | None = None,
    defended_lens: bool = False,
    compile_fn: object | None = None,
    gate_fn: object | None = None,
) -> RegateResult:
    """Re-compile ``deck``'s authored driver against the CURRENT harness and re-run the gate.

    The stale→valid recovery after a harness bump: a driver whose bytecode was compiled against an
    older dist reads as ``'stale'`` (:func:`~pipeline.sim.drivers.driver_state`). This reuses the
    original compile path (:func:`recompile_authored_driver` →
    :func:`~pipeline.sim.driver_compile.compile_for_injection`, the same ECJ invocation) to
    produce fresh bytecode, then re-runs the behavioral :func:`gate_driver` (mode recovered from
    the existing stamp — no live :class:`QuadSpec` needed). On PASS the gate re-stamps a current
    ``meta.json`` (``ok=True``, ``driver_valid`` True again). On a compile/gate FAILURE the stamp
    is rewritten ``gates_passed=false`` (state → ``'broken'``) and the authored source is KEPT. A
    toolchain failure (no ECJ/JRE/jar) returns ``outcome='environment'`` WITHOUT condemning the
    driver — the caller falls back loudly.

    ``compile_fn`` / ``gate_fn`` are the recompile + behavioral-gate seams, injectable so unit
    tests exercise the state machine with NO real JVM/ECJ.
    """
    from pipeline.sim.driver_compile import DriverCompileError, DriverCompileToolError

    recompile = compile_fn if compile_fn is not None else recompile_authored_driver
    gate = gate_fn if gate_fn is not None else gate_driver

    meta = drivers.read_meta(deck, data_dir=data_dir)
    if meta is None:
        return RegateResult(
            ok=False,
            outcome='absent',
            reason='no parseable meta.json to re-gate (author + compile + gate the driver first)',
        )

    fqcn = meta.fqcn or driver_fqcn(deck)
    try:
        recompile(deck, fqcn, data_dir=data_dir)  # type: ignore[operator]
    except DriverCompileError as exc:
        if _is_toolchain_absence(exc):
            # The "compile failure" is actually a MISSING toolchain (no JRE/javac/ECJ) surfacing as
            # a nonzero ECJ exit with no parseable diagnostics — an ENVIRONMENT problem, not a broken
            # driver. Do NOT condemn it; it may re-gate cleanly on a machine that can compile.
            return RegateResult(
                ok=False,
                outcome='environment',
                reason=f'the compile toolchain could not run (no javac/ECJ/JRE): {exc}',
            )
        reason = f're-compile against the current dist FAILED: {exc}'
        _mark_broken(deck, meta, reason, data_dir=data_dir)
        return RegateResult(ok=False, outcome='failed', reason=reason)
    except (DriverCompileToolError, FileNotFoundError, OSError) as exc:
        # Toolchain/environment could not run (no ECJ, no JRE, no jar, missing source). Do NOT
        # condemn the driver — it may re-gate cleanly on a machine that can compile.
        return RegateResult(
            ok=False,
            outcome='environment',
            reason=f'the compile toolchain could not run (no javac/ECJ/JRE or missing source): {exc}',
        )

    result: GateResult = gate(  # type: ignore[operator]
        deck,
        deck_ref,
        mode=meta.gate_mode,
        install=install,
        games=games,
        tolerance=tolerance,
        engine=engine,
        data_dir=data_dir,
        defended_lens=defended_lens,
    )
    if result.passed:
        return RegateResult(ok=True, outcome='regated', gate=result)
    _mark_broken(deck, meta, result.reason, data_dir=data_dir)
    return RegateResult(ok=False, outcome='failed', reason=result.reason, gate=result)
