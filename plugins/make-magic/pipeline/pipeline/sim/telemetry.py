"""Tier-1 numerical telemetry from a captured Forge verbose game log (pure parse).

Phase 2 (:mod:`pipeline.sim.runner`) captured the whole verbose stdout+stderr in
``MatchResult.raw_log``; this module turns that text into aggregatable numbers
WITHOUT re-running Forge. Every field of :class:`GameFeatures` is numeric or a
small enum-like string so a batch of games reduces to statistics (means, ramp
curves, kill-turn distributions).

The line formats below are verified against the real captured fixtures in
``tests/fixtures/forge/`` (``sim2.log`` is the ONE fully-verbose game):

  * ``Turn: Turn N (Ai(k)-Deck)``     — the authoritative game turn counter.
  * ``Mulligan: Ai(k)-Deck has mulliganed down to M cards.``
  * ``Land: Ai(k)-Deck played <land>``
  * ``Life: Life: Ai(k)-Deck L1 > L2``
  * ``Damage: <source> deals N [combat ]damage to Ai(k)-Deck.``
  * ``Game Result: Game N ended in <ms> ms. Ai(k)-Deck has won!`` (or ``Draw!``)
  * ``Game Outcome: Ai(k)-Deck has lost trying to draw cards from empty library``
    — the empty-library (mill) loss signal (compact logs carry only Outcome
    lines, no per-turn detail).

Robustness is a hard contract: a malformed / truncated / empty log NEVER raises.
Any field that cannot be derived is ``None`` (scalars) or ``[]`` (curves), and
the winner defaults to ``'draw'`` when no result line is present.

Slot mapping mirrors the runner: ``Ai(1)`` = ``deck_a`` = ``'a'``, ``Ai(2)`` =
``deck_b`` = ``'b'``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = (
    'GameFeatures',
    'extract_game_features',
    'extract_match_features',
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
_MILL_OUTCOME_RE = re.compile(
    r'^Game Outcome: Ai\((\d)\)-\S.*? has lost trying to draw cards from empty library'
)


def _slot_to_side(slot: str) -> str:
    """Map a Forge ``Ai(k)`` slot digit to a side: ``'1' -> 'a'``, else ``'b'``."""
    return 'a' if slot == '1' else 'b'


@dataclass(frozen=True)
class GameFeatures:
    """Tier-1 numerical features for ONE game — every field aggregates cleanly.

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


def extract_game_features(game_log: str, *, deck_a: str, deck_b: str) -> GameFeatures:
    """Parse ONE game's verbose log into :class:`GameFeatures` (pure, never raises).

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
    """
    del deck_a, deck_b  # slot-keyed; names documented for the caller's benefit.

    start_life = (
        _COMMANDER_START_LIFE if re.search(r'of Commander\b', game_log) else _DEFAULT_START_LIFE
    )

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


def extract_match_features(
    match_log: str, *, deck_a: str, deck_b: str
) -> list[GameFeatures]:
    """Extract per-game :class:`GameFeatures` for every game in a multi-game log.

    Splits with :func:`split_games`, then extracts each segment independently.
    Returns ``[]`` for empty / result-less input (never raises).
    """
    return [
        extract_game_features(seg, deck_a=deck_a, deck_b=deck_b)
        for seg in split_games(match_log)
    ]
