"""Pipeline ``factsheet_for`` — the otag-powered deck fact sheet mart.

Emits a multi-label ``otag_buckets`` map (bucket -> card count) and a
data-grounded ``susceptibility`` list, on top of the structured facts. Its
output validates against ``contracts.FactSheet`` (``model_validate``).

Inputs are card dicts already resolved with an ``oracle_id`` and the structured
Scryfall fields (name/cmc/type_line/keywords/produced_mana/mana_cost/
oracle_text). The otag join is supplied as ``card_otag`` — an
``oracle_id -> set[slug]`` map (the rolled-up slug closure from ``otag_rollup``).

The structured facts (shape/mana/interaction/card_advantage/structural/keywords)
are recomputed here from the same precision-first rules used at the scripts
boundary, so this mart is self-contained. Functional categorization comes from
otag buckets rather than an oracle-text regex census (which leaves 60%+ of
nonlands uncategorized).
"""

from __future__ import annotations

import re
from collections import Counter

from pipeline.transforms.crosswalk import BUCKETS, buckets_for

_CMC_BUCKETS = ('0', '1', '2', '3', '4', '5', '6', '7+')
_PIP_SYMBOLS = ('W', 'U', 'B', 'R', 'G', 'C')

#: A focus entry is "thin" when fewer than this many cards support it. Small by
#: design: the point of `thin_focus` is "you care about X but have little of it".
_THIN_FOCUS_THRESHOLD = 2


# --------------------------------------------------------------------------- #
# Small structured-fact helpers (precision-first; mirror scripts/deck_factsheet).
# --------------------------------------------------------------------------- #


def is_land(type_line: str) -> bool:
    """A card is a land iff its FRONT face is a land (modal DFC spell // land is a spell)."""
    front = (type_line or '').split('//')[0]
    return 'land' in front.lower()


def _text(card: dict) -> str:
    return (card.get('oracle_text') or '').lower()


def _type_line(card: dict) -> str:
    return card.get('type_line') or ''


def _is_creature(card: dict) -> bool:
    return 'creature' in _type_line(card).lower()


def _is_instant_speed(card: dict) -> bool:
    if 'instant' in _type_line(card).lower():
        return True
    return 'flash' in {k.lower() for k in card.get('keywords', []) or []}


_BOARD_WIPE_PATTERNS = (
    re.compile(r'destroy all creatures'),
    re.compile(r'destroy all (nonland )?permanents'),
    re.compile(r'exile all creatures'),
    re.compile(r'deals? \d+ damage to each creature'),
    re.compile(r'all creatures get -'),
)
_SPOT_REMOVAL_PATTERN = re.compile(
    r'(destroy|exile) target '
    r'(creature|permanent|artifact|enchantment|planeswalker|land|'
    r'nonland permanent|creature or planeswalker|creature or enchantment|'
    r'artifact or enchantment|artifact or creature)'
)
_COUNTER_PATTERN = re.compile(r'counter target')
_PROTECTION_KEYWORDS = {'hexproof', 'indestructible', 'ward', 'shroud'}
_PROTECTION_TEXT_PATTERNS = (
    re.compile(r'\b(hexproof|indestructible|shroud)\b'),
    re.compile(r'\bward\b'),
    re.compile(r'protection from'),
    re.compile(r'phases? out'),
)
_LAND_FETCH_PATTERN = re.compile(r'search your library for .{0,60}?\bland', re.DOTALL)
_LAND_TO_BATTLEFIELD = re.compile(r'onto the battlefield')
_REPEATABLE_DRAW_PATTERNS = (
    re.compile(r'at the beginning of .{0,80}?draw', re.DOTALL),
    re.compile(r'whenever .{0,80}?draw a card', re.DOTALL),
)
_ONE_SHOT_DRAW_PATTERN = re.compile(r'draw (a|one|two|three|four|five|six|seven|\d+) cards?')
_ETB_PATTERN = re.compile(r'when(ever)? .{0,40}?enters', re.DOTALL)
_GRAVEYARD_RECURSION_PATTERN = re.compile(r'return .{0,60}?from .{0,30}?graveyard to the battlefield', re.DOTALL)


