"""Numerical telemetry from a captured Forge verbose game log (pure parse).

:mod:`pipeline.sim.runner` captures the whole verbose stdout+stderr in
``MatchResult.raw_log``; this module turns that text into aggregatable numbers
without re-running Forge. Every field of :class:`GameFeatures` is numeric or a
small enum-like string so a batch of games reduces to statistics (means, ramp
curves, kill-turn distributions).

The line formats below are verified against the real captured fixtures in
``tests/fixtures/forge/`` (``sim2.log`` is the one fully-verbose game):

  * ``Turn: Turn N (Ai(k)-Deck)``     — the authoritative game turn counter.
  * ``Mulligan: Ai(k)-Deck has mulliganed down to M cards.``
  * ``Land: Ai(k)-Deck played <land>``
  * ``Life: Life: Ai(k)-Deck L1 > L2``
  * ``Damage: <source> deals N [combat ]damage to Ai(k)-Deck.``
  * ``Game Result: Game N ended in <ms> ms. Ai(k)-Deck has won!`` (or ``Draw!``)
  * ``Game Outcome: Ai(k)-Deck has lost trying to draw cards from empty library``
    — the empty-library (mill) loss signal (compact logs carry only Outcome
    lines, no per-turn detail).

Robustness is a hard contract: a malformed / truncated / empty log never raises.
Any field that cannot be derived is ``None`` (scalars) or ``[]`` (curves), and
the winner defaults to ``'draw'`` when no result line is present.

Slot mapping mirrors the runner: ``Ai(1)`` = ``deck_a`` = ``'a'``, ``Ai(2)`` =
``deck_b`` = ``'b'``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Set as AbstractSet

__all__ = (
    'GameFeatures',
    'PilotingProfile',
    'extract_game_features',
    'extract_match_features',
    'extract_piloting',
    'split_games',
)

#: Constructed starting life; Commander is 40 (detected from the match header).
_DEFAULT_START_LIFE = 20
_COMMANDER_START_LIFE = 40

#: ``Turn: Turn N (Ai(k)-Deck)`` — group 1 = turn number, group 2 = slot.
_TURN_RE = re.compile(r'^Turn: Turn (\d+) \(Ai\((\d)\)-')
#: ``Mulligan: Ai(k)-Deck has mulliganed down to M cards.`` — slot, kept size.
_MULLIGAN_RE = re.compile(r'^Mulligan: Ai\((\d)\)-\S.*? has mulliganed down to (\d+) cards')
#: ``Land: Ai(k)-Deck played <land>`` — slot of the player who played a land.
_LAND_RE = re.compile(r'^Land: Ai\((\d)\)-\S.*? played ')
#: ``Life: Life: Ai(k)-Deck L1 > L2`` — slot, before, after.
_LIFE_RE = re.compile(r'^Life: Life: Ai\((\d)\)-\S.*? (\d+) > (-?\d+)')
#: ``Damage: <source> deals N [combat ]damage to Ai(k)-Deck.`` — combat flag + slot.
_DAMAGE_RE = re.compile(r'^Damage: .*? deals \d+ (combat )?damage to Ai\((\d)\)-')
#: ``Game Result: Game N ended in <ms> ms. <tail>`` — elapsed + winner tail.
_RESULT_RE = re.compile(r'^Game Result: Game \d+ ended in (\d+) ms\. (.+)$')
#: Winner tail within a Game Result line: ``Ai(k)-Deck has won!``. Name matched
#: non-greedily (``.+?``) so spaced/paren deck names parse (only the slot matters).
_WINNER_RE = re.compile(r'Ai\((\d)\)-.+? has won!')
#: The empty-library (mill) loss on a Game Outcome line — slot of the milled loser.
_MILL_OUTCOME_RE = re.compile(r'^Game Outcome: Ai\((\d)\)-\S.*? has lost trying to draw cards from empty library')


def _slot_to_side(slot: str) -> str:
    """Map a Forge ``Ai(k)`` slot digit to a side: ``'1' -> 'a'``, else ``'b'``."""
    return 'a' if slot == '1' else 'b'


@dataclass(frozen=True)
class GameFeatures:
    """Numerical features for one game — every field aggregates cleanly.

    ``winner`` is ``'a'`` / ``'b'`` / ``'draw'``. Scalars that could not be parsed
    are ``None``; the per-turn ramp curves are ``[]`` when no ``Turn:`` boundaries
    were seen. ``win_margin_life`` is the winner's remaining life at game end.
    ``wincon`` is ``'combat'`` / ``'burn'`` / ``'mill'`` / ``'other'`` (``None``
    when no game end was parsed).
    """

    winner: str
    kill_turn: int | None
    win_margin_life: int | None
    wincon: str | None
    mulligans_a: int
    mulligans_b: int
    game_length_ms: int | None
    lands_by_turn_a: list[int] = field(default_factory=list)
    lands_by_turn_b: list[int] = field(default_factory=list)


def split_games(match_log: str) -> list[str]:
    """Split a multi-game verbose log into one text segment per game.

    Each finished game prints exactly one ``Game Result: Game N ended …`` line as
    its terminator, so every segment is the run of lines up to and including its
    ``Game Result``. Preamble before the first result (card-DB load, headers) is
    folded into game 1. Trailing text after the last ``Game Result`` (no
    terminator) is dropped — an unfinished game has no result to attribute.

    Returns ``[]`` for empty / result-less input (never raises).
    """
    if not match_log:
        return []

    segments: list[str] = []
    current: list[str] = []
    for line in match_log.splitlines():
        current.append(line)
        if _RESULT_RE.match(line.strip()):
            segments.append('\n'.join(current))
            current = []
    return segments


def extract_game_features(game_log: str, *, deck_a: str, deck_b: str, commander: bool | None = None) -> GameFeatures:
    """Parse one game's verbose log into :class:`GameFeatures` (pure, never raises).

    ``deck_a`` / ``deck_b`` are accepted for symmetry with the runner API and to
    document the slot mapping; the parse keys off the ``Ai(1)`` / ``Ai(2)`` slots
    (``Ai(1)`` = ``deck_a``), not the names, so renamed decks parse identically.

    Derivation:
      * turn counter from ``Turn:`` lines;
      * cumulative lands snapshotted per player at each turn boundary;
      * per-player life from ``Life:`` deltas, seeded from the format's starting
        life so an untouched winner still reports a concrete ``win_margin_life``;
      * ``kill_turn`` = the tracked turn at which a player first hit <= 0 life, or
        the last observed turn if a game end is present without a life-0 line;
      * ``wincon`` classified from the killing ``Damage:`` line (combat vs burn),
        an empty-library ``Game Outcome`` (mill), else ``'other'``.

    ``commander`` selects the 40-life start; ``None`` (a standalone single-game
    parse) detects it from this log's text. :func:`extract_match_features` passes
    it explicitly because the ``… games of Commander`` header prints once in the
    match preamble — which :func:`split_games` folds into game 1 only, so
    per-segment detection would silently seed games 2+ with 20 life.
    """
    del deck_a, deck_b  # slot-keyed; names documented for the caller's benefit.

    if commander is None:
        commander = re.search(r'of Commander\b', game_log) is not None
    start_life = _COMMANDER_START_LIFE if commander else _DEFAULT_START_LIFE

    current_turn: int | None = None
    lands = {'a': 0, 'b': 0}
    life = {'a': start_life, 'b': start_life}
    life_seen = {'a': False, 'b': False}
    mulligans = {'a': 0, 'b': 0}
    lands_by_turn: dict[str, list[int]] = {'a': [], 'b': []}

    winner = 'draw'
    game_length_ms: int | None = None
    kill_turn: int | None = None
    kill_wincon: str | None = None
    mill_loser: str | None = None
    # The most recent Damage: line's (side_hit, is_combat) — the potential killer.
    last_damage: tuple[str, bool] | None = None

    def snapshot() -> None:
        lands_by_turn['a'].append(lands['a'])
        lands_by_turn['b'].append(lands['b'])

    for raw in game_log.splitlines():
        line = raw.strip()

        m = _TURN_RE.match(line)
        if m:
            # Close out the turn we were in before advancing the counter.
            if current_turn is not None:
                snapshot()
            current_turn = int(m.group(1))
            continue

        m = _MULLIGAN_RE.match(line)
        if m:
            side = _slot_to_side(m.group(1))
            kept = int(m.group(2))
            mulligans[side] = max(mulligans[side], 7 - kept)
            continue

        m = _LAND_RE.match(line)
        if m:
            lands[_slot_to_side(m.group(1))] += 1
            continue

        m = _LIFE_RE.match(line)
        if m:
            side = _slot_to_side(m.group(1))
            after = int(m.group(3))
            life[side] = after
            life_seen[side] = True
            if after <= 0 and kill_turn is None:
                kill_turn = current_turn
                # Classify from the most recent damage that plausibly caused it.
                if last_damage is not None and last_damage[0] == side:
                    kill_wincon = 'combat' if last_damage[1] else 'burn'
                else:
                    kill_wincon = 'other'
            continue

        m = _DAMAGE_RE.match(line)
        if m:
            is_combat = m.group(1) is not None
            last_damage = (_slot_to_side(m.group(2)), is_combat)
            continue

        m = _MILL_OUTCOME_RE.match(line)
        if m:
            mill_loser = _slot_to_side(m.group(1))
            continue

        m = _RESULT_RE.match(line)
        if m:
            game_length_ms = int(m.group(1))
            wm = _WINNER_RE.search(m.group(2))
            if wm:
                winner = _slot_to_side(wm.group(1))
            elif 'Draw' in m.group(2):
                winner = 'draw'
            continue

    # Close out the final in-progress turn (no trailing Turn: line follows it).
    if current_turn is not None:
        snapshot()

    ended = game_length_ms is not None
    if kill_turn is None and ended:
        kill_turn = current_turn

    # wincon precedence: a decided game with a life-0 killing blow uses that
    # classification; an empty-library loss is mill; any other decided game is
    # 'other'; an undecided/garbage log leaves it None.
    if kill_wincon is not None:
        wincon = kill_wincon
    elif mill_loser is not None:
        wincon = 'mill'
    elif ended and winner != 'draw':
        wincon = 'other'
    else:
        wincon = None

    win_margin_life: int | None = None
    # Report the winner's remaining life only for a decided game with a life
    # baseline: the winner appeared in a Life line, or we saw per-turn detail at
    # all (the `winner in life` guard also keeps a 'draw' out of the life dicts).
    saw_turns = bool(lands_by_turn['a'] or lands_by_turn['b'])
    if ended and winner in life and (life_seen[winner] or saw_turns):
        win_margin_life = life[winner]

    return GameFeatures(
        winner=winner,
        kill_turn=kill_turn,
        win_margin_life=win_margin_life,
        wincon=wincon,
        mulligans_a=mulligans['a'],
        mulligans_b=mulligans['b'],
        game_length_ms=game_length_ms,
        lands_by_turn_a=lands_by_turn['a'],
        lands_by_turn_b=lands_by_turn['b'],
    )


def extract_match_features(match_log: str, *, deck_a: str, deck_b: str) -> list[GameFeatures]:
    """Extract per-game :class:`GameFeatures` for every game in a multi-game log.

    Splits with :func:`split_games`, then extracts each segment independently.
    The Commander flag is detected once over the whole match log (the header
    prints only in the preamble, which lands in game 1's segment) and applied to
    every game. Returns ``[]`` for empty / result-less input (never raises).
    """
    commander = re.search(r'of Commander\b', match_log) is not None
    return [
        extract_game_features(seg, deck_a=deck_a, deck_b=deck_b, commander=commander) for seg in split_games(match_log)
    ]


# --------------------------------------------------------------------------- #
# Piloting telemetry — HANDLOG opportunity-conditioning (the deck-vs-pilot metric)
# --------------------------------------------------------------------------- #
#
# Ported faithfully from the lab reference
# ``~/mtg-sim-lab/variant_gauntlet/analyze_forge_variant.py`` (``parse_game`` /
# ``analyze_game`` / ``wilson``). That analyzer answers the question this feature
# exists to answer: when a deck under-performs, is it the DECK or the AI failing
# to PILOT it? It conditions on *opportunity* — a counter/removal spell only
# "should" fire on a turn where the candidate actually holds an affordable answer
# with a legal target — and reports the conversion rate over those turns.
#
# Divergences from the reference, all faithful to the model (see task 1.4 spec):
#   * Classification (which cards are counters / removal / interaction / lands and
#     each card's mv) is passed IN, not derived from a card-facts file. The engine
#     supplies it from the deck's otag buckets (task 1.6); this module stays
#     classification-agnostic. The affordability proxy is unchanged:
#     ``untapped_lands >= card_mv`` (the reference's documented count-based model).
#   * Self-target rate is NOT derivable from HANDLOG — ``cast`` lines carry
#     ``source=`` but no target — so it is OMITTED here (the reference derived it
#     from a different signal). We do not fabricate it.
#   * ``wilson_ci`` is reused from :mod:`pipeline.sim.core` (imported lazily to
#     avoid the core<->telemetry import cycle) instead of the reference's inline
#     ``wilson``; the score formula is identical.

#: ``Ai(1)`` is the candidate (slot ``'a'``); ``Ai(2)`` the opponent. HANDLOG's
#: ``UR_*`` fields ALWAYS report the candidate's private state (hand / untapped
#: lands / opp creature count), regardless of whose turn it is.
_HANDLOG_RE = re.compile(
    r'^HANDLOG turn=(\d+) event=(\w+)'
    r'(?: kind=(\w+))?'
    r'(?: active=(.*?))?(?: castBy=(.*?) source=(.*?))?'
    r' UR_hand=\[(.*?)\] UR_untapped_lands=(\d+)'
    r' opp_creatures=(\d+) UR_life=(-?\d+) opp_life=(-?\d+)$'
)


@dataclass(frozen=True)
class _HandlogEvent:
    """One parsed HANDLOG line (candidate-centric view)."""

    turn: int
    ev: str
    kind: str | None
    active: str | None
    cast_by: str | None
    source: str | None
    hand: tuple[str, ...]
    untapped: int
    opp_creatures: int


@dataclass(frozen=True)
class PilotingProfile:
    """Opportunity-conditioned piloting metrics pooled across a whole match.

    Each ``*_opps`` is a count of decision points where the candidate (slot
    ``'a'`` / ``Ai(1)``) *could* have interacted — held an affordable answer with
    a legal target — and each ``*_casts`` is how often it actually did. The
    ``*_fire`` rate and ``*_ci`` Wilson interval are ``None`` when there were no
    opportunities (denominator 0): no opportunities, no information.

    * **Counter**: an opportunity is an opponent (``Ai(2)``) casting a spell on a
      turn the candidate holds an affordable counter; conversion = the candidate
      casts a counter in *immediate* response (before the next ``turnstart``, and
      as its first spell — the reference's "strict" rule).
    * **Removal**: an opportunity is a turn the candidate holds an affordable
      removal/burn card AND the opponent has >= 1 creature to target; conversion =
      the candidate casts removal that turn. (Own-turn affordability gets the
      reference's ``eff`` +1-land tweak when a land is in hand.)
    * **Stranded interaction**: interaction cards still in the candidate's hand at
      ``event=gameend``, averaged per game.
    """

    counter_opps: int
    counter_casts: int
    counter_fire: float | None
    counter_ci: tuple[float, float] | None
    removal_opps: int
    removal_casts: int
    removal_fire: float | None
    removal_ci: tuple[float, float] | None
    interaction_stranded_per_game: float
    games: int


def _split_handlog_games(match_log: str) -> list[list[_HandlogEvent]]:
    """Split a HANDLOG stream into per-game event lists on the ``gameend`` marker.

    Mirrors :func:`split_games`: each finished game ends with exactly one
    ``event=gameend`` HANDLOG line, so a game is the run of parsed events up to
    and including it. Non-HANDLOG and malformed lines are skipped. Trailing events
    after the last ``gameend`` (an unterminated game) are dropped. Returns ``[]``
    for empty / marker-less input (never raises).
    """
    games: list[list[_HandlogEvent]] = []
    current: list[_HandlogEvent] = []
    for raw in match_log.splitlines():
        m = _HANDLOG_RE.match(raw.strip())
        if m is None:
            continue
        turn, ev, kind, active, cast_by, source, hand, unt, oppc, _ulife, _olife = m.groups()
        current.append(
            _HandlogEvent(
                turn=int(turn),
                ev=ev,
                kind=kind,
                active=active,
                cast_by=cast_by,
                source=source,
                hand=tuple(c for c in hand.split(';') if c),
                untapped=int(unt),
                opp_creatures=int(oppc),
            )
        )
        if ev == 'gameend':
            games.append(current)
            current = []
    return games


def _candidate_prefix(slot: str) -> str:
    """The ``Ai(k)`` prefix for a candidate slot (``'a'`` -> ``'Ai(1)'``)."""
    return 'Ai(1)' if slot == 'a' else 'Ai(2)'


def extract_piloting(
    match_log: str,
    *,
    candidate_slot: str = 'a',
    counters: AbstractSet[str],
    removal: AbstractSet[str],
    interaction: AbstractSet[str],
    costs: Mapping[str, int],
    lands: AbstractSet[str],
) -> PilotingProfile:
    """Opportunity-conditioned piloting profile from a HANDLOG stream (pure).

    Ports ``analyze_game`` from the lab reference (cited above). The classification
    is passed in and this function is classification-agnostic:

    * ``counters`` / ``removal`` / ``interaction`` / ``lands`` are ``set[str]`` of
      card names (``interaction`` is typically ``counters | removal``, but is taken
      as given — stranded counts anything in it).
    * ``costs`` maps a card name to its mana value for the affordability proxy
      ``untapped_lands >= mv``. A card missing from ``costs`` is treated as
      unaffordable (defensive; the reference used ``99``).
    * ``candidate_slot`` is the piloted deck's slot (``'a'`` = ``Ai(1)``).

    Robustness is a hard contract: empty / truncated / garbage input yields a
    fully-zeroed profile and NEVER raises. The Wilson ``*_ci`` and ``*_fire`` rate
    are ``None`` whenever the corresponding ``*_opps`` denominator is 0.
    """
    from pipeline.sim.core import wilson_ci  # lazy: avoid core<->telemetry cycle.

    cand = _candidate_prefix(candidate_slot)

    def affordable(card: str, untapped: int) -> bool:
        return untapped >= costs.get(card, 99)

    counter_opps = 0
    counter_casts = 0
    removal_opps = 0
    removal_casts = 0
    stranded_total = 0

    games = _split_handlog_games(match_log)

    for ev in games:
        # --- Counter opportunities: opponent casts, candidate holds an answer. ---
        for i, e in enumerate(ev):
            if e.ev != 'cast' or e.kind != 'spell' or not e.cast_by:
                continue
            if e.cast_by.startswith(cand):
                continue  # candidate's own cast, not a counter opportunity.
            afford = [c for c in counters if c in e.hand and affordable(c, e.untapped)]
            if not afford:
                continue
            counter_opps += 1
            # Strict conversion: the candidate's FIRST spell before the next
            # turnstart is a counter (the reference's `strict_open` rule).
            converted = False
            strict_open = True
            for e2 in ev[i + 1 :]:
                if e2.ev == 'turnstart':
                    break
                if e2.ev == 'cast' and e2.kind == 'spell' and e2.cast_by:
                    is_counter = e2.cast_by.startswith(cand) and (e2.source in counters)
                    if is_counter and strict_open:
                        converted = True
                    strict_open = False
            counter_casts += converted

        # --- Removal opportunities: per turn with an affordable answer + a target. ---
        turns: dict[int, list[_HandlogEvent]] = {}
        for e in ev:
            turns.setdefault(e.turn, []).append(e)
        for _t, tev in sorted(turns.items()):
            ts = next((e for e in tev if e.ev == 'turnstart'), None)
            if ts is None:
                continue
            a_active = bool(ts.active) and ts.active.startswith(cand)
            # On the candidate's own turn it can still make a land drop for +1 mana.
            eff = ts.untapped + (1 if a_active and any(c in lands for c in ts.hand) else 0)
            afford = [c for c in removal if c in ts.hand and eff >= costs.get(c, 99)]
            if afford and ts.opp_creatures >= 1:
                removal_opps += 1
                cast = any(
                    e.ev == 'cast' and e.cast_by and e.cast_by.startswith(cand) and (e.source in removal) for e in tev
                )
                removal_casts += cast

        # --- Stranded interaction: interaction cards left in hand at gameend. ---
        end = next((e for e in reversed(ev) if e.ev == 'gameend'), None)
        if end is not None:
            stranded_total += sum(1 for c in end.hand if c in interaction)

    n_games = len(games)
    counter_fire = counter_casts / counter_opps if counter_opps else None
    counter_ci = wilson_ci(counter_casts, counter_opps) if counter_opps else None
    removal_fire = removal_casts / removal_opps if removal_opps else None
    removal_ci = wilson_ci(removal_casts, removal_opps) if removal_opps else None
    stranded_per_game = stranded_total / n_games if n_games else 0.0

    return PilotingProfile(
        counter_opps=counter_opps,
        counter_casts=counter_casts,
        counter_fire=counter_fire,
        counter_ci=counter_ci,
        removal_opps=removal_opps,
        removal_casts=removal_casts,
        removal_fire=removal_fire,
        removal_ci=removal_ci,
        interaction_stranded_per_game=stranded_per_game,
        games=n_games,
    )
