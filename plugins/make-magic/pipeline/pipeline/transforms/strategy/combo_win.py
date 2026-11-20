"""Closure-mechanism win-walk — the AUTHORITATIVE combo win-detection path (ADR 0016).

Phase 2B step 5b-ii: the combo win-detection CLOSURE WALK is the source of
truth for ``resolve()`` / combo flags / DRIVE.  It replaces the regex path
(``is_game_win_result`` + ``LETHAL_MANA_SINK_NAMES``) with a graph walk over
the fid-to-Event adapter (``fid_adapter.py``) and card-edge classes
(``data/card_edges.json``).

**This module IS imported by strategy_graph.py** and determines every combo's win flag.
The walk is deck-aware: a combo's win-ness depends on the deck's card
composition (e.g. infinite mana + a mana_sink card = win).
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from pipeline.transforms.strategy.fid_adapter import load_adapter

# ---------------------------------------------------------------------------
# Card edge data (derived from the prototype's card_edges.json)
# ---------------------------------------------------------------------------

_CARD_EDGES_PATH = Path(__file__).resolve().parent / 'data' / 'card_edges.json'


def _norm(name: str) -> str:
    """Case/space-insensitive card-name key."""
    return ' '.join((name or '').split()).casefold()


@cache
def _load_card_edges() -> dict[str, frozenset[str]]:
    """Load the card edge classes → frozenset of normalized card names.

    Falls back to an inline minimal set if the data file doesn't exist yet.
    """
    if _CARD_EDGES_PATH.exists():
        with _CARD_EDGES_PATH.open() as f:
            raw = json.load(f)
        return {cls: frozenset(_norm(c) for c in cards) for cls, cards in raw.items() if not cls.startswith('_')}
    return _FALLBACK_CARD_EDGES


# Minimal fallback card edges (enough for tests; the full data comes from
# the prototype's lake-derived card_edges.json).
_FALLBACK_CARD_EDGES: dict[str, frozenset[str]] = {}  # populated below


# ---------------------------------------------------------------------------
# Event → edge class mapping (which card edge classes convert each event)
# ---------------------------------------------------------------------------

#: For each produced event, which card edge classes can convert it.
EVENT_EDGE_CLASSES: dict[str, list[str]] = {
    'mana-produced': ['mana_sink', 'altwin::mana-produced', 'mana_to_combat'],
    'draw-a-card': ['draw_drain', 'altwin::draw-a-card', 'altwin::self-deplete'],
    'lifegain': ['gain_drain', 'altwin::lifegain'],
    # ADR 0025 resource split (A1): spell-cast is the CAST-ONLY node; cast_drain
    # (whenever-you-CAST drains: Aria of Flame, Aetherflux) + altwin::spell-cast
    # (Approach of the Second Sun — won by the SECOND CAST, a cast-only wincon) stay
    # here. spell-on-stack carries cast_drain too (magecraft drains on cast OR copy),
    # but NOT altwin::spell-cast (you cannot cast Approach twice by copying). spell-
    # copied has no win-edge (a bare copy closes nothing without a cast-or-copy payoff,
    # which it reaches via the union node spell-on-stack).
    'spell-cast': ['cast_drain', 'altwin::spell-cast'],
    'spell-copied': [],
    'spell-on-stack': ['cast_drain'],
    'creature-enters-battlefield': ['etb_drain'],
    'permanent-sacrificed': ['death_drain'],
    'permanent-dies': ['death_drain'],
    'recursion-from-graveyard': [],
    'token-created': ['etb_drain', 'altwin::token-created'],
    'untap': ['altwin::untap'],
    'counter-placed': ['altwin::counter-placed'],
    # ADR 0025 resource split (A2): the former generic `player-counter` node is gone,
    # replaced by energy/rad/experience. NONE of them carries a win-edge:
    #  * energy — infinite energy is not itself a win (Aetherworks-style energy combos
    #    close through a DIFFERENT event's payoff, not an energy wincon card), so [].
    #  * experience — infinite experience is not a win (the commander's +1/+1 or
    #    cost-reduction payoff is a different event), so [].
    #  * rad — self-mill, modeled as a self-deplete route in fid_route (not a win-edge).
    # altwin::player-counter (Azor's Elocutors / filibuster counters) is now UNMAPPED:
    # filibuster is neither energy/rad/experience, and the former node spuriously let
    # an infinite-energy/experience combo "win" via Azor's. It stays in card_edges.json
    # as data but no event references it — walk-inert, the same posture as a poison/
    # domain altwin with no fid route (a future filibuster producer-concept would re-map it).
    'energy': [],
    'rad': [],
    'experience': [],
    # Poison is an SBA loss for the opponent (CR 104.3d / 704.5c: 10 poison =
    # loss), so reaching the poisoned event is a WIN rule-edge (#74 §4.5). Mirrors
    # player-counter structurally. Walk-inert until a poison fid route exists
    # (fid 1800 "Gives Infect" -> None today); no payoff card maps to the class
    # because poison kills by rule, not by a "you win" card (see card_edges.json).
    'poisoned': ['altwin::poisoned'],
    # Domain is a five-color/all-basic-type board-state WIN (CR 104.2a; Coalition
    # Victory), so reaching the `domain` state-event is a WIN rule-edge (ADR 0024).
    # Mirrors poisoned structurally; walk-inert until a domain fid route exists
    # (none today). card_edges.json altwin::domain is [] — Coalition Victory / Call
    # the Spirit Dragons stay in _altwin_unmapped until a fid routes to the event
    # (same posture as poison fid 1800 -> None); the enabler->win link is the
    # strategy-graph event edge color-fixing -> domain -> five-color-win.
    'domain': ['altwin::domain'],
    'card-enters-graveyard': ['altwin::card-enters-graveyard', 'altwin::self-deplete'],
    'library-depleted': ['altwin::self-deplete'],
    'discard': [],
    'permanent-destroyed': [],
    'land-enters-battlefield': ['altwin::land-enters-battlefield'],
    'creature-attacks': [],
    'combat-damage-dealt': [],
    'permanent-leaves-battlefield': [],
    'creature-copied': [],
}

#: Self-deplete edge classes (self-draw/self-mill can reach these alt-wins).
SELFDEP_CLASSES: list[str] = ['altwin::self-deplete', 'altwin::card-enters-graveyard']

#: Edge classes whose hit means the walk reaches game-end with a WIN polarity.
#: These are the rule-edges + card-edges that terminate the walk.
_DRAIN_CLASSES = frozenset(
    {
        'mana_sink',
        'draw_drain',
        'etb_drain',
        'death_drain',
        'cast_drain',
        'gain_drain',
        'mana_to_combat',
    }
)
_ALTWIN_CLASSES = frozenset(
    {
        'altwin::mana-produced',
        'altwin::draw-a-card',
        'altwin::lifegain',
        'altwin::spell-cast',
        'altwin::token-created',
        'altwin::untap',
        'altwin::counter-placed',
        'altwin::player-counter',
        'altwin::poisoned',
        'altwin::domain',
        'altwin::card-enters-graveyard',
        'altwin::self-deplete',
        'altwin::land-enters-battlefield',
        'altwin::creature-copied',
    }
)

# Soft combat notes: bodies without haste (policy bit — deferred, not immediate).
_SOFT_COMBAT_NOTES = frozenset({'bodies-sans-haste', 'ctr-on-creature', 'face-down bodies'})

# ---------------------------------------------------------------------------
# Guard evaluation (scope + characteristic + infinity-dominance)
# ---------------------------------------------------------------------------

#: Cards with draw-to-drain that require OPPONENT draw (trigger_scope=opponent).
#: These MUST NOT promote on self-draw. Hand-curated from the validation.
_OPPONENT_DRAW_DRAINERS: frozenset[str] = frozenset(
    _norm(n)
    for n in (
        'Sheoldred, the Apocalypse',
        'Nekusar, the Mindrazer',
        'Underworld Dreams',
        'Fate Unraveler',
        'Ob Nixilis, the Hate-Twisted',
        'Orcish Bowmasters',
        'Seizan, Perverter of Truth',
    )
)

#: Cards with cast-to-drain that require a specific COLOR cast (guard=characteristic).
#: Aragorn the Uniter triggers on RED casts only.
_COLOR_GATED_CAST_DRAINERS: dict[str, str] = {
    _norm('Aragorn, the Uniter'): 'red',
}

#: Cards with death-to-drain that have "another" identity guard.
_ANOTHER_IDENTITY_DEATH_DRAINERS: frozenset[str] = frozenset(_norm(n) for n in ('Plague Belcher',))


def _guard_passes(
    edge_class: str,
    card_norm: str,
    source_event: str,
    fid_routes: list[dict],
) -> bool:
    """Evaluate the guard for a card edge hit.

    Returns True if the guard passes (the edge fires), False if it fails.
    Implements:
    - trigger_scope: self|opponent|any
    - characteristic: color/type guard
    - infinity-dominance: infinite stream satisfies ordinal gates

    The guard DEFINES the producer set (design-3 §3.3). A failed guard
    means this card edge does NOT fire for this combo.
    """
    # Opponent-draw drainers: only fire on OPPONENT draw, not self-draw.
    # Sheoldred/Nekusar/etc. trigger on opponent's draw step or forced-draw.
    # If the combo produces draw-a-card and it's NOT explicitly opponent-scoped
    # (table/target with "opponent" in name), the draw is self-draw and the
    # guard BLOCKS. Conservative: unscoped draw = self-draw (the combo draws
    # for its controller).
    if card_norm in _OPPONENT_DRAW_DRAINERS:
        if source_event == 'draw-a-card':
            # Check if ANY draw fid is explicitly opponent-scoped
            has_opponent_draw = False
            for r in fid_routes:
                if r.get('event') == 'draw-a-card':
                    scope = r.get('scope', 'unscoped')
                    name_lower = r.get('name', '').lower()
                    # Only fire if the draw is opponent-directed
                    if scope in ('table', 'target') and (
                        'opponent' in name_lower or 'each player' in name_lower or 'all player' in name_lower
                    ):
                        has_opponent_draw = True
            if not has_opponent_draw:
                return False
        return True

    # Color-gated cast drainers: Aragorn requires red casts
    if card_norm in _COLOR_GATED_CAST_DRAINERS:
        # The walk can't determine spell color from fids alone → guard=unresolved
        # But we conservatively do NOT promote (the guard fails)
        return False

    # "Another" identity guards: Plague Belcher on own death
    # Needs "another Zombie" to die — can't confirm from fids alone
    # Conservatively fail (deferred to per-card edge authoring)
    #
    # Infinity-dominance: ordinal/threshold gates fed by infinite stream → satisfied
    # (Helix Pinnacle, Door to Nothingness) — these are in altwin:: classes,
    # which inherently pass because they ARE the alt-win condition.
    # No additional guard needed for altwin:: edges.
    return card_norm not in _ANOTHER_IDENTITY_DEATH_DRAINERS


# ---------------------------------------------------------------------------
# fid → route classification
# ---------------------------------------------------------------------------

#: Events that reach game-end through RULE edges (not needing a deck card).
_RULE_TERMINAL_EVENTS: frozenset[str] = frozenset(
    {
        'game-end',  # direct
        'life-loss-drain',  # SBA 104.3b (unbounded)
        'library-depleted',  # CR 104.3c (opponent-scoped)
    }
)

#: Events that are topological sinks (never reach game-end).
_STATE_EVENTS: frozenset[str] = frozenset(
    {
        'lock',
        'cant-lose',
        'control-change',
    }
)


def fid_route(fid: int) -> dict[str, Any] | None:
    """Classify one fid into a walk route record.

    Returns a dict with: route, timing, event, scope, magnitude, risk, note,
    scope_self (bool).  Returns None if the fid is unknown.

    Route values: terminal-win, terminal-draw, terminal-loss-self, sba-drain,
    deckout-opp, self-deplete, combat, intermediate, state, inert, oneshot, meta.
    """
    m = load_adapter().get(fid)
    if m is None:
        return None

    event = m.event
    mag = m.magnitude
    scope = m.scope
    name_lower = m.name.lower()
    unbounded = mag in ('infinite', 'near-infinite')
    obj = m.object_descriptor or {}

    base = {
        'fid': fid,
        'name': m.name,
        'event': event,
        'magnitude': mag,
        'scope': scope,
        'scope_self': False,
    }

    # game-end: direct terminal
    if event == 'game-end':
        # Determine polarity from the fid
        if 'lose the game' in name_lower and ('you ' in name_lower or 'yourself' in name_lower):
            base.update(route='terminal-loss-self', timing='immediate', polarity='loss')
        elif 'draw' in name_lower and 'game' in name_lower:
            base.update(route='terminal-draw', timing='immediate', polarity='draw')
        else:
            deferred = bool(any(w in name_lower for w in ('upkeep', 'end step', 'next turn', 'beginning of')))
            base.update(route='terminal-win', timing='deferred' if deferred else 'immediate', polarity='win')
        return base

    # life-loss-drain: SBA 104.3b (unbounded damage/lifeloss at opponents)
    if event == 'life-loss-drain':
        if unbounded:
            # Check if it's self-directed
            if scope == 'self' or 'to you' in name_lower or 'you lose' in name_lower:
                base.update(route='self-harm', timing='immediate')
                return base
            base.update(route='sba-drain', timing='immediate')
            return base
        base.update(route='oneshot')
        return base

    # library-depleted: CR 104.3c (opponent deck-out)
    if event == 'library-depleted':
        # Self-directed = self-deplete risk
        if scope == 'self' or 'your library' in name_lower or 'your own' in name_lower:
            base.update(route='self-deplete', event='library-depleted', scope_self=True, risk='self-deplete')
            return base
        # Opponent-scoped library depletion is game-ending regardless of magnitude.
        # "Exile each opponent's library" is finite (one-shot) but lethal.
        is_full_depletion = (
            unbounded
            or 'entire' in name_lower
            or 'all' in name_lower
            or ('exile' in name_lower and ('opponent' in name_lower or 'each' in name_lower))
            or scope in ('table', 'target')
        )
        if is_full_depletion:
            base.update(route='deckout-opp', timing='deferred')
            return base
        base.update(route='oneshot')
        return base

    # State events: topological sinks
    if event in _STATE_EVENTS:
        base.update(route='state')
        return base

    # cant-lose: a standing condition, not a win
    if event == 'cant-lose':
        base.update(route='state')
        return base

    # extra-turn: policy cell — infinite turns = win (renamed from
    # monarch-or-extra-turn in the #74 §2 split; all 4 turn fids route here)
    if event == 'extra-turn':
        if unbounded:
            base.update(route='turn-monopoly', timing='deferred')
            return base
        base.update(route='oneshot')
        return base

    # combat-damage-dealt: direct combat — magnitude-guarded like every other
    # route (ADR 0016 / win-walk Item 1). Finite combat effects are defensive
    # prevention/tax ("opponents can't attack", "you can't be attacked"), NOT
    # wins; only an unbounded combat source closes the game.
    if event == 'combat-damage-dealt':
        if unbounded:
            base.update(route='combat', timing='immediate')
            return base
        base.update(route='oneshot')
        return base

    # creature-attacks: direct combat (magnitude-guarded — see above)
    if event == 'creature-attacks':
        if unbounded:
            base.update(route='combat', timing='immediate')
            return base
        base.update(route='oneshot')
        return base

    # Token/creature production: combat path
    if event == 'token-created' and unbounded:
        keywords = obj.get('keywords') or []
        if 'haste' in keywords or 'haste' in name_lower or 'attacking' in name_lower:
            base.update(route='combat', timing='immediate', event='token-created')
        else:
            base.update(route='combat', timing='deferred', event='token-created', note='bodies-sans-haste')
        return base
    if event == 'token-created':
        base.update(route='oneshot')
        return base

    # creature-enters-battlefield: INTERMEDIATE (an event that card edges can
    # convert, e.g. Impact Tremors etb_drain).  It also enables combat bodies,
    # but that's an action-closure the priority ladder handles AFTER card-edge
    # checks.  (Tokens are classified separately via token-created.)
    if event == 'creature-enters-battlefield' and unbounded:
        base.update(route='intermediate', event='creature-enters-battlefield')
        return base

    # counter-placed: +1/+1 on creatures = combat bodies if unbounded
    if event == 'counter-placed' and unbounded:
        if '+1/+1' in name_lower and 'creature' in name_lower:
            base.update(route='combat', timing='deferred', note='ctr-on-creature')
            return base
        base.update(route='intermediate', event='counter-placed')
        return base

    # mana-produced: intermediate (needs a card edge to close)
    if event == 'mana-produced':
        if unbounded:
            base.update(route='intermediate', event='mana-produced')
        else:
            base.update(route='oneshot')
        return base

    # draw-a-card: intermediate with self-deplete risk
    if event == 'draw-a-card':
        is_self = scope == 'self' or 'you draw' in name_lower or 'self' in name_lower
        if is_self:
            base['scope_self'] = True
        if unbounded:
            base.update(route='intermediate', event='draw-a-card', risk='self-deplete')
        else:
            base.update(route='oneshot')
        return base

    # rad (radiation): self-deplete risk — rad counters mill YOUR OWN library on
    # upkeep (CR self-deplete). ADR 0025 resource split (A2): the former generic
    # player-counter 'rad' sub-rule is now keyed on the event itself.
    if event == 'rad':
        base.update(route='self-deplete', event='card-enters-graveyard', risk='self-deplete', note='RAD-RULE')
        return base

    # energy / experience (player resource counters, ADR 0025 A2): intermediate if
    # unbounded (no win-edge in EVENT_EDGE_CLASSES, so an infinite stream closes
    # nothing by itself), oneshot otherwise. Replaces the old player-counter ->
    # altwin::player-counter (Azor's filibuster) path, which spuriously let infinite
    # energy/experience "win" through an unrelated filibuster card.
    if event in ('energy', 'experience'):
        if unbounded:
            base.update(route='intermediate', event=event)
        else:
            base.update(route='oneshot')
        return base

    # All other events: intermediate if unbounded, oneshot otherwise
    if event is None:
        base.update(route='inert')
        return base

    if unbounded:
        base.update(route='intermediate', event=event)
    else:
        base.update(route='oneshot')
    return base


# ---------------------------------------------------------------------------
# The closure walk
# ---------------------------------------------------------------------------


def combo_win_verdict(
    *,
    fids: list[int],
    deck_card_names: set[str],
    card_edges: dict[str, frozenset[str]] | None = None,
) -> dict[str, Any]:
    """Compute the closure walk for one combo in one deck.

    Parameters
    ----------
    fids
        The feature IDs this combo produces (from ``Combo.produces[].fid``).
    deck_card_names
        Normalized (casefold + space-collapsed) names of all cards in the deck.
    card_edges
        Optional override for card edge classes (for testing). Defaults to
        the production card edges data.

    Returns
    -------
    dict with keys: kind, timing, polarity, via, detail (optional), mechs (dict).
    kind: rule | action | card | card-needed | unclosable | self-loss | draw | none
    """
    if card_edges is None:
        card_edges = _load_card_edges()

    cards_norm = frozenset(_norm(n) for n in deck_card_names)

    # 1. Classify each fid into a route record
    routes: list[dict] = []
    for fid in fids:
        r = fid_route(fid)
        if r is not None:
            routes.append(r)

    if not routes:
        return _verdict('none', 'n/a', 'none', 'no-fids')

    # 2. Build the mechanisms view
    def have(rt: str, tm: str | None = None) -> bool:
        return any(r['route'] == rt and (tm is None or r.get('timing') == tm) for r in routes)

    events: set[str] = set()
    for r in routes:
        if r['route'] == 'intermediate' and r.get('event'):
            events.add(r['event'])

    selfdep = have('self-deplete') or any(r.get('risk') == 'self-deplete' for r in routes)

    # All events (including non-intermediate) for the mechanisms lens
    all_events: set[str] = {e for r in routes if isinstance((e := r.get('event')), str)}

    # Card edge hits from intermediate events
    hits = _card_edge_hits(events, selfdep, cards_norm, card_edges, routes)

    # All card edge hits (lens material)
    mech_hits = _card_edge_hits(all_events, selfdep, cards_norm, card_edges, routes)

    mechs = {
        'rule_imm': have('terminal-win', 'immediate') or have('sba-drain'),
        'rule_def': have('terminal-win', 'deferred') or have('deckout-opp'),
        'action_imm': have('combat', 'immediate'),
        'action_def': have('combat', 'deferred') or have('turn-monopoly'),
        'card_member': bool(mech_hits),
        'card_potential': sorted(all_events & set(EVENT_EDGE_CLASSES)),
        'state': have('state'),
        'selfdep': selfdep,
    }

    # 3. Priority ladder (same as prototype closurelib.closure)
    if have('terminal-win', 'immediate'):
        return _verdict('rule', 'immediate', 'win', 'literal-win-fid', mechs=mechs)
    if have('sba-drain'):
        return _verdict('rule', 'immediate', 'win', 'sba-life', mechs=mechs)

    # Card-closed via intermediate events
    if hits:
        ev, cls, card = hits[0]
        return _verdict('card', 'immediate', 'win', f'{cls}<-{ev}', detail=card, mechs=mechs)

    if have('terminal-win', 'deferred'):
        return _verdict('rule', 'deferred', 'win', 'literal-win-deferred', mechs=mechs)
    if have('deckout-opp'):
        return _verdict('rule', 'deferred', 'win', 'deck-out-opp', mechs=mechs)

    # RAD rule (6th rule edge)
    rad_routes = [r for r in routes if r.get('note') == 'RAD-RULE']
    if rad_routes:
        return _verdict('rule', 'deferred', 'win', 'RAD-RULE', mechs=mechs)

    if have('combat', 'immediate'):
        return _verdict('action', 'immediate', 'win', 'combat', mechs=mechs)
    if have('combat', 'deferred'):
        return _verdict('action', 'deferred', 'win', 'combat-deferred', mechs=mechs)
    if have('turn-monopoly'):
        return _verdict('action', 'deferred', 'win', 'turn-monopoly', mechs=mechs)

    # Card-needed: has intermediate events but no card edge fires
    if events or selfdep:
        pol = 'loss-self-risk' if (selfdep and not events) else 'none-yet'
        return _verdict('card-needed', 'n/a', pol, ','.join(sorted(e for e in events)) or 'self-deplete', mechs=mechs)

    # Terminal draw
    if have('terminal-draw'):
        return _verdict('rule', 'immediate', 'draw', 'literal-draw-fid', mechs=mechs)

    # Self-loss
    if have('terminal-loss-self') or have('self-harm'):
        return _verdict('rule', 'deferred', 'loss-self', 'self-loss', mechs=mechs)

    # State (lock, cant-lose, etc.)
    if have('state'):
        return _verdict('unclosable', 'never', 'none', 'state', mechs=mechs)

    return _verdict('none', 'n/a', 'none', 'finite-oneshot-only', mechs=mechs)


def _verdict(
    kind: str,
    timing: str,
    polarity: str,
    via: str,
    *,
    detail: str | None = None,
    mechs: dict | None = None,
) -> dict[str, Any]:
    """Build a closure verdict dict."""
    d: dict[str, Any] = {
        'kind': kind,
        'timing': timing,
        'polarity': polarity,
        'via': via,
    }
    if detail is not None:
        d['detail'] = detail
    if mechs is not None:
        d['mechs'] = mechs
    return d


def _card_edge_hits(
    events: set[str],
    selfdep: bool,
    cards_norm: frozenset[str],
    edges: dict[str, frozenset[str]],
    fid_routes: list[dict],
) -> list[tuple[str, str, str]]:
    """All (event, edge_class, card) triples available from deck cards.

    Applies guard evaluation: a hit is only returned if the guard passes.
    """
    hits: list[tuple[str, str, str]] = []
    for ev in events:
        for cls in EVENT_EDGE_CLASSES.get(ev, []):
            edge_cards = edges.get(cls, frozenset())
            for c in sorted(cards_norm & edge_cards):
                if _guard_passes(cls, c, ev, fid_routes):
                    hits.append((ev, cls, c))
    if selfdep:
        for cls in SELFDEP_CLASSES:
            edge_cards = edges.get(cls, frozenset())
            for c in sorted(cards_norm & edge_cards):
                if _guard_passes(cls, c, 'self-deplete', fid_routes):
                    hits.append(('self-deplete', cls, c))
    return hits


# ---------------------------------------------------------------------------
# Public verdict helpers
# ---------------------------------------------------------------------------


def walk_is_win(verdict: dict) -> bool:
    """Does the walk verdict say this combo is a win?"""
    return verdict['polarity'] == 'win'


def walk_win_kind(verdict: dict) -> str:
    """Classify the walk verdict into a win_kind label for facts output.

    Returns 'rule', 'action', 'card', or 'non-win'.
    """
    if not walk_is_win(verdict):
        return 'non-win'
    return verdict.get('kind', 'rule')


# ---------------------------------------------------------------------------
# Name-to-fid resolution (for combos that lack fids in the lake)
# ---------------------------------------------------------------------------


@cache
def _name_to_fid_map() -> dict[str, int]:
    """Build a case-insensitive feature-name → fid mapping."""
    return {m.name.strip().lower(): fid for fid, m in load_adapter().items()}


def resolve_fids_from_feature_names(feature_names: list[str]) -> list[int]:
    """Resolve feature names to fids via the adapter.

    Returns a list of fids (0 for unresolved names).
    """
    m = _name_to_fid_map()
    return [m.get(n.strip().lower(), 0) for n in feature_names]
