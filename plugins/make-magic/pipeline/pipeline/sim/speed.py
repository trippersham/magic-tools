"""Speed router — closed-form default -> DRIVEN goldfish, gated by DRIVER RICHNESS.

The single entry point the CRISPI Speed axis consumes. It turns a deck into a
``FundamentalTurn`` (the turn CRISPI's :func:`~pipeline.transforms.crispi.speed_axis`
maps onto the Speed ladder). The closed-form estimate is the universal default; whether
Speed ESCALATES to a driven goldfish is decided by ONE thing — the deck's authored
driver richness:

  * **Closed form** — :func:`~pipeline.sim.speed_estimate.estimate_speed`, the pure
    own-turn kill estimate (no engine). The deterministic default AND the fallback.
  * **Driven goldfish** — a **DRIVEN** solo goldfish (``engine.goldfish(..., driver=...)``),
    run ONLY when the deck has a CURRENT, gate-passed, **DRIVE**-class driver
    (:func:`~pipeline.sim.drivers.driver_is_drive` True) AND a reachable install. The
    driver pilots PlayerA so the goldfish executes the deck's real win line.

Driver richness is the whole escalation gate. A thin driver reproduces baseline play, so a
goldfish driven by it adds nothing over the closed form; thin decks keep the closed form.

The hard invariant: XMage is never run for Speed without a runnable drive-class driver — never
driverless, never with a thin driver. Any deck without one falls back to the closed-form number,
with ``driver_recommended`` (and a lowered confidence) when a drive driver would have sharpened
it. This router only consumes a driver authored and gated elsewhere; it never authors a line.

Control decks (and any estimate with ``own_turn is None``) return Speed N/A
(``tier='na'``, ``turn=None``) — we never fabricate an own-turn kill for a deck that
has no honest clock.
"""

from __future__ import annotations

from dataclasses import dataclass

from pipeline.sim import drivers
from pipeline.sim.speed_estimate import DEFAULT_LETHAL, SpeedEstimate, estimate_speed

__all__ = ('DEFAULT_GOLDFISH_GAMES', 'FundamentalTurn', 'fundamental_turn')

#: Games for the driven goldfish. Speed reads the all-games median own-turn, so a modest sample
#: gives a stable number.
DEFAULT_GOLDFISH_GAMES = 20

#: One-step confidence de-rating for a closed-form fallback where a driven goldfish would have
#: helped but could not run (no drive driver / no install / no kill in the window).
_LOWER_CONFIDENCE = {'high': 'medium', 'medium': 'low', 'low': 'low', 'n/a': 'low'}

#: Floor on the game count handed to a re-gate. The behavioral gate flakes below its own floor,
#: so a router call with fewer games still re-gates at the honest minimum.
_MIN_REGATE_GAMES = 20


@dataclass(frozen=True)
class FundamentalTurn:
    """The Speed axis's resolved fundamental turn + its provenance.

    ``turn`` is the own-turn kill to feed the Speed ladder (``None`` for Speed N/A —
    control / no honest clock). ``tier`` records which model produced it
    (``'tier1'`` closed form, ``'tier2'`` driven goldfish, ``'na'`` no honest turn).
    ``driver_recommended`` is True when a DRIVE driver would sharpen this Speed but none
    could run it — a win-line deck with no driver, a DRIVE driver with no install, a
    stale/unstamped/broken driver, or a driven goldfish that found no kill. It is a
    surfaced signal to author/re-gate a driver, NOT a naive-XMage fallback. It is False
    for a THIN driver (closed form is the correct authored answer) and for a CP7-fine
    deck (no driver needed).
    """

    turn: float | None
    confidence: str
    tier: str
    source_rationale: str
    driver_recommended: bool = False


def _deck_ref(deck: object) -> tuple[str, str]:
    """Render ``deck`` to the ``(name, dck_text)`` pair the goldfish consumes.

    Mirrors ``sim/run.py``'s ``_resolve_deck_arg``: the Forge ``.dck`` exporter is the
    same text XMage's ``--solo`` stages for PlayerA. Imported lazily so the pure Tier-1
    path (and the tests that inject ``deck_ref``) never pull the exporter.
    """
    from pipeline.destinations.deck_export import get_exporter

    name = getattr(deck, 'name', '') or ''
    return (name, get_exporter('forge_dck').export(deck))


