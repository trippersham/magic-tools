"""Parse an XMage per-game TRANSCRIPT into structured metrics + persist them.

This is the single source that turns a Phase-2 live-flushed transcript (the
per-game side-channel log the persistent worker writes as a game runs) into a
:class:`GameFeatures` row in the sim store. It is PURE PYTHON — it never touches
the JVM; it only reads the text the harness emits.

The P4->P2 marker contract
==========================
Every field is grounded in a marker that ALREADY appears in
``mage/collectors/MakeMagicHooks.java``, ``org/makemagic/xmage/XMageBatch.java``,
or the driver seam patches. Phase 2's live-flush MUST emit EXACTLY these lines
(they are the union of the batch harness's stdout + the driver seam's stderr,
interleaved into one transcript stream) for the parser to work:

* Turn boundary (``MakeMagicHooks.onGameLog``)::

      Turn: Turn <N> (Ai(<k>)-<name>)

* Turn-start / cast / game-end snapshots (``MakeMagicHooks``)::

      HANDLOG turn=<N> event=turnstart active=Ai(<k>)-<name> UR_hand=[...] \
          UR_untapped_lands=<u> UR_total_lands=<t> opp_creatures=<c> \
          UR_life=<l> opp_life=<o>
      HANDLOG turn=<N> event=cast kind=spell castBy=Ai(<k>)-<name> source=<card> <snapshot>
      HANDLOG turn=<N> event=gameend <snapshot>

* Life / land / damage (``MakeMagicHooks.emitLifeChanges`` / ``emitLandChanges``)::

      Life: Life: Ai(<k>)-<name> <old> > <new>
      Land: Ai(<k>)-<name> played a land
      Damage: <src> deals <n> [combat ]damage to Ai(<k>)-<name>.

* Per-game terminators (``XMageBatch.runMatch``)::

      XMAGEBATCH RESULT game=<g>/<N> winner=PlayerA|PlayerB|DRAW/UNFINISHED \
          endTurn=<n> ended=<bool> lifeA=<a> lifeB=<b> ms=<ms> :: <winner>
      Game Result: Game <N> ended in <ms> ms. Ai(<k>)-<name> has won!
      Game Result: Game <N> ended in a Draw! Took <ms> ms.

* Goldfish terminator (``XMageBatch.runSolo``)::

      GOLDFISH GAME <g>/<N> deck=<path> variant=<v> killed=<bool> \
          ownKillTurn=<n>|NONE globalTurn=<n> lifeB=<b> ms=<ms>

* Driver seam markers (``XMageBatch``: ``DRIVER_REGISTERED``; patch 0003
  ``ComputerPlayer6.act``: ``MACRO_FIRE_REAL``; the generated/emitter driver's
  ``apply()``: ``DRIVER_MACRO_FIRED``; patch 0005: ``DRIVER_MULLIGAN``;
  ``DRIVER_STEER_FIRED``)::

      DRIVER_REGISTERED fqcn=<fqcn> playerId=<id>
      MACRO_FIRE_REAL name=<name> turn=<n>          # the REAL commit -> fired_turn
      DRIVER_MACRO_FIRED ...                          # reachable in search -> assembled proxy
      DRIVER_STEER_FIRED ...
      DRIVER_MULLIGAN name=<seat> ship=<bool>

What is sourceable today vs. a Phase-2 follow-up
------------------------------------------------
Sourced now: winner, kill_turn, win_margin_life, wincon (combo/combat/burn/
other), game_length_ms, fired_turn (``MACRO_FIRE_REAL turn=``), driver_registered
/ macro_reachable / steer_fired, storm_count (casts on the fire turn),
disruption_survived (opponent cast before the fire), opp life-swing (snapshot
``opp_life``), mulligans (``DRIVER_MULLIGAN ship=true``, driven seats only),
timeout/brick + partial-transcript detection.

``assembled_turn`` is a PROXY: the turn (from the enclosing ``Turn:`` boundary) at
which the macro first became REACHABLE in search (``DRIVER_MACRO_FIRED``, which
carries no ``turn=`` of its own). A precise "all combo pieces present on the real
board" marker needs Phase 2 to add one (e.g. ``COMBO_ASSEMBLED turn=<n>`` from the
driver) — tracked as a follow-up. ``mill`` and ``deckout`` wincons likewise need a
new terminal marker: ``MakeMagicHooks`` does not emit an empty-library/mill line
today, so a deckout win currently classifies as ``combo`` (if a macro fired) or
``other``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pipeline import store
from pipeline.sim import store as sim_store
from pipeline.sim._log_patterns import DRAW_RESULT_RE, RESULT_RE, WINNER_RE

if TYPE_CHECKING:
    from collections.abc import Iterable

    import duckdb

log = logging.getLogger('make_magic.sim.transcript_parser')

__all__ = (
    'GameFeatures',
    'ingest_transcript',
    'is_fake_macro_win',
    'loser_life',
    'parse_transcript',
    'win_lethality',
)

# --- marker regexes (grounded in MakeMagicHooks.java / XMageBatch.java) ----- #

_TURN_RE = re.compile(r'^Turn: Turn (\d+) \(Ai\((\d)\)-')
_HANDLOG_TURNSTART_RE = re.compile(r'^HANDLOG turn=(\d+) event=turnstart active=Ai\((\d)\)-')
_HANDLOG_CAST_RE = re.compile(r'^HANDLOG turn=(\d+) event=cast .*?castBy=Ai\((\d)\)-')
_HANDLOG_GAMEEND_RE = re.compile(r'^HANDLOG turn=(\d+) event=gameend\b')
_OPP_LIFE_RE = re.compile(r'\bopp_life=(-?\d+)')
_LIFE_RE = re.compile(r'^Life: Life: Ai\((\d)\)-\S.*? (\d+) > (-?\d+)')
_DAMAGE_RE = re.compile(r'^Damage: .*? deals \d+ (combat )?damage to Ai\((\d)\)-')

_XMAGEBATCH_RESULT_RE = re.compile(
    r'^XMAGEBATCH RESULT game=\S+ winner=(\S+) endTurn=(\d+) ended=(true|false) '
    r'lifeA=(-?\d+) lifeB=(-?\d+) ms=(\d+)'
)
_GOLDFISH_RE = re.compile(
    r'^GOLDFISH GAME \S+ .*?\bkilled=(true|false) ownKillTurn=(\d+|NONE) globalTurn=(\d+) '
    r'lifeB=(-?\d+) ms=(\d+)'
)

_DRIVER_REGISTERED = 'DRIVER_REGISTERED'
_MACRO_FIRE_REAL_RE = re.compile(r'^MACRO_FIRE_REAL .*?\bturn=(\d+)')
_DRIVER_MACRO_FIRED = 'DRIVER_MACRO_FIRED'
_DRIVER_STEER_FIRED = 'DRIVER_STEER_FIRED'
_DRIVER_MULLIGAN_RE = re.compile(r'^DRIVER_MULLIGAN name=(\S+) ship=(true|false)')


def _slot_to_side(slot: str) -> str:
    """Map an ``Ai(k)`` slot digit to a side: ``'1' -> 'a'`` (candidate), else ``'b'``."""
    return 'a' if slot == '1' else 'b'


@dataclass(frozen=True)
class GameFeatures:
    """Per-game metrics parsed from ONE XMage transcript.

    ``winner`` is ``'a'`` / ``'b'`` / ``'draw'`` / ``'unknown'`` (the last for a
    partial transcript with no terminator). Scalars that could not be sourced are
    ``None``. ``incomplete`` is ``True`` when no terminator was seen; ``timeout``
    is ``True`` for an ``ended=false`` / brick game. ``is_anomaly`` (property) is
    the union used by the ``interest`` persistence policy.
    """

    winner: str
    kill_turn: int | None
    win_margin_life: int | None
    wincon: str | None
    mulligans_a: int
    mulligans_b: int
    game_length_ms: int | None
    # NEW richer transcript metrics.
    assembled_turn: int | None
    fired_turn: int | None
    driver_registered: bool
    macro_reachable: bool
    steer_fired: bool
    storm_count: int | None
    life_swing: int | None
    disruption_survived: bool | None
    last_turn: int | None
    incomplete: bool
    timeout: bool

    @property
    def macro_fired(self) -> bool:
        """Whether the registered combo macro really committed (``MACRO_FIRE_REAL``)."""
        return self.fired_turn is not None

    @property
    def is_anomaly(self) -> bool:
        """A game worth keeping under the ``interest`` policy for a non-macro reason."""
        return self.incomplete or self.timeout


def parse_transcript(text_or_lines: str | Iterable[str]) -> GameFeatures:
    """Parse a transcript (string or line iterable) into :class:`GameFeatures`.

    Pure and total: never raises on a partial / truncated / garbage transcript —
    it parses what is present and marks the rest ``None`` / ``incomplete``.
    """
    lines = text_or_lines.splitlines() if isinstance(text_or_lines, str) else list(text_or_lines)

    cur_turn: int | None = None
    last_turn: int | None = None
    kill_turn: int | None = None
    kill_wincon: str | None = None
    last_damage_combat: bool | None = None

    driver_registered = False
    macro_reachable = False
    steer_fired = False
    assembled_turn: int | None = None
    fired_turn: int | None = None

    casts_by_turn: dict[int, int] = {}
    opp_casts_before_fire = 0
    mull_a = 0
    mull_b = 0

    opp_life_first: int | None = None
    opp_life_last: int | None = None

    winner: str | None = None
    game_length_ms: int | None = None
    timeout = False
    saw_terminator = False
    saw_gameend = False

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        m = _TURN_RE.match(line)
        if m:
            cur_turn = int(m.group(1))
            last_turn = cur_turn
            continue

        m = _HANDLOG_TURNSTART_RE.match(line)
        if m:
            cur_turn = int(m.group(1))
            last_turn = cur_turn
            _ol = _OPP_LIFE_RE.search(line)
            if _ol:
                val = int(_ol.group(1))
                if opp_life_first is None:
                    opp_life_first = val
                opp_life_last = val
            continue

        m = _HANDLOG_CAST_RE.match(line)
        if m:
            t = int(m.group(1))
            side = _slot_to_side(m.group(2))
            casts_by_turn[t] = casts_by_turn.get(t, 0) + 1
            if side == 'b' and fired_turn is None:
                opp_casts_before_fire += 1
            _ol = _OPP_LIFE_RE.search(line)
            if _ol:
                opp_life_last = int(_ol.group(1))
            continue

        m = _HANDLOG_GAMEEND_RE.match(line)
        if m:
            saw_gameend = True
            _ol = _OPP_LIFE_RE.search(line)
            if _ol:
                opp_life_last = int(_ol.group(1))
            continue

        m = _DAMAGE_RE.match(line)
        if m:
            last_damage_combat = m.group(1) is not None
            continue

        m = _LIFE_RE.match(line)
        if m:
            side = _slot_to_side(m.group(1))
            after = int(m.group(3))
            if after <= 0 and kill_turn is None:
                kill_turn = cur_turn
                kill_wincon = 'combat' if last_damage_combat else 'burn'
            continue

        # Driver seam markers.
        if line.startswith(_DRIVER_REGISTERED):
            driver_registered = True
            continue
        if line.startswith(_DRIVER_MACRO_FIRED):
            macro_reachable = True
            if assembled_turn is None:
                assembled_turn = cur_turn
            continue
        if line.startswith(_DRIVER_STEER_FIRED):
            steer_fired = True
            continue
        fm = _MACRO_FIRE_REAL_RE.match(line)
        if fm:
            fired_turn = int(fm.group(1))
            continue
        mu = _DRIVER_MULLIGAN_RE.match(line)
        if mu:
            if mu.group(2) == 'true':
                if mu.group(1) == 'PlayerA':
                    mull_a += 1
                else:
                    mull_b += 1
            continue

        # Terminators.
        rm = _XMAGEBATCH_RESULT_RE.match(line)
        if rm:
            raw_winner = rm.group(1)
            if raw_winner == 'PlayerA':
                winner = 'a'
            elif raw_winner == 'PlayerB':
                winner = 'b'
            else:
                winner = 'draw'
            ended = rm.group(3) == 'true'
            if not ended:
                timeout = True
            game_length_ms = int(rm.group(6))
            saw_terminator = True
            continue

        gm = _GOLDFISH_RE.match(line)
        if gm:
            killed = gm.group(1) == 'true'
            winner = 'a' if killed else 'draw'
            if not killed:
                timeout = True
            elif gm.group(2) != 'NONE' and kill_turn is None:
                kill_turn = int(gm.group(2))
            game_length_ms = int(gm.group(5))
            saw_terminator = True
            continue

        if RESULT_RE.match(line):
            wm = WINNER_RE.search(line)
            if wm:
                winner = _slot_to_side(wm.group(1))
            saw_terminator = True
            if game_length_ms is None:
                rr = RESULT_RE.match(line)
                if rr:
                    game_length_ms = int(rr.group(1))
            continue
        dr = DRAW_RESULT_RE.match(line)
        if dr:
            if winner is None:
                winner = 'draw'
            saw_terminator = True
            if game_length_ms is None:
                game_length_ms = int(dr.group(1))
            continue

    incomplete = not (saw_terminator or saw_gameend)
    if winner is None:
        winner = 'unknown' if incomplete else 'draw'

    # A driven combo win with no life-loss kill turn -> attribute the kill to the
    # turn the macro committed (fired_turn).
    if kill_turn is None and fired_turn is not None and winner == 'a':
        kill_turn = fired_turn

    # wincon precedence: a life-0 killing blow (combat/burn) wins; else a macro
    # fire means combo; else a decided game is 'other'; else None.
    if kill_wincon is not None:
        wincon = kill_wincon
    elif fired_turn is not None and winner == 'a':
        wincon = 'combo'
    elif saw_terminator and winner in ('a', 'b'):
        wincon = 'other'
    else:
        wincon = None

    # win_margin_life: the winner's remaining life from the XMAGEBATCH RESULT line
    # (lifeA/lifeB) when decided. Sourced only from that line's snapshot.
    win_margin_life = _extract_win_margin(lines, winner)

    storm_count = casts_by_turn.get(fired_turn) if fired_turn is not None else None
    disruption_survived = (opp_casts_before_fire > 0) if fired_turn is not None else None

    life_swing: int | None = None
    if opp_life_first is not None and opp_life_last is not None:
        life_swing = opp_life_first - opp_life_last

    return GameFeatures(
        winner=winner,
        kill_turn=kill_turn,
        win_margin_life=win_margin_life,
        wincon=wincon,
        mulligans_a=mull_a,
        mulligans_b=mull_b,
        game_length_ms=game_length_ms,
        assembled_turn=assembled_turn,
        fired_turn=fired_turn,
        driver_registered=driver_registered,
        macro_reachable=macro_reachable,
        steer_fired=steer_fired,
        storm_count=storm_count,
        life_swing=life_swing,
        disruption_survived=disruption_survived,
        last_turn=last_turn,
        incomplete=incomplete,
        timeout=timeout,
    )


def _extract_win_margin(lines: list[str], winner: str) -> int | None:
    """The winner's remaining life from the ``XMAGEBATCH RESULT`` lifeA/lifeB pair."""
    if winner not in ('a', 'b'):
        return None
    for raw in lines:
        rm = _XMAGEBATCH_RESULT_RE.match(raw.strip())
        if rm:
            return int(rm.group(4)) if winner == 'a' else int(rm.group(5))
    return None


