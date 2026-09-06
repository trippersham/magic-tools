"""Tier-1 closed-form kill-completion speed estimator (NO engine, pure math).

Predicts a deck's fundamental **own-turn kill** — the turn it can realistically
CLOSE the game — from deck facts alone, by archetype-specific closed-form math.
This is emphatically **NOT naive findability** (the turn you first draw a combo
piece): the tier1-vs-tier2 spike proved naive findability under-predicts the real
goldfish by 2-3 turns because it ignores the develop -> assemble -> execute ->
connect tail. We model that tail.

Three models, one per archetype family:

  1. damage-race (aggro): accumulate curve-weighted creature power + burn reach
     per turn under a one-body-per-turn develop ramp-up; first turn cumulative
     damage crosses lethal (default 20) is the kill.
  2. mana-development + connect (ramp / midrange): turns to ramp to the deck's
     first castable top-end payoff, deploy it, then CONNECT (swings to lethal).
  3. draw-aware hypergeometric assembly + execution-lag (combo): the turn the
     hypergeometric draw model says every combo piece is assembled, PLUS an
     execution-lag term (deploy the remaining pieces + a turn to fire the loop).
     The closed form gives the assembly turn; the execution tail is what a DRIVEN
     goldfish resolves — but whether one runs is the router's call (keyed on the
     deck's authored driver richness), not a flag set here.

Control decks get **Speed N/A** (``own_turn=None``): a control deck has no honest
own-turn kill, so we refuse to emit a fake number.

Reuses the existing fact extractors rather than re-deriving curve/ramp/power:
``transforms.deck_factsheet`` (``is_land``, ``_mana``/``_is_ramp_source``,
``_shape``) and ``transforms.crispi`` (``_power``). Engine-free and DB-light: no
XMage, no network, no card DB (combo pieces arrive as pre-detected
``combo_detect.Combo`` rows or via combo otag slugs).

Every chosen constant states its reasoning in a comment AND in the returned
``rationale`` — these are judgment calls calibrated against the spike's measured
Tier-2 goldfish own-turns (MonoR_Aggro 5.0, Ramp_Green 7.0, Combo_Mikaeus ~7.0).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from math import ceil, comb

from pipeline.transforms.crispi import _power
from pipeline.transforms.deck_factsheet import (
    _is_ramp_source,
    _type_line,
    is_land,
)

__all__ = (
    'COMBO_ASSEMBLY_THRESHOLD',
    'DEFAULT_LETHAL',
    'DRAW_RATE_CAP',
    'DRAW_SPELL_YIELD',
    'RAMP_SATURATION_DENSITY',
    'RAMP_WINDOW',
    'SpeedEstimate',
    'detect_archetype',
    'estimate_speed',
    'p_all_pieces',
    'p_at_least_one',
)

# --------------------------------------------------------------------------- #
# Tunable constants — every one is a judgment call. Stated here once, cited in
# each model's rationale. Calibrated against the spike anchors, NOT curve-fit to
# them (a defensible model with a stated assumption beats a fudged constant).
# --------------------------------------------------------------------------- #

#: Default lethal damage. 20 = a 1v1 / constructed starting life total, which is
#: what the lab goldfish decks were measured against. Commander's 40 life (and the
#: 21-commander-damage rule) is a follow-on: pass ``lethal=40`` for a Commander
#: pod read. Only the RATIO damage/lethal matters, so this is the single biggest
#: lever on the aggro/ramp clocks.
DEFAULT_LETHAL = 20.0

#: Aggro: fraction of a haste creature's value realized as ONE extra attack on its
#: deploy turn (non-haste bodies wait a turn). A per-body, once-only bonus.
#: (No standalone constant — folded into the combat accumulator below.)

#: Ramp: how many of the opening turns you spend deploying ramp before pivoting to
#: threats. Ramp decks front-load acceleration for ~4 turns then cast their payoff;
#: past that the extra mana stops compounding the KILL turn (you've already reached
#: payoff mana). Caps the ramp acceleration window.
RAMP_WINDOW = 4

#: Ramp: a deck is "ramp-saturated" (ramp_rate 1.0 — a ramp piece essentially every
#: early turn) once its ramp density hits this fraction of the nonland base. 0.25 =
#: one ramp source per four nonland cards reliably puts one in the opening turns.
RAMP_SATURATION_DENSITY = 0.25

#: Combo: the assembly probability threshold. 0.35 is deliberately a FINDABILITY
#: bar (more than a third of games you have both pieces by now), NOT a
#: certainty bar — because we then ADD the execution-lag tail. A higher bar would
#: double-count the delay the lag term already models. This is the crux of "naive
#: findability under-predicts": findability ~turn 5 + lag 2 = the real ~turn 7.
COMBO_ASSEMBLY_THRESHOLD = 0.35

#: Combo: net extra cards a one-shot draw spell yields (draw two minus the card
#: itself ~= +1..+2). 1.5 is the conservative middle; it feeds the draw_rate that
#: accelerates the hypergeometric "cards seen per turn".
DRAW_SPELL_YIELD = 1.5

#: Combo: cap on the draw-acceleration rate (extra cards seen per turn). Even a
#: draw-heavy deck can't chain cantrips every turn while also developing mana and
#: combo pieces; 0.6 keeps the assembly turn honest.
DRAW_RATE_CAP = 0.6


# --------------------------------------------------------------------------- #
# Result type.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SpeedEstimate:
    """The closed-form own-turn kill estimate.

    ``own_turn`` is the predicted turn the deck closes (``None`` for control decks, which have no
    honest kill turn — Speed N/A). ``rationale`` states the math and the constants behind it.
    Whether Speed escalates to a driven goldfish is the router's decision, keyed on driver
    richness — not encoded here.
    """

    own_turn: float | None
    confidence: str
    archetype: str
    rationale: str


# --------------------------------------------------------------------------- #
# Small fact helpers (build on the reused extractors, never re-derive).
# --------------------------------------------------------------------------- #


def _expand(cards: list[dict]) -> list[dict]:
    """Expand each card row by its ``quantity`` (default 1) into physical copies.

    The factsheet counts one dict per physical card; a compact fixture (or a
    quantity-bearing decklist row) is expanded here so deck_size / ramp / creature
    counts are physical. Rows without ``quantity`` pass through as a single copy.
    """
    out: list[dict] = []
    for c in cards:
        try:
            n = max(1, int(c.get('quantity') or 1))
        except (TypeError, ValueError):
            n = 1
        out.extend([c] * n)
    return out


def _is_creature(card: dict) -> bool:
    return 'creature' in _type_line(card).lower() and not is_land(_type_line(card))


def _cmc(card: dict) -> float:
    try:
        return float(card.get('cmc') or 0.0)
    except (TypeError, ValueError):
        return 0.0


_HASTE = re.compile(r'\bhaste\b')
_DEALS_DAMAGE = re.compile(r'deals? (\d+) damage')
_DRAW_N_RE = re.compile(r'draw (a|one|two|three|four|five|six|seven|\d+) cards?')


def _has_haste(card: dict) -> bool:
    kws = {k.lower() for k in card.get('keywords') or []}
    return 'haste' in kws or bool(_HASTE.search((card.get('oracle_text') or '').lower()))


def _burn_damage(card: dict) -> float:
    """Face-damage of a noncreature, nonland burn spell (0 if it isn't one).

    Reads the first ``deals N damage`` from oracle text. Creatures are excluded
    (their damage is combat, counted in the creature clock). This is the burn
    "reach" that closes an aggro race after the board stalls.
    """
    if is_land(_type_line(card)) or _is_creature(card):
        return 0.0
    m = _DEALS_DAMAGE.search((card.get('oracle_text') or '').lower())
    return float(m.group(1)) if m else 0.0


# --------------------------------------------------------------------------- #
# Hypergeometric draw model (combo assembly).
# --------------------------------------------------------------------------- #


def p_at_least_one(deck_size: int, copies: int, drawn: int) -> float:
    """P(>=1 copy of a piece in ``drawn`` cards seen), hypergeometric, no replacement.

    ``1 - C(deck_size - copies, drawn) / C(deck_size, drawn)``. Clamped for the
    degenerate ends (no copies -> 0; you've seen the whole deck -> 1).
    """
    if copies <= 0 or drawn <= 0 or deck_size <= 0:
        return 0.0
    if drawn >= deck_size or deck_size - copies < drawn:
        return 1.0
    return 1.0 - comb(deck_size - copies, drawn) / comb(deck_size, drawn)


def p_all_pieces(deck_size: int, piece_copies: list[int], drawn: int) -> float:
    """P(>=1 of EVERY piece in ``drawn`` cards seen).

    APPROXIMATION: treats the pieces as independent (product of per-piece
    probabilities). Drawing copies of A slightly changes the pool for B, but with
    small copy counts against a large deck the correction is second-order; the
    dependence makes the true joint probability marginally LOWER, so this is a mild
    optimism we absorb into the findability threshold. Documented for Tier-2.
    """
    p = 1.0
    for c in piece_copies:
        p *= p_at_least_one(deck_size, c, drawn)
    return p


def _cards_seen(turn: int, draw_rate: float) -> int:
    """Cards seen by own-turn ``t`` on the play: 7 opener + (t-1) natural draws,
    accelerated by the deck's draw engine (``draw_rate`` extra cards per turn)."""
    return round(7 + (turn - 1) * (1.0 + draw_rate))


# --------------------------------------------------------------------------- #
# Archetype detection.
# --------------------------------------------------------------------------- #


def detect_archetype(
    cards: list[dict],
    card_otag: dict[str, set[str]] | None = None,
    *,
    combo_pieces: list[list[int]] | None = None,
) -> str:
    """Classify a deck into aggro | ramp | midrange | combo | control from facts.

    Signals (precision-first, in priority order):
      * combo — a detected multi-card combo is present (``combo_pieces`` non-empty).
        Finding+firing a named combo is the deck's plan regardless of its shell.
      * control — heavy interaction (removal + counters + wipes) with very few
        threats: no honest own-turn kill.
      * aggro — low average CMC and a creature-dense, ramp-light board.
      * ramp — a ramp-saturated base with a real top-end (cmc>=6) payoff.
      * midrange — the residual (threats + some ramp, no combo, not aggro-fast).
    """
    expanded = _expand(cards)
    nonland = [c for c in expanded if not is_land(_type_line(c))]
    if not nonland:
        return 'midrange'

    creatures = [c for c in nonland if _is_creature(c)]
    ramp = sum(1 for c in expanded if _is_ramp_source(c))
    top_end = sum(1 for c in nonland if _cmc(c) >= 6)
    avg_cmc = sum(_cmc(c) for c in nonland) / len(nonland)
    n = len(nonland)

    if combo_pieces:
        return 'combo'

    # Control: interaction-dense with few threats. Reuse the factsheet text tells.
    from pipeline.transforms.deck_factsheet import (
        _is_board_wipe,
        _is_counterspell,
        _is_spot_removal,
    )

    interaction = sum(1 for c in nonland if _is_counterspell(c) or _is_spot_removal(c) or _is_board_wipe(c))
    threats = sum(1 for c in creatures if _power(c) >= 3) + top_end
    if interaction >= max(6, 0.30 * n) and threats <= 0.10 * n:
        return 'control'

    # Aggro: fast, creature-dense, ramp-light, low curve.
    if avg_cmc <= 2.5 and len(creatures) >= 0.30 * n and ramp <= 0.05 * n:
        return 'aggro'

    # Ramp: saturated ramp base with a genuine top end to ramp toward.
    if ramp >= RAMP_SATURATION_DENSITY * n and top_end >= 3:
        return 'ramp'

    return 'midrange'


# --------------------------------------------------------------------------- #
# Model 1 — damage-race (aggro).
# --------------------------------------------------------------------------- #


def _estimate_aggro(cards: list[dict], lethal: float) -> SpeedEstimate:
    """Turns to deal ``lethal`` from a curve-weighted creature clock + burn reach.

    Develop ramp-up: you deploy roughly ONE body per turn (you don't empty your
    hand turn 1 — the spike's key correction over naive findability). A body
    deployed on turn ``i`` attacks on turns ``i+1..t`` (non-haste), so by turn ``t``:

        combat(t) = avg_power * ( sum_{i=1..min(t,N)} (t - i)   # attacks landed
                                  + haste_frac * min(t, N) )     # +1 haste attack/body

    Burn is thrown at the face at ~one spell per turn from turn 2 (early mana buys
    creatures first): burn(t) = min(max(t-1, 0), n_burn) * avg_burn_damage. First
    turn combat(t) + burn(t) >= lethal is the kill.
    """
    expanded = _expand(cards)
    creatures = [c for c in expanded if _is_creature(c)]
    burns = [_burn_damage(c) for c in expanded]
    burns = [b for b in burns if b > 0]

    if not creatures and not burns:
        return SpeedEstimate(None, 'low', 'aggro', 'no creatures or burn — no aggro clock.')

    n_creatures = len(creatures)
    avg_power = (sum(_power(c) for c in creatures) / n_creatures) if n_creatures else 0.0
    haste_frac = (sum(1 for c in creatures if _has_haste(c)) / n_creatures) if n_creatures else 0.0
    n_burn = len(burns)
    avg_burn = (sum(burns) / n_burn) if n_burn else 0.0

    for t in range(1, 21):
        bodies = min(t, n_creatures)
        landed = sum(t - i for i in range(1, bodies + 1))  # attacks by bodies deployed 1..bodies
        combat = avg_power * (landed + haste_frac * bodies)
        burn = min(max(t - 1, 0), n_burn) * avg_burn
        if combat + burn >= lethal:
            rationale = (
                f'aggro damage-race: {n_creatures} creatures avg power {avg_power:.1f} '
                f'({haste_frac * 100:.0f}% haste), one body/turn; {n_burn} burn spells '
                f'avg {avg_burn:.1f} to face from turn 2. Cumulative damage crosses '
                f'lethal={lethal:g} on own-turn {t} '
                f'(combat {combat:.1f} + burn {burn:.1f}).'
            )
            return SpeedEstimate(float(t), 'high', 'aggro', rationale)

    return SpeedEstimate(
        20.0,
        'low',
        'aggro',
        f'aggro clock never reaches lethal={lethal:g} within 20 turns (grindy).',
    )


# --------------------------------------------------------------------------- #
# Model 2 — mana-development + connect (ramp / midrange).
# --------------------------------------------------------------------------- #


def _mana_available(turn: int, ramp_rate: float) -> float:
    """Mana on own-turn ``t``: one land/turn + ramp acceleration (a ramp piece
    resolved on each of the first ``RAMP_WINDOW`` turns adds +1 going forward)."""
    return turn + min(max(turn - 1, 0), RAMP_WINDOW) * ramp_rate


def _estimate_ramp(cards: list[dict], lethal: float, archetype: str) -> SpeedEstimate:
    """Turns to ramp to the first castable top-end payoff, deploy it, then connect.

        own_turn = deploy_turn + ceil(lethal / payoff_power)

    ``deploy_turn`` is the first turn mana reaches the payoff's CMC (mana =
    one/turn + ramp acceleration). The payoff is the CHEAPEST creature of CMC>=6
    (the first big threat you actually ramp onto — the finisher is upside, not the
    altitude of the goldfish). Connect = swings needed for lethal, since the threat
    is summoning-sick the turn it lands and attacks the following turns.
    """
    expanded = _expand(cards)
    nonland = [c for c in expanded if not is_land(_type_line(c))]
    ramp = sum(1 for c in expanded if _is_ramp_source(c))
    n = len(nonland) or 1
    # Ramp density -> acceleration rate (1.0 = a ramp source essentially every early
    # turn once density hits RAMP_SATURATION_DENSITY of the nonland base).
    ramp_rate = min(1.0, (ramp / n) / RAMP_SATURATION_DENSITY)

    top_end = [c for c in nonland if _is_creature(c) and _cmc(c) >= 6]
    if top_end:
        # Cheapest top-end = first castable; tie-break on higher power (better clock).
        payoff = min(top_end, key=lambda c: (_cmc(c), -_power(c)))
    else:
        # No true top-end: fall back to the biggest creature as the payoff threat.
        creatures = [c for c in nonland if _is_creature(c)]
        if not creatures:
            return SpeedEstimate(None, 'low', archetype, 'no creature payoff to connect with.')
        payoff = max(creatures, key=_power)

    payoff_cmc = _cmc(payoff)
    payoff_power = _power(payoff) or 1.0

    deploy_turn = next((t for t in range(1, 21) if _mana_available(t, ramp_rate) >= payoff_cmc), 20)
    connect = ceil(lethal / payoff_power)
    own_turn = float(deploy_turn + connect)

    confidence = 'high' if archetype == 'ramp' else 'medium'
    rationale = (
        f'{archetype} mana-development+connect: {ramp} ramp sources (rate {ramp_rate:.2f}) '
        f'reach the payoff {payoff.get("name", "?")} (CMC {payoff_cmc:g}, power {payoff_power:g}) '
        f'on deploy-turn {deploy_turn}; connect needs ceil(lethal={lethal:g}/{payoff_power:g})='
        f'{connect} swings -> own-turn {own_turn:g}.'
    )
    return SpeedEstimate(own_turn, confidence, archetype, rationale)


# --------------------------------------------------------------------------- #
# Model 3 — hypergeometric assembly + execution-lag (combo).
# --------------------------------------------------------------------------- #


def _combo_piece_copies(cards: list[dict], combo_pieces: list[list[int]]) -> tuple[list[int], int]:
    """Return (piece_copies, num_pieces) for the FASTEST detected combo.

    ``combo_pieces`` is a list of combos, each a list of per-piece copy counts
    (already including any tutors folded into a piece's effective copies). The
    fastest combo is the one with the FEWEST pieces (and, tie-broken, the most
    copies) — it assembles soonest. Returns its per-piece copy list.
    """
    if not combo_pieces:
        return [], 0
    best = min(combo_pieces, key=lambda pcs: (len(pcs), -sum(pcs)))
    return list(best), len(best)


def _estimate_combo(
    cards: list[dict],
    combo_pieces: list[list[int]],
    lethal: float,
) -> SpeedEstimate:
    """Draw-aware hypergeometric assembly turn + execution-lag.

        assembly_turn = first t with P(all pieces drawn by cards_seen(t)) >= 0.35
        execution_lag = (num_pieces - 1) deploy turns + 1 activation turn = num_pieces
        own_turn      = assembly_turn + execution_lag   (floored by mana for the
                        most expensive piece)

    The 0.35 threshold is a FINDABILITY bar, not certainty: naive findability is
    exactly this, and the spike showed it under-predicts by 2-3 turns — so we ADD
    the execution-lag (you still have to cast the pieces one per turn and spend a
    turn firing the loop). The closed form nails the assembly turn; the true execution
    tail (interaction, mana sequencing, the actual loop) is what a DRIVEN goldfish
    resolves — if the router escalates (driver-richness call), not a flag set here.
    """
    expanded = _expand(cards)
    deck_size = len(expanded)
    piece_copies, num_pieces = _combo_piece_copies(cards, combo_pieces)

    if num_pieces == 0 or deck_size == 0:
        return SpeedEstimate(None, 'low', 'combo', 'no combo pieces detected.')

    # Draw acceleration from one-shot draw spells (Sign in Blood / Night's Whisper
    # class): each nets ~DRAW_SPELL_YIELD cards; spread over the deck as an extra
    # per-turn rate, capped so a cantrip chain can't run unboundedly.
    draw_spells = sum(
        1 for c in expanded if _DRAW_N_RE.search((c.get('oracle_text') or '').lower()) and not is_land(_type_line(c))
    )
    draw_rate = min(DRAW_RATE_CAP, draw_spells * DRAW_SPELL_YIELD / deck_size)

    assembly_turn = 20
    p_at_assembly = 0.0
    for t in range(1, 21):
        p = p_all_pieces(deck_size, piece_copies, _cards_seen(t, draw_rate))
        if p >= COMBO_ASSEMBLY_THRESHOLD:
            assembly_turn = t
            p_at_assembly = p
            break

    # Execution-lag: deploy the remaining (num_pieces-1) pieces one per turn, then
    # one turn to fire the loop.
    execution_lag = num_pieces  # (num_pieces - 1) + 1
    own_turn = float(assembly_turn + execution_lag)

    # Mana floor: you cannot fire before you can cast the most expensive piece.
    ramp = sum(1 for c in expanded if _is_ramp_source(c))
    nonland_n = sum(1 for c in expanded if not is_land(_type_line(c))) or 1
    ramp_rate = min(1.0, (ramp / nonland_n) / RAMP_SATURATION_DENSITY)
    # Mana wall assumption: ``combo_pieces`` carries copy counts, not CMCs, so we
    # can't read the exact cost. A two/three-card combo's heaviest piece is
    # typically ~6 mana (Triskelion class); require the loop not fire before mana
    # reaches 6. Documented approximation — Tier-2 sees the real sequencing.
    mana_turn = next((t for t in range(1, 21) if _mana_available(t, ramp_rate) >= 6.0), 20)
    own_turn = max(own_turn, float(mana_turn))

    rationale = (
        f'combo hypergeometric-assembly + execution-lag: {num_pieces} pieces '
        f'(copies {piece_copies}) in a {deck_size}-card deck, draw_rate {draw_rate:.2f}. '
        f'P(all pieces assembled) crosses {COMBO_ASSEMBLY_THRESHOLD:g} on turn '
        f'{assembly_turn} (P={p_at_assembly:.2f}); +execution-lag {execution_lag} '
        f'(deploy {num_pieces - 1} + fire 1) -> own-turn {own_turn:g}.'
    )
    return SpeedEstimate(own_turn, 'low', 'combo', rationale)


# --------------------------------------------------------------------------- #
# Entry point.
# --------------------------------------------------------------------------- #


def estimate_speed(
    cards: list[dict],
    card_otag: dict[str, set[str]] | None = None,
    *,
    archetype: str | None = None,
    combo_pieces: list[list[int]] | None = None,
    lethal: float = DEFAULT_LETHAL,
) -> SpeedEstimate:
    """Estimate a deck's own-turn kill (Tier-1 closed form).

    Args:
        cards: Resolved card-fact dicts (name/cmc/type_line/power/oracle_text/
            keywords/produced_mana/mana_cost). ``quantity`` (default 1) expands to
            physical copies.
        card_otag: ``oracle_id -> set[slug]`` closure (reserved; the closed forms
            read structured facts, not otags, today).
        archetype: Override the detector (keeps tests deterministic). One of
            aggro | ramp | midrange | combo | control.
        combo_pieces: Pre-detected combos as per-piece copy-count lists (from
            ``combo_detect.combos_in_deck``, or hand-supplied). Selects the combo
            model and feeds its hypergeometric assembly.
        lethal: Damage to win (default 20 = 1v1 life; pass 40 for Commander).

    Returns:
        A :class:`SpeedEstimate`. Control -> ``own_turn=None`` (Speed N/A). Every other
        archetype returns its closed-form own-turn; escalation to a driven goldfish is the
        router's decision (driver richness), not encoded in the estimate.
    """
    arch = archetype or detect_archetype(cards, card_otag, combo_pieces=combo_pieces)

    if arch == 'control':
        return SpeedEstimate(
            None,
            'n/a',
            'control',
            'control: no honest own-turn kill (wins by attrition/inevitability, not a closed-form clock) — Speed N/A.',
        )
    if arch == 'combo':
        return _estimate_combo(cards, combo_pieces or [], lethal)
    if arch == 'aggro':
        return _estimate_aggro(cards, lethal)
    # ramp / midrange share the mana-development + connect model.
    return _estimate_ramp(cards, lethal, arch)