def _is_board_wipe(c: dict) -> bool:
    t = _text(c)
    return any(p.search(t) for p in _BOARD_WIPE_PATTERNS)


def _is_spot_removal(c: dict) -> bool:
    return bool(_SPOT_REMOVAL_PATTERN.search(_text(c)))


def _is_counterspell(c: dict) -> bool:
    return bool(_COUNTER_PATTERN.search(_text(c)))


def _is_protection(c: dict) -> bool:
    if {k.lower() for k in c.get('keywords', []) or []} & _PROTECTION_KEYWORDS:
        return True
    t = _text(c)
    return any(p.search(t) for p in _PROTECTION_TEXT_PATTERNS)


def _produces_mana(c: dict) -> bool:
    return bool(c.get('produced_mana'))


def _is_land_fetch_ramp(c: dict) -> bool:
    t = _text(c)
    return bool(_LAND_FETCH_PATTERN.search(t)) and bool(_LAND_TO_BATTLEFIELD.search(t))


def structured_ramp(c: dict) -> bool:
    """A NONLAND ramp source keyed on structured ``produced_mana`` ONLY.

    The precision-first, regex-free ramp signal (no oracle-text). This is the
    baseline the scripts fallback census reuses so there is ONE copy of the
    produced_mana ramp rule; the pipeline path layers ``_is_land_fetch_ramp`` on
    top of it (see ``_is_ramp_source``).
    """
    if is_land(_type_line(c)):
        return False
    return _produces_mana(c)


def _is_ramp_source(c: dict) -> bool:
    if is_land(_type_line(c)):
        return False
    return structured_ramp(c) or _is_land_fetch_ramp(c)


def _is_fixing_source(c: dict) -> bool:
    if is_land(_type_line(c)):
        return False
    colors = {m for m in (c.get('produced_mana') or []) if m in ('W', 'U', 'B', 'R', 'G')}
    return len(colors) > 1


def _is_repeatable_draw(c: dict) -> bool:
    t = _text(c)
    return any(p.search(t) for p in _REPEATABLE_DRAW_PATTERNS)


def _is_one_shot_draw(c: dict) -> bool:
    return bool(_ONE_SHOT_DRAW_PATTERN.search(_text(c)))


def _has_etb(c: dict) -> bool:
    return bool(_ETB_PATTERN.search(_text(c)))


def _cmc_bucket(cmc: float) -> str:
    n = int(cmc or 0)
    return '7+' if n >= 7 else str(n)


# --------------------------------------------------------------------------- #
# Structured-fact blocks (shape / mana / interaction / card_advantage / etc.).
# --------------------------------------------------------------------------- #


def _shape(cards: list[dict]) -> dict:
    nonland = [c for c in cards if not is_land(_type_line(c))]
    hist: dict[str, int] = dict.fromkeys(_CMC_BUCKETS, 0)
    for c in nonland:
        hist[_cmc_bucket(c.get('cmc') or 0)] += 1
    avg = round(sum(float(c.get('cmc') or 0) for c in nonland) / len(nonland), 2) if nonland else 0.0
    return {
        'nonland_count': len(nonland),
        'land_count': sum(1 for c in cards if is_land(_type_line(c))),
        'cmc_histogram': hist,
        'avg_cmc': avg,
        'top_end_count': sum(1 for c in nonland if float(c.get('cmc') or 0) >= 6),
    }


def _pip_counts(cards: list[dict]) -> dict:
    counts = dict.fromkeys(_PIP_SYMBOLS, 0)
    for c in cards:
        if is_land(_type_line(c)):
            continue
        for sym in re.findall(r'\{([^}]+)\}', c.get('mana_cost') or ''):
            for part in sym.split('/'):
                if part in counts:
                    counts[part] += 1
    return counts


def _mana(cards: list[dict]) -> dict:
    return {
        'ramp_sources': sum(1 for c in cards if _is_ramp_source(c)),
        'fixing_sources': sum(1 for c in cards if _is_fixing_source(c)),
        'pip_counts': _pip_counts(cards),
    }