# --- win-lethality guardrail ----------------------------------------------- #
#
# A win is REAL only if the loser reached a substantive terminal (life <= 0 covers
# lethal damage, burn, and drain collapse; deckout/poison/commander damage also drive
# the loser to a rules loss). A decided win where the loser is still ALIVE is NON-LETHAL:
# either a macro-fire spurious end (the cheating driver — the sim awards a win the deck
# never played) or an opponent-AI concede. These functions expose that split for the
# honest-win-rate audit and the goldfish/gate lethality checks, WITHOUT touching the
# frozen ``GameFeatures``/``classify_validity`` schema.


def _as_lines(text_or_lines: str | Iterable[str]) -> list[str]:
    return text_or_lines.splitlines() if isinstance(text_or_lines, str) else list(text_or_lines)


def loser_life(text_or_lines: str | Iterable[str]) -> int | None:
    """The LOSER's remaining life from the ``XMAGEBATCH RESULT`` line.

    Complements ``win_margin_life`` (the WINNER's life). Returns ``None`` when the game is
    not a decided win (``DRAW/UNFINISHED``) or no RESULT line is present.
    """
    for raw in _as_lines(text_or_lines):
        rm = _XMAGEBATCH_RESULT_RE.match(raw.strip())
        if rm:
            win = rm.group(1)
            if win == 'PlayerA':
                return int(rm.group(5))  # loser = PlayerB
            if win == 'PlayerB':
                return int(rm.group(4))  # loser = PlayerA
            return None  # DRAW/UNFINISHED — no loser
    return None