def fundamental_turn(
    deck: object,
    cards: list[dict],
    card_otag: dict[str, set[str]] | None = None,
    *,
    install: object | None = None,
    engine: object | None = None,
    deck_ref: tuple[str, str] | None = None,
    games: int = DEFAULT_GOLDFISH_GAMES,
    data_dir: str | None = None,
    archetype: str | None = None,
    combo_pieces: list[list[int]] | None = None,
    lethal: float = DEFAULT_LETHAL,
    estimate=estimate_speed,
    regate=None,
) -> FundamentalTurn:
    """Resolve ``deck``'s fundamental own-turn kill.

    Returns the closed-form estimate unless the deck has a current, gate-passed, drive-class
    driver and a reachable install, in which case it returns the driven goldfish's median
    own-turn instead. A stale driver is re-gated once first; a broken, absent, thin, or
    unknown-richness driver keeps the closed form (recommending a driver where one would help).
    A control estimate (``own_turn is None``) returns Speed N/A.

    Args:
        deck: A ``contracts.Deck`` (rendered to ``.dck`` only for the driven goldfish).
        cards: The deck's CRISPI card dicts (closed-form input).
        card_otag: ``oracle_id -> {slug}`` closure (reserved by the estimate).
        install: An XMage ``EngineInstall``, or ``None`` to disable the goldfish.
        engine: The engine exposing ``goldfish(...)`` (defaults to XMage; injectable for tests).
        deck_ref: Pre-rendered ``(name, dck_text)`` to skip the exporter (tests).
        games: Driven-goldfish game count.
        data_dir: Driver-registry data root override.
        archetype, combo_pieces, lethal: Passed through to the closed-form estimate.
        estimate: The closed-form estimate function (injectable for tests).
        regate: The stale-driver re-gate seam (injectable for tests).

    Returns:
        A :class:`FundamentalTurn`.
    """
    est: SpeedEstimate = estimate(
        cards, card_otag, archetype=archetype, combo_pieces=combo_pieces, lethal=lethal
    )

    # 1. Speed N/A — control or any estimate with no honest own-turn kill.
    if est.own_turn is None:
        return FundamentalTurn(
            turn=None, confidence=est.confidence, tier='na',
            source_rationale=est.rationale, driver_recommended=False,
        )

    tier1_turn = float(est.own_turn)
    # A detected in-deck combo makes a deck worth driving — the signal behind the
    # "author a driver" recommendation when none is present.
    deck_has_winline = bool(combo_pieces)

    def _closed_form(reason: str, *, lowered: bool, recommend: bool) -> FundamentalTurn:
        """The closed-form fallback — the deterministic default whenever a goldfish doesn't run."""
        conf = _LOWER_CONFIDENCE[est.confidence] if lowered else est.confidence
        return FundamentalTurn(
            turn=tier1_turn, confidence=conf, tier='tier1',
            source_rationale=reason, driver_recommended=recommend,
        )

    # 2. Classify the driver. A STALE driver (the deck or harness ABI moved under its stamp) is
    # re-gated once against the current harness before we consult its richness; every other state
    # is read as-is. We never re-gate a current driver during a scoring call — that is the
    # authoring/simulating flow's job.
    state = drivers.driver_state(deck, data_dir=data_dir)  # type: ignore[arg-type]

    if state == 'stale':
        if install is None:
            return _closed_form(
                'a per-deck driver is stale (deck/harness moved under its stamp) but no XMage '
                f'install is available to re-gate it. Using the closed-form estimate. ({est.rationale})',
                lowered=True, recommend=True,
            )
        # Recompile the authored source against the current harness + re-run the gate. On success
        # the driver is current again (with a fresh richness stamp) — re-read below. A gate or
        # environment (no javac/ECJ/JRE) failure falls back to the closed form, naming the cause.
        regate_fn = regate if regate is not None else _default_regate
        ref = deck_ref if deck_ref is not None else _deck_ref(deck)
        rg = regate_fn(
            deck, ref, install=install, games=max(games, _MIN_REGATE_GAMES),
            engine=engine, data_dir=data_dir,
        )
        if rg.ok:
            state = 'valid'  # re-stamped a current meta (incl. richness) — re-read below.
        elif rg.outcome == 'environment':
            return _closed_form(
                'a stale per-deck driver could not be re-gated in this environment: '
                f'{rg.reason}. Using the closed-form estimate. ({est.rationale})',
                lowered=True, recommend=True,
            )
        else:  # 'failed' → the recompile/gate rejected the driver.
            return _closed_form(
                'a stale per-deck driver failed re-gate against the current harness: '
                f'{rg.reason}. Using the closed-form estimate. ({est.rationale})',
                lowered=True, recommend=True,
            )

    if state == 'broken':
        # A driver present but rejected by its gate (gates_passed=false). Name the gate failure
        # in the fallback rationale so it is actionable.
        meta = drivers.read_meta(deck, data_dir=data_dir)  # type: ignore[arg-type]
        detail = ''
        if meta is not None:
            detail = str(meta.extra.get('regate_failure_reason', '')) or 'gate did not sign off (gates_passed=false)'
        return _closed_form(
            'the per-deck driver is BROKEN (its gate rejected it): '
            f'{detail}. Using the closed-form estimate. ({est.rationale})',
            lowered=True, recommend=True,
        )

    if state == 'absent':
        # No driver. The closed form is the honest a-priori Speed. Recommend authoring a DRIVE
        # driver ONLY when the deck has a real win-line to pilot; a CP7-fine deck needs none.
        if deck_has_winline:
            return _closed_form(
                'a win-line is detected but no per-deck driver exists — a DRIVE driver would let a '
                f'goldfish measure the real execution clock. Using the closed-form estimate. ({est.rationale})',
                lowered=True, recommend=True,
            )
        return _closed_form(
            f'no per-deck driver; the closed-form own-turn is the Speed. ({est.rationale})',
            lowered=False, recommend=False,
        )

    # 3. state == 'valid' — driver richness decides: DRIVE runs the goldfish, THIN keeps the
    # closed form, unknown keeps the closed form and recommends re-authoring.
    is_drive = drivers.driver_is_drive(deck, data_dir=data_dir)  # re-read (post-regate if it ran).
    if is_drive is False:
        # A thin driver reproduces baseline play, so a goldfish adds nothing over the closed form.
        return _closed_form(
            'the per-deck driver is thin (baseline play) — the closed-form own-turn is '
            f'authoritative. ({est.rationale})',
            lowered=False, recommend=False,
        )
    if is_drive is None:
        # Richness could not be determined (a legacy driver whose class was never resolved). Keep
        # the closed form and recommend re-authoring to restore a driven Speed — never guess a tier.
        return _closed_form(
            'the per-deck driver richness is unknown; re-author it to restore a driven Speed. '
            f'Using the closed-form estimate. ({est.rationale})',
            lowered=True, recommend=True,
        )

    # is_drive is True — a DRIVE driver.
    if install is None:
        return _closed_form(
            'a DRIVE per-deck driver is present but no XMage install is available to run the '
            f'driven goldfish. Using the closed-form estimate. ({est.rationale})',
            lowered=True, recommend=True,
        )

    eng = engine if engine is not None else _default_engine()
    classes = str(drivers.classes_dir(deck, data_dir=data_dir))  # type: ignore[arg-type]
    meta = drivers.read_meta(deck, data_dir=data_dir)  # type: ignore[arg-type]
    if meta is None:
        # Racy delete between the state check and here — fall back honestly.
        return _closed_form(
            f'the per-deck driver meta could not be read; using the closed-form estimate. ({est.rationale})',
            lowered=True, recommend=True,
        )
    ref = deck_ref if deck_ref is not None else _deck_ref(deck)
    # Commander decks run the commander-native solo (40 life + command zone) so the commander is
    # seated — otherwise a commander-dependent line can never execute.
    fmt = 'commander' if getattr(deck, 'commanders', None) else 'constructed'
    result = eng.goldfish(
        ref, games=games, install=install, driver=(classes, meta.fqcn), fmt=fmt
    )
    # Use the all-games median (the deckcheck Speed rubric, issue #30): the turn a kill completes
    # in ≥50% of games, bricks counted as slow. A kills-only median flatters a line that kills
    # fast in <50% of games. A median past the brick cap means most games never killed — no honest
    # clock — so fall back.
    median_all = getattr(result, 'median_all_own', None)
    max_turn = getattr(result, 'max_turn', None)
    if median_all is not None:
        # Past the brick cap ⇒ most games never killed ⇒ the -1.0 no-kill sentinel below.
        median = float(median_all) if median_all >= 0 and (max_turn is None or median_all <= max_turn) else -1.0
    else:
        median = float(result.median_kills_own)  # engines that don't report an all-games median.
    if median >= 0:
        return FundamentalTurn(
            turn=median, confidence='high', tier='tier2',
            source_rationale=(
                f'driven goldfish ({meta.fqcn}, {games} games): '
                f'median own-turn kill {median:g}. (closed form: {est.rationale})'
            ),
            driver_recommended=False,
        )
    # -1.0 sentinel: the driver fired but no kill landed in the window. Do NOT return -1 as a turn.
    return _closed_form(
        'the driven goldfish found no ≥50%-of-games kill in the window (no kill at all, or the '
        f'majority of games bricked); using the closed-form estimate. ({est.rationale})',
        lowered=True, recommend=True,
    )


def _default_engine() -> object:
    """The real XMage engine (lazy — the pure/fallback paths never import it)."""
    from pipeline.sim.engine import get_engine

    return get_engine('xmage')


def _default_regate(deck, deck_ref, **kwargs):  # type: ignore[no-untyped-def]
    """The real re-gate seam (lazy — the pure/fallback paths never import the gate/ECJ stack)."""
    from pipeline.sim.driver_gate import regate_driver

    return regate_driver(deck, deck_ref, **kwargs)
