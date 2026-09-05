"""Speed router — Tier-1 closed form -> Tier-2 DRIVEN goldfish -> fundamental turn.

The single entry point the CRISPI Speed axis consumes. It turns a deck into a
``FundamentalTurn`` (the turn CRISPI's :func:`~pipeline.transforms.crispi.speed_axis`
maps onto the Speed ladder) by routing through two tiers:

  * **Tier-1** — :func:`~pipeline.sim.speed_estimate.estimate_speed`, the pure
    closed-form own-turn kill estimate (no engine). Most decks resolve here.
  * **Tier-2** — a **DRIVEN** solo goldfish (``engine.goldfish(..., driver=...)``),
    run ONLY when Tier-1 flags ``needs_tier2`` AND the deck has a CURRENT, gate-passed
    compiled driver (:func:`~pipeline.sim.drivers.driver_valid`). The driver pilots
    PlayerA so the goldfish actually executes the deck's line.

**AC3 — the hard invariant.** XMage is NEVER run naively (driverless) for Speed. A
deck that needs Tier-2 but has no ``driver_valid`` driver (or no reachable XMage
install) does NOT get a naive goldfish — it falls back to the Tier-1 number with
``tier2_recommended=True`` and a lowered confidence. Auto-authoring an arbitrary
deck's driver LINE is out of scope (it needs the per-deck line spec — the skill /
caller's job); this router only CONSUMES a driver authored + gated upstream.

Control decks (and any Tier-1 estimate with ``own_turn is None``) return Speed N/A
(``tier='na'``, ``turn=None``) — we never fabricate an own-turn kill for a deck that
has no honest clock.
"""

from __future__ import annotations

from dataclasses import dataclass

from pipeline.sim import drivers
from pipeline.sim.speed_estimate import DEFAULT_LETHAL, SpeedEstimate, estimate_speed

__all__ = ('DEFAULT_GOLDFISH_GAMES', 'FundamentalTurn', 'fundamental_turn')

#: Games for the Tier-2 driven goldfish. The median own-turn kill is read across the
#: games that actually killed, so a modest sample gives a stable median.
DEFAULT_GOLDFISH_GAMES = 20

#: One-step confidence de-rating for a Tier-1 fallback where Tier-2 was warranted but
#: unavailable (no driver / no install) or the driven goldfish found no kill.
_LOWER_CONFIDENCE = {'high': 'medium', 'medium': 'low', 'low': 'low', 'n/a': 'low'}

#: Floor on the game count handed to an auto-re-gate. The behavioral gate flakes below its own
#: floor; a router call with fewer games must still re-gate at the honest minimum rather than
#: trip the gate's hard ``ValueError``.
_MIN_REGATE_GAMES = 20