def win_lethality(text_or_lines: str | Iterable[str]) -> str:
    """Classify a game's terminal as ``'lethal'`` / ``'nonlethal'`` / ``'undecided'``.

    ``'lethal'`` — a decided win with the loser at <= 0 life (a real kill).
    ``'nonlethal'`` — a decided win with the loser still ALIVE (a macro-fire spurious end
    or an opponent concede) — never our deck reducing the opponent to lethal.
    ``'undecided'`` — a draw / unfinished / unparseable game.
    """
    life = loser_life(text_or_lines)
    if life is None:
        return 'undecided'
    return 'lethal' if life <= 0 else 'nonlethal'


def is_fake_macro_win(text_or_lines: str | Iterable[str]) -> bool:
    """A macro-attributed non-lethal win — the "cheating driver" artifact.

    True iff the driven combo macro really committed (``MACRO_FIRE_REAL``) AND the game
    was credited a win with the loser still ALIVE. Distinguishes the driver bug from an
    opponent-AI concede (no macro) and from a real lethal (loser at <= 0). The honest
    win-rate audit excludes these from W/L.
    """
    lines = _as_lines(text_or_lines)
    if win_lethality(lines) != 'nonlethal':
        return False
    return any(_MACRO_FIRE_REAL_RE.match(ln.strip()) for ln in lines)