def _interaction(cards: list[dict]) -> dict:
    return {
        'board_wipes': sum(1 for c in cards if _is_board_wipe(c)),
        'spot_removal': sum(1 for c in cards if _is_spot_removal(c)),
        'counterspells': sum(1 for c in cards if _is_counterspell(c)),
        'protection': sum(1 for c in cards if _is_protection(c)),
        'instant_speed': sum(1 for c in cards if _is_instant_speed(c)),
    }


def _card_advantage(cards: list[dict]) -> dict:
    repeatable = one_shot = 0
    for c in cards:
        if _is_repeatable_draw(c):
            repeatable += 1
        elif _is_one_shot_draw(c):
            one_shot += 1
    return {'repeatable_draw': repeatable, 'one_shot_draw': one_shot}


def _structural(cards: list[dict]) -> dict:
    return {
        'etb_creatures': sum(1 for c in cards if _is_creature(c) and _has_etb(c)),
        'graveyard_recursion_present': any(_GRAVEYARD_RECURSION_PATTERN.search(_text(c)) for c in cards),
    }


def _keywords(cards: list[dict]) -> dict:
    counter: Counter[str] = Counter()
    for c in cards:
        for kw in c.get('keywords', []) or []:
            counter[kw] += 1
    return dict(counter)


def _card_record(c: dict) -> dict:
    return {
        'name': c.get('name', ''),
        'cmc': c.get('cmc'),
        'type_line': _type_line(c),
        'keywords': c.get('keywords', []) or [],
        'produced_mana': c.get('produced_mana'),
        'is_land': is_land(_type_line(c)),
        'oracle_text': c.get('oracle_text') or '',
    }


# --------------------------------------------------------------------------- #
# The otag payoff: multi-label buckets + coverage + susceptibility.
# --------------------------------------------------------------------------- #


def _card_slugs(card: dict, card_otag: dict[str, set[str]]) -> set[str]:
    """The rolled-up slug closure for a card (empty if untagged / no oracle_id)."""
    oid = card.get('oracle_id')
    if not oid:
        return set()
    return set(card_otag.get(str(oid), set()))


def otag_buckets(cards: list[dict], card_otag: dict[str, set[str]]) -> dict[str, int]:
    """Multi-label bucket -> NONLAND card count.

    A card counts once in EVERY bucket its slug closure hits (Cultivate ->
    ramp+tutor). Only buckets with a nonzero count appear. Lands are excluded so
    the count aligns with the nonland denominator used for coverage.
    """
    counts: Counter[str] = Counter()
    for c in cards:
        if is_land(_type_line(c)):
            continue
        for bucket in buckets_for(_card_slugs(c, card_otag)):
            counts[bucket] += 1
    # Stable, curated bucket order; nonzero only.
    return {b: counts[b] for b in BUCKETS if counts[b] > 0}


def _coverage(cards: list[dict], card_otag: dict[str, set[str]]) -> dict:
    """otag coverage: % of NONLAND cards that land in >=1 bucket.

    The uncategorized list is the residual (cards no bucket claims), the synergy /
    low-signal tell the reasoning layer inspects.
    """
    nonland = [c for c in cards if not is_land(_type_line(c))]
    total = len(nonland)
    if total == 0:
        return {
            'categorized_pct': 0.0,
            'uncategorized_pct': 0.0,
            'uncategorized_cards': [],
        }
    uncategorized = [c for c in nonland if not buckets_for(_card_slugs(c, card_otag))]
    categorized_n = total - len(uncategorized)
    return {
        'categorized_pct': round(categorized_n / total * 100, 2),
        'uncategorized_pct': round(len(uncategorized) / total * 100, 2),
        'uncategorized_cards': [c.get('name', '') for c in uncategorized],
    }