@dataclass(frozen=True)
class FundamentalTurn:
    """The Speed axis's resolved fundamental turn + its provenance.

    ``turn`` is the own-turn kill to feed the Speed ladder (``None`` for Speed N/A —
    control / no honest clock). ``tier`` records which model produced it
    (``'tier1'`` closed form, ``'tier2'`` driven goldfish, ``'na'`` no honest turn).
    ``tier2_recommended`` is True when the deck WANTED a Tier-2 driven goldfish but
    could not get one (no ``driver_valid`` driver, or no XMage install, or the driven
    goldfish found no kill) — a surfaced signal, NOT a naive-XMage fallback.
    """

    turn: float | None
    confidence: str
    tier: str
    source_rationale: str
    tier2_recommended: bool = False


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
    """Resolve ``deck``'s fundamental own-turn kill (Tier-1 -> Tier-2-driven -> turn).

    Decision tree (AC3-safe — no naive XMage anywhere on this path):

      1. Run Tier-1 ``estimate(cards, card_otag, ...)``.
      2. ``own_turn is None`` (control / no honest clock) -> ``tier='na'``, ``turn=None``.
         Never fabricate a turn.
      3. NOT ``needs_tier2`` -> the Tier-1 ``own_turn`` (``tier='tier1'``).
      4. ``needs_tier2``:
           a. ``driver_valid(deck)`` AND ``install`` present -> run the DRIVEN solo
              goldfish; return its ``median_kills_own`` (``tier='tier2'``). The
              ``-1.0`` no-kill sentinel is mapped honestly: fall back to the Tier-1
              number + ``tier2_recommended`` (never return ``-1`` as a turn).
           b. else (no valid driver / no install) -> the Tier-1 number with
              ``tier='tier1'``, ``tier2_recommended=True``, lowered confidence.
              **Never** a naive XMage goldfish.

    Args:
        deck: A ``contracts.Deck`` (or any object with ``.name`` + a ``.uuid`` the
            driver registry keys on). Rendered to ``.dck`` only on the driven branch.
        cards: The deck's CRISPI card dicts (Tier-1 input).
        card_otag: ``oracle_id -> {slug}`` closure (reserved by Tier-1).
        install: An XMage ``EngineInstall`` (read-only). ``None`` disables Tier-2 —
            the router then falls back with ``tier2_recommended`` (AC3).
        engine: The engine exposing ``goldfish(...)`` (defaults to the XMage engine).
            Injectable for tests (a fake goldfish, no JVM).
        deck_ref: Pre-rendered ``(name, dck_text)`` — skips the exporter (tests).
        games: Driven-goldfish game count.
        data_dir: Driver-registry data root override.
        archetype / combo_pieces / lethal: Passed through to the Tier-1 estimator.
        estimate: The Tier-1 function (injectable for tests).
        regate: The stale-driver re-gate seam (defaults to
            :func:`~pipeline.sim.driver_gate.regate_driver`); injectable so tests drive the
            stale→re-gate→Tier-2 and stale→fail→loud-Tier-1 flows with no real JVM/ECJ.

    Returns:
        A :class:`FundamentalTurn`.
    """
    est: SpeedEstimate = estimate(
        cards, card_otag, archetype=archetype, combo_pieces=combo_pieces, lethal=lethal
    )

    # 2. Speed N/A — control or any estimate with no honest own-turn kill.
    if est.own_turn is None:
        return FundamentalTurn(
            turn=None, confidence=est.confidence, tier='na',
            source_rationale=est.rationale, tier2_recommended=False,
        )

    # 3. Tier-1 suffices.
    if not est.needs_tier2:
        return FundamentalTurn(
            turn=float(est.own_turn), confidence=est.confidence, tier='tier1',
            source_rationale=est.rationale, tier2_recommended=False,
        )

    # 4. Tier-2 warranted. Classify the driver into valid / stale / broken / absent so a STALE
    # driver (the jar-cut case: authored + gated, but the harness ABI moved under its stamp) gets
    # AUTO-RE-GATED once against the current harness before we give up on Tier-2.
    state = drivers.driver_state(deck, data_dir=data_dir)  # type: ignore[arg-type]

    if state == 'stale' and install is not None:
        # STALE → attempt a bounded, ONCE-per-call re-gate: recompile the authored driver against
        # the current harness and re-run the gate. On success Tier-2 resumes; on a compile/gate
        # failure or an environment (no javac/ECJ/JRE) failure we fall back LOUDLY, naming the cause.
        regate_fn = regate if regate is not None else _default_regate
        ref = deck_ref if deck_ref is not None else _deck_ref(deck)
        rg = regate_fn(
            deck, ref, install=install, games=max(games, _MIN_REGATE_GAMES),
            engine=engine, data_dir=data_dir,
        )
        if rg.ok:
            state = 'valid'  # re-gate re-stamped a current meta — proceed Tier-2.
        elif rg.outcome == 'environment':
            return FundamentalTurn(
                turn=float(est.own_turn), confidence=_LOWER_CONFIDENCE[est.confidence], tier='tier1',
                source_rationale=(
                    'needs_tier2 and the per-deck driver is STALE (harness/deck moved under its '
                    f'stamp), but re-gate could not run in THIS environment: {rg.reason}. '
                    f'Using the tier-1 estimate. ({est.rationale})'
                ),
                tier2_recommended=True,
            )
        else:  # 'failed' → the recompile/gate REJECTED the driver; it is now broken.
            return FundamentalTurn(
                turn=float(est.own_turn), confidence=_LOWER_CONFIDENCE[est.confidence], tier='tier1',
                source_rationale=(
                    'needs_tier2 but the STALE per-deck driver FAILED re-gate against the current '
                    f'harness: {rg.reason}. Using the tier-1 estimate. ({est.rationale})'
                ),
                tier2_recommended=True,
            )

    if state == 'broken':
        # A driver present but rejected by its gate (gates_passed=false). Do NOT emit the quiet
        # "no driver" text — name the gate failure so the reason is actionable.
        meta = drivers.read_meta(deck, data_dir=data_dir)  # type: ignore[arg-type]
        detail = ''
        if meta is not None:
            detail = str(meta.extra.get('regate_failure_reason', '')) or 'gate did not sign off (gates_passed=false)'
        return FundamentalTurn(
            turn=float(est.own_turn), confidence=_LOWER_CONFIDENCE[est.confidence], tier='tier1',
            source_rationale=(
                'needs_tier2 but the per-deck driver is BROKEN (its gate rejected it): '
                f'{detail}. Using the tier-1 estimate. ({est.rationale})'
            ),
            tier2_recommended=True,
        )

    has_driver = state == 'valid'
    if has_driver and install is not None:
        eng = engine if engine is not None else _default_engine()
        classes = str(drivers.classes_dir(deck, data_dir=data_dir))  # type: ignore[arg-type]
        meta = drivers.read_meta(deck, data_dir=data_dir)  # type: ignore[arg-type]
        if meta is not None:
            ref = deck_ref if deck_ref is not None else _deck_ref(deck)
            # Commander decks run the commander-native solo (CommanderDuel, 40 life +
            # command zone) so the commander is seated — else a commander-dependent line
            # can never execute in the constructed goldfish (P6.2 Yawgmoth VETO).
            fmt = 'commander' if getattr(deck, 'commanders', None) else 'constructed'
            result = eng.goldfish(
                ref, games=games, install=install, driver=(classes, meta.fqcn), fmt=fmt
            )
            # RUBRIC FIDELITY (issue #30 spec of record, deckcheck Speed rubric): the
            # fundamental turn is "the score times the MEDIAN GAME" — the turn a kill
            # completes in ≥50% of ALL games, bricks counted as slow. A kills-only
            # median flatters a high-roll line (kills fast in <50% of games), so the
            # all-games median is authoritative when the harness reports it; a median
            # past the brick cap means the majority of games never killed — no honest
            # clock — and must fall back rather than return the flattering number.
            median_all = getattr(result, 'median_all_own', None)
            max_turn = getattr(result, 'max_turn', None)
            if median_all is not None:
                if median_all >= 0 and (max_turn is None or median_all <= max_turn):
                    median = float(median_all)
                else:
                    median = -1.0  # majority-brick → the no-kill fallback below.
            else:
                median = float(result.median_kills_own)  # pre-medianAllOwn harness output.
            if median >= 0:
                return FundamentalTurn(
                    turn=median, confidence='high', tier='tier2',
                    source_rationale=(
                        f'tier-2 driven goldfish ({meta.fqcn}, {games} games): '
                        f'median own-turn kill {median:g}. (tier-1: {est.rationale})'
                    ),
                    tier2_recommended=False,
                )
            # -1.0 sentinel: the driver fired but no kill landed in the goldfish
            # window. Do NOT return -1 as a turn — fall back to Tier-1, flagged.
            return FundamentalTurn(
                turn=float(est.own_turn), confidence=_LOWER_CONFIDENCE[est.confidence], tier='tier1',
                source_rationale=(
                    'tier-2 driven goldfish found no ≥50%-of-games kill in the window '
                    '(no kill at all, or the majority of games bricked); '
                    f'using the tier-1 estimate. ({est.rationale})'
                ),
                tier2_recommended=True,
            )

    # 4b. No valid driver / no install -> Tier-1 fallback + tier2_recommended.
    # AC3: this branch NEVER runs a naive (driverless) XMage goldfish.
    reason = 'no valid per-deck driver' if not has_driver else 'no XMage install available'
    return FundamentalTurn(
        turn=float(est.own_turn), confidence=_LOWER_CONFIDENCE[est.confidence], tier='tier1',
        source_rationale=(
            f'needs_tier2 but {reason} — tier-2 driven goldfish recommended; '
            f'using the tier-1 estimate. ({est.rationale})'
        ),
        tier2_recommended=True,
    )


def _default_engine() -> object:
    """The real XMage engine (lazy — the pure/fallback paths never import it)."""
    from pipeline.sim.engine import get_engine

    return get_engine('xmage')


def _default_regate(deck, deck_ref, **kwargs):  # type: ignore[no-untyped-def]
    """The real re-gate seam (lazy — the pure/fallback paths never import the gate/ECJ stack)."""
    from pipeline.sim.driver_gate import regate_driver

    return regate_driver(deck, deck_ref, **kwargs)