# --- persistence ----------------------------------------------------------- #

#: Additive transcript columns on ``sim_game_features`` (base schema owns the
#: first block). Added migration-tolerantly at ingest time (the EP4a idiom) so an
#: existing DB forward-migrates and ``store_matchup``'s named-column INSERT — which
#: never lists these — keeps writing NULLs into them unchanged.
_TRANSCRIPT_COLUMNS: tuple[tuple[str, str], ...] = (
    ('assembled_turn', 'INTEGER'),
    ('fired_turn', 'INTEGER'),
    ('driver_registered', 'BOOLEAN'),
    ('macro_reachable', 'BOOLEAN'),
    ('steer_fired', 'BOOLEAN'),
    ('storm_count', 'INTEGER'),
    ('life_swing', 'INTEGER'),
    ('disruption_survived', 'BOOLEAN'),
    ('incomplete', 'BOOLEAN'),
    ('timeout', 'BOOLEAN'),
)


def _ensure_transcript_columns(conn: duckdb.DuckDBPyConnection) -> None:
    """Add the transcript feature columns if a prior-schema DB lacks them (idempotent)."""
    cols = {row[1] for row in conn.execute('PRAGMA table_info(sim_game_features)').fetchall()}
    for name, sqltype in _TRANSCRIPT_COLUMNS:
        if name not in cols:
            conn.execute(f'ALTER TABLE sim_game_features ADD COLUMN {name} {sqltype}')