def susceptibility(
    buckets: dict[str, int],
    interaction: dict,
    structural: dict,
    cards: list[dict],
    card_otag: dict[str, set[str]],
) -> list[str]:
    """Data-grounded weaknesses: over-reliance on X + X has a common answer +
    deck lacks the resilience. Each signal CITES the counts driving it.

    This is the research's susceptibility model. It is deliberately conservative
    and count-referenced (the tag->answer mapping is a reasoning layer, but every
    claim points at concrete numbers so it is auditable, not a vibe).
    """
    signals: list[str] = []
    tokens = buckets.get('tokens', 0)
    counters = buckets.get('counters', 0)
    sweepers = interaction.get('board_wipes', 0)
    recursion = structural.get('graveyard_recursion_present', False)
    board_units = tokens + counters
    etb = structural.get('etb_creatures', 0)

    # 1. Board-wipe susceptibility: a wide/go-tall board with little to rebuild.
    if board_units >= 6 and sweepers <= 1 and not recursion:
        signals.append(
            f'Board wipes: {board_units} token/counter payoff cards '
            f'({tokens} token, {counters} counter) but only {sweepers} sweeper(s) '
            f'and no graveyard recursion to rebuild — a wrath erases the board state.'
        )

    # 2. Can't protect the payoff / can't disrupt on the stack: 0 counterspells.
    if interaction.get('counterspells', 0) == 0:
        signals.append(
            'Stack interaction: 0 counterspells — cannot protect the payoff or '
            'answer an opposing combo/spell before it resolves.'
        )

    # 3. ETB / flicker engine reliant on creatures staying alive.
    flicker = buckets.get('flicker', 0)
    if flicker >= 2 and etb >= 4 and sweepers <= 1:
        signals.append(
            f'Board wipes hit the engine: {flicker} flicker + {etb} ETB creatures '
            f'drive value, but only {sweepers} sweeper(s) of your own — a wrath '
            f'strands the value engine.'
        )

    # 4. Typal dependency (PTTD's tell): heavy changeling / typal-hero.
    typal = sum(
        1
        for c in cards
        if not is_land(_type_line(c)) and _card_slugs(c, card_otag) & {'changeling', 'typal-hero', 'typal-share'}
    )
    if typal >= 4:
        signals.append(
            f'Typal hate / non-creature answers: {typal} cards depend on the tribal '
            f'payoff (changeling / typal-hero) — typal hate or board wipes hit a '
            f'hard dependency.'
        )

    # 5. Life-loss/aristocrat kill reliant, vulnerable to lifegain.
    burn = buckets.get('burn', 0)
    if burn >= 5 and buckets.get('removal', 0) <= 2:
        signals.append(
            f'Lifegain: {burn} burn/life-loss/drain cards carry the kill but only '
            f'{buckets.get("removal", 0)} removal — opposing lifegain outpaces the clock.'
        )

    return signals


# --------------------------------------------------------------------------- #
# Focus-relative analysis — actual card tags measured vs the deck's NARROW,
# skill-authored focus set. The engine only READS the focus; it never writes it.
# --------------------------------------------------------------------------- #


def _card_supports_focus_entry(card: dict, entry: str, card_otag: dict[str, set[str]]) -> bool:
    """Does one card support a single focus entry?

    A focus entry resolves at BOTH levels of the actual derived set:
      * bucket-level — the entry is a bucket name (e.g. ``counters``, ``tokens``):
        the card supports it iff the entry is among the card's rolled-up buckets
        (``buckets_for`` of its slug closure).
      * slug-level — the entry is a raw otag slug (e.g. ``land-ramp``): the card
        supports it iff the entry is in the card's slug closure directly.

    A bucket name and a slug never collide in practice, but checking both makes
    the membership test robust to either kind of focus entry.
    """
    slugs = _card_slugs(card, card_otag)
    if entry in slugs:
        return True
    return entry in buckets_for(slugs)


def focus_relative(
    cards: list[dict],
    buckets: dict[str, int],
    focus: list[str],
    card_otag: dict[str, set[str]],
) -> dict:
    """Measure actual (nonland) card tags against the deck's declared focus.

    Args:
        cards: The deck's resolved card dicts.
        buckets: The already-computed multi-label ``otag_buckets`` (bucket ->
            nonland card count) — used to surface prominent OFF-focus buckets.
        focus: The deck's NARROW declared focus set (bucket names and/or otag
            slugs). Read-only; never mutated, never persisted.
        card_otag: ``oracle_id -> set[slug]`` closure.

    Returns:
        ``{coverage_of_focus, thin_focus, off_focus}``:
          * ``coverage_of_focus``: focus entry -> count of NONLAND cards supporting
            it (bucket- or slug-level). Every declared entry is echoed (0 support
            included), so thin items stay visible.
          * ``thin_focus``: focus entries with support < ``_THIN_FOCUS_THRESHOLD``.
          * ``off_focus``: prominent card buckets present in the deck but NOT in
            the focus set (and not implied by a slug-level focus entry).
    """
    nonland = [c for c in cards if not is_land(_type_line(c))]

    coverage: dict[str, int] = {}
    for entry in focus:
        coverage[entry] = sum(1 for c in nonland if _card_supports_focus_entry(c, entry, card_otag))

    thin = [entry for entry in focus if coverage[entry] < _THIN_FOCUS_THRESHOLD]

    # off_focus: prominent card buckets the deck did NOT declare. A focus entry
    # can be a bucket name directly, or a slug that rolls up INTO a bucket — either
    # way that bucket is "declared" and should not read as off-focus noise.
    focus_set = set(focus)
    declared_buckets = set(focus_set)
    for entry in focus:
        # A slug-level focus entry declares whatever bucket(s) it belongs to.
        declared_buckets |= buckets_for({entry})
    off = [b for b in BUCKETS if buckets.get(b, 0) > 0 and b not in declared_buckets]

    return {
        'coverage_of_focus': coverage,
        'thin_focus': thin,
        'off_focus': off,
    }


# --------------------------------------------------------------------------- #
# The mart entry point.
# --------------------------------------------------------------------------- #


def factsheet_for(
    deck_cards: list[dict],
    *,
    card_otag: dict[str, set[str]] | None = None,
    deck: str | None = None,
    missing: list[str] | None = None,
    focus: list[str] | None = None,
) -> dict:
    """Build the otag-powered, ``contracts.FactSheet``-valid fact sheet.

    Args:
        deck_cards: Resolved card dicts (name/oracle_id/cmc/type_line/keywords/
            produced_mana/mana_cost/oracle_text).
        card_otag: ``oracle_id -> set[slug]`` rolled-up slug closure (from
            ``otag_rollup``). Defaults to empty (buckets/susceptibility empty).
        deck: Optional deck name.
        missing: Decklist names that did not resolve to a card.
        focus: The deck's NARROW, skill-authored focus set (``Focus Otags`` —
            bucket names and/or otag slugs). READ-ONLY: this transform measures
            the deck's actual card tags against it and NEVER authors or persists
            it. When None/empty the focus fields are emitted empty and the output
            is byte-identical to the pre-focus build.

    Returns:
        A dict that satisfies ``contracts.FactSheet.model_validate``. The
        structured facts mirror the scripts census; ``coverage`` is the OTAG
        coverage; ``otag_buckets`` + ``susceptibility`` are the otag-derived
        fields; ``focus`` + ``focus_relative`` are the focus-relative
        signals (empty when no focus is supplied).
    """
    otag = card_otag or {}
    focus = focus or []
    interaction = _interaction(deck_cards)
    structural = _structural(deck_cards)
    buckets = otag_buckets(deck_cards, otag)
    focus_block = (
        focus_relative(deck_cards, buckets, focus, otag)
        if focus
        else {'coverage_of_focus': {}, 'thin_focus': [], 'off_focus': []}
    )
    return {
        'deck': deck,
        'shape': _shape(deck_cards),
        'mana': _mana(deck_cards),
        'keywords': _keywords(deck_cards),
        'interaction': interaction,
        'card_advantage': _card_advantage(deck_cards),
        'structural': structural,
        'coverage': _coverage(deck_cards, otag),
        'cards': [_card_record(c) for c in deck_cards],
        'missing': missing or [],
        'otag_buckets': buckets,
        'susceptibility': susceptibility(buckets, interaction, structural, deck_cards, otag),
        'focus': list(focus),
        'focus_relative': focus_block,
    }