def _should_persist(feat: GameFeatures, game_index: int, policy: str) -> bool:
    """Apply the ``--transcript-policy`` filter to one parsed game."""
    if policy == 'all':
        return True
    if policy == 'interest':
        return feat.macro_fired or feat.is_anomaly
    if policy.startswith('sample:'):
        try:
            n = int(policy.split(':', 1)[1])
        except ValueError:
            n = 1
        return n <= 0 or game_index % n == 0
    # Unknown policy -> conservative default: keep everything.
    log.warning('unknown transcript policy %r; persisting anyway', policy)
    return True


def ingest_transcript(
    store_conn_or_path: str | os.PathLike[str] | None,
    cell_key: str,
    game_index: int,
    transcript_path: str | os.PathLike[str],
    *,
    policy: str = 'all',
    cleanup: bool = True,
) -> bool:
    """Parse a transcript file and persist its features + raw log to the store.

    ``cell_key`` (the matchup/cell content key) + ``game_index`` key the row —
    matching the ``store_matchup`` grain (``matchup_key`` + ``game_index``).
    ``store_conn_or_path`` is a data-dir path (or ``None`` for the default store).

    Best-effort and NON-FATAL: a missing / unreadable / malformed file is logged
    and returns ``False`` (never raises — a transcript hiccup must never break the
    game-queue run). Honors ``policy`` (``all`` / ``interest`` / ``sample:N``): a
    filtered-out game is not persisted and returns ``False``. On a successful
    persist the transcript file is deleted when ``cleanup`` is true.

    Returns ``True`` iff a row was written.
    """
    path = os.fspath(transcript_path)
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    except OSError as exc:
        log.warning('transcript ingest: cannot read %s (%s) — skipping', path, exc)
        return False

    try:
        feat = parse_transcript(text)
    except Exception:  # pragma: no cover - parse_transcript is total, defensive only
        log.exception('transcript ingest: parse failed for %s — skipping', path)
        return False

    if not _should_persist(feat, game_index, policy):
        log.debug('transcript ingest: game %s skipped by policy %s', game_index, policy)
        return False

    data_dir = os.fspath(store_conn_or_path) if store_conn_or_path is not None else None
    try:
        db_path = sim_store._db_path(data_dir)
        with store.connect(db_path) as conn:
            sim_store._ensure_tables(conn)
            _ensure_transcript_columns(conn)
            conn.execute(
                'DELETE FROM sim_game_features WHERE matchup_key = ? AND game_index = ?',
                [cell_key, game_index],
            )
            conn.execute(
                """
                INSERT INTO sim_game_features
                    (matchup_key, game_index, winner, kill_turn, win_margin_life,
                     wincon, mulligans_a, mulligans_b, game_length_ms,
                     assembled_turn, fired_turn, driver_registered, macro_reachable,
                     steer_fired, storm_count, life_swing, disruption_survived,
                     incomplete, timeout)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    cell_key,
                    game_index,
                    feat.winner,
                    feat.kill_turn,
                    feat.win_margin_life,
                    feat.wincon,
                    feat.mulligans_a,
                    feat.mulligans_b,
                    feat.game_length_ms,
                    feat.assembled_turn,
                    feat.fired_turn,
                    feat.driver_registered,
                    feat.macro_reachable,
                    feat.steer_fired,
                    feat.storm_count,
                    feat.life_swing,
                    feat.disruption_survived,
                    feat.incomplete,
                    feat.timeout,
                ],
            )
            conn.execute(
                'DELETE FROM sim_game_logs WHERE matchup_key = ? AND game_index = ?',
                [cell_key, game_index],
            )
            conn.execute(
                'INSERT INTO sim_game_logs (matchup_key, game_index, raw_log) VALUES (?, ?, ?)',
                [cell_key, game_index, text],
            )
    except Exception:
        log.exception('transcript ingest: persist failed for %s — skipping', path)
        return False

    if cleanup:
        try:
            os.unlink(path)
        except OSError as exc:
            log.warning('transcript ingest: could not remove %s (%s)', path, exc)

    return True
