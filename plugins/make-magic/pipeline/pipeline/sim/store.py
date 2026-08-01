"""Content-addressed cache for Forge matchups + a queryable telemetry feature store.

Phases 2-3 run a Forge matchup (:class:`~pipeline.sim.runner.MatchResult`) and
parse per-game telemetry (:class:`~pipeline.sim.telemetry.GameFeatures`). Re-running
Forge is expensive and fully deterministic in its inputs, so this module caches a
matchup by a CONTENT HASH of its inputs — the two ``.dck`` texts plus the run
params (seed, game count, format, Forge version). Any change to a deck OR a Forge
version bump changes the hash, so the cache self-invalidates: a stale entry can
never be served for changed inputs (the classic content-addressed cache guarantee).

Everything lands in the SAME ``make_magic.duckdb`` as the rest of the lake, via
:mod:`pipeline.store.io` (callers NEVER ``import duckdb`` themselves). Two tables:

  * ``sim_matchups`` — one row per cached matchup: the content key, the per-deck
    hashes + params that produced it, and the win tally.
  * ``sim_game_features`` — one row per game, scalar telemetry in its own column
    so a batch aggregates in plain SQL (avg kill-turn, wincon counts, …). Land
    ramp curves are stored as native DuckDB ``INTEGER[]`` (list binding round-trips
    cleanly as Python ``list[int]`` — no JSON juggling needed).
  * ``sim_game_logs`` — one row per game holding the FULL verbose Forge log for
    that game, so any past game is forensically replayable WITHOUT re-running
    Forge (whose ``-s`` seed is not reliably reproducible — a re-run is a
    different game, so the log must be retained at run time, not re-derived). The
    log is sliced from ``MatchResult.raw_log`` via
    :func:`~pipeline.sim.telemetry.split_games`, so its ``game_index`` lines up
    1:1 with ``sim_game_features`` (both derive from the same split). Read on
    demand via :func:`get_game_logs` — NOT loaded on the hot cache path.

The read-through hook is :func:`get_cached` (returns ``None`` on a miss); the
``--force`` bypass is the CALLER'S concern (they simply skip :func:`get_cached`).
Tables are created idempotently on first write, honoring ``MAKE_MAGIC_DATA_DIR``
so tests point at a tmp db.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pipeline import store
from pipeline.sim.runner import MatchResult
from pipeline.sim.telemetry import GameFeatures, split_games

if TYPE_CHECKING:
    import os

    import duckdb

__all__ = (
    'CachedMatchup',
    'MatchupMeta',
    'MatchupRow',
    'deck_hash',
    'feature_stats',
    'find_matchups',
    'get_cached',
    'get_game_logs',
    'matchup_key',
    'store_matchup',
)

#: DDL for the matchup cache — one row per content-addressed matchup.
_MATCHUPS_DDL = """
CREATE TABLE IF NOT EXISTS sim_matchups (
    matchup_key   TEXT PRIMARY KEY,
    deck_a_hash   TEXT,
    deck_b_hash   TEXT,
    seed          INT,
    n_games       INT,
    format        TEXT,
    forge_version TEXT,
    wins_a        INT,
    wins_b        INT,
    draws         INT,
    created_at    TIMESTAMP
)
"""

#: DDL for the telemetry feature store — one row per game; scalars aggregate in
#: SQL, land curves are native INTEGER[] lists.
_FEATURES_DDL = """
CREATE TABLE IF NOT EXISTS sim_game_features (
    matchup_key     TEXT,
    game_index      INT,
    winner          TEXT,
    kill_turn       INT,
    win_margin_life INT,
    wincon          TEXT,
    mulligans_a     INT,
    mulligans_b     INT,
    game_length_ms  INT,
    lands_by_turn_a INTEGER[],
    lands_by_turn_b INTEGER[]
)
"""

#: DDL for the per-game raw-log store — one row per game, the full verbose Forge
#: log for that game (``game_index`` aligns with ``sim_game_features``).
_LOGS_DDL = """
CREATE TABLE IF NOT EXISTS sim_game_logs (
    matchup_key TEXT,
    game_index  INT,
    raw_log     TEXT
)
"""


@dataclass(frozen=True)
class MatchupMeta:
    """The inputs that produced a cached matchup (stored alongside the tally).

    Carried into :func:`store_matchup` so the matchup row records exactly which
    decks (by hash) + params produced the result — useful for later audits and
    for reconstructing the key.
    """

    deck_a_hash: str
    deck_b_hash: str
    seed: int
    n_games: int
    format: str
    forge_version: str


@dataclass(frozen=True)
class CachedMatchup:
    """A cache hit: the stored win tally + per-game telemetry (ordered by game)."""

    wins_a: int
    wins_b: int
    draws: int
    features: list[GameFeatures]


@dataclass(frozen=True)
class MatchupRow:
    """A stored matchup's metadata — the lookup surface for forensic log retrieval.

    Everything needed to identify a past run (the ``matchup_key`` that
    :func:`get_game_logs` / :func:`get_cached` take) plus the human-facing summary
    (decks by hash, params, tally, when it ran).
    """

    matchup_key: str
    deck_a_hash: str
    deck_b_hash: str
    seed: int
    n_games: int
    format: str
    forge_version: str
    wins_a: int
    wins_b: int
    draws: int
    created_at: str


def _normalize_dck(dck_text: str) -> str:
    """Normalize ``.dck`` text for hashing: strip trailing whitespace per line and
    a trailing newline, so cosmetic whitespace churn does not spuriously bust the
    cache while any real card-list change still does."""
    return '\n'.join(line.rstrip() for line in dck_text.splitlines())


def deck_hash(dck_text: str) -> str:
    """The per-deck sha256 (hex) of the normalized ``.dck`` text.

    Used for ``deck_a_hash`` / ``deck_b_hash`` and folded into :func:`matchup_key`,
    so editing a deck changes both the per-deck hash and the matchup key.
    """
    return hashlib.sha256(_normalize_dck(dck_text).encode('utf-8')).hexdigest()


def matchup_key(
    deck_a_dck: str,
    deck_b_dck: str,
    *,
    seed: int,
    n_games: int,
    fmt: str,
    forge_version: str,
) -> str:
    """A stable content hash identifying a matchup by its exact inputs.

    Combines the two per-deck hashes with the run params (seed, game count,
    format, Forge version) into one sha256. Deterministic and order-sensitive on
    ``(deck_a, deck_b)`` — swapping the decks yields a different key (Ai(1) vs
    Ai(2) is not symmetric). A deck edit OR a Forge-version bump changes the key,
    guaranteeing a miss for changed inputs.
    """
    parts = (
        deck_hash(deck_a_dck),
        deck_hash(deck_b_dck),
        str(seed),
        str(n_games),
        fmt,
        forge_version,
    )
    payload = '\x00'.join(parts).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the store tables if absent (idempotent — safe to call every op)."""
    conn.execute(_MATCHUPS_DDL)
    conn.execute(_FEATURES_DDL)
    conn.execute(_LOGS_DDL)


def store_matchup(
    key: str,
    meta: MatchupMeta,
    result: MatchResult,
    features: list[GameFeatures],
    *,
    data_dir: str | os.PathLike[str] | None = None,
) -> None:
    """Upsert the matchup row + REPLACE its feature and per-game log rows under ``key``.

    Idempotent by key: the matchup row is deleted-then-inserted and every prior
    ``sim_game_features`` / ``sim_game_logs`` row for ``key`` is cleared first, so
    re-storing the same key never leaves duplicate rows or a stale tally.
    ``features`` is persisted in order, one row per game (``game_index`` =
    position). The full verbose log is sliced from ``result.raw_log`` via
    :func:`~pipeline.sim.telemetry.split_games` and persisted one row per game
    under the SAME ``game_index`` grain (a result-less log simply yields no log
    rows — never an error).
    """
    db_path = _db_path(data_dir)
    with store.connect(db_path) as conn:
        _ensure_tables(conn)
        # Upsert the matchup row (DuckDB has no ON CONFLICT for INSERT here; a
        # delete-then-insert under the PK is the portable, race-free-in-one-conn form).
        conn.execute('DELETE FROM sim_matchups WHERE matchup_key = ?', [key])
        conn.execute(
            """
            INSERT INTO sim_matchups
                (matchup_key, deck_a_hash, deck_b_hash, seed, n_games, format,
                 forge_version, wins_a, wins_b, draws, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                key,
                meta.deck_a_hash,
                meta.deck_b_hash,
                meta.seed,
                meta.n_games,
                meta.format,
                meta.forge_version,
                result.wins_a,
                result.wins_b,
                result.draws,
                datetime.now(UTC),
            ],
        )
        # Replace the feature rows wholesale.
        conn.execute('DELETE FROM sim_game_features WHERE matchup_key = ?', [key])
        for game_index, feat in enumerate(features):
            conn.execute(
                """
                INSERT INTO sim_game_features
                    (matchup_key, game_index, winner, kill_turn, win_margin_life,
                     wincon, mulligans_a, mulligans_b, game_length_ms,
                     lands_by_turn_a, lands_by_turn_b)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    key,
                    game_index,
                    feat.winner,
                    feat.kill_turn,
                    feat.win_margin_life,
                    feat.wincon,
                    feat.mulligans_a,
                    feat.mulligans_b,
                    feat.game_length_ms,
                    list(feat.lands_by_turn_a),
                    list(feat.lands_by_turn_b),
                ],
            )

        # Replace the per-game log rows wholesale (sliced from the full verbose log).
        conn.execute('DELETE FROM sim_game_logs WHERE matchup_key = ?', [key])
        for game_index, game_log in enumerate(split_games(result.raw_log)):
            conn.execute(
                'INSERT INTO sim_game_logs (matchup_key, game_index, raw_log) VALUES (?, ?, ?)',
                [key, game_index, game_log],
            )


def get_cached(
    key: str,
    *,
    data_dir: str | os.PathLike[str] | None = None,
) -> CachedMatchup | None:
    """Read-through hook: return the cached tally + features for ``key``, or ``None``.

    ``None`` on a miss (unknown key, or a fresh db with no tables yet). Features
    come back ordered by ``game_index`` with the land curves rehydrated as
    ``list[int]``. The ``--force`` bypass is the caller's job — they skip this call.
    """
    db_path = _db_path(data_dir)
    with store.connect(db_path) as conn:
        _ensure_tables(conn)
        row = conn.execute(
            'SELECT wins_a, wins_b, draws FROM sim_matchups WHERE matchup_key = ?',
            [key],
        ).fetchone()
        if row is None:
            return None
        wins_a, wins_b, draws = row

        feature_rows = conn.execute(
            """
            SELECT winner, kill_turn, win_margin_life, wincon, mulligans_a,
                   mulligans_b, game_length_ms, lands_by_turn_a, lands_by_turn_b
            FROM sim_game_features
            WHERE matchup_key = ?
            ORDER BY game_index
            """,
            [key],
        ).fetchall()

    features = [
        GameFeatures(
            winner=fr[0],
            kill_turn=fr[1],
            win_margin_life=fr[2],
            wincon=fr[3],
            mulligans_a=fr[4],
            mulligans_b=fr[5],
            game_length_ms=fr[6],
            lands_by_turn_a=list(fr[7]) if fr[7] is not None else [],
            lands_by_turn_b=list(fr[8]) if fr[8] is not None else [],
        )
        for fr in feature_rows
    ]
    return CachedMatchup(wins_a=wins_a, wins_b=wins_b, draws=draws, features=features)


def get_game_logs(
    key: str,
    *,
    game_index: int | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Return the retained per-game verbose logs for ``key``, ordered by game.

    The forensic-replay reader (kept OFF the hot cache path so ``get_cached``
    stays lean). Pass ``game_index`` to fetch just that one game's log (a list of
    0 or 1). An unknown key, a fresh db, or a matchup stored with a result-less
    log all yield ``[]`` — never a raise. Each string is the full verbose Forge
    log for exactly one game, so it can be re-parsed or eyeballed line by line.
    """
    db_path = _db_path(data_dir)
    with store.connect(db_path) as conn:
        _ensure_tables(conn)
        if game_index is not None:
            rows = conn.execute(
                'SELECT raw_log FROM sim_game_logs WHERE matchup_key = ? AND game_index = ?',
                [key, game_index],
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT raw_log FROM sim_game_logs WHERE matchup_key = ? ORDER BY game_index',
                [key],
            ).fetchall()
    return [row[0] for row in rows]


def find_matchups(
    *,
    deck_a_hash: str | None = None,
    deck_b_hash: str | None = None,
    fmt: str | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> list[MatchupRow]:
    """Find stored matchups, newest first — the offline lookup for log retrieval.

    All filters are optional AND-ed: pass the two deck hashes (computed offline
    via :func:`deck_hash`, no Forge needed) to locate every run of a deck pair
    across seeds / game-counts / Forge versions, optionally narrowed by ``fmt``.
    No filter -> every stored matchup. Empty store -> ``[]``.
    """
    clauses: list[str] = []
    params: list[object] = []
    for column, value in (
        ('deck_a_hash', deck_a_hash),
        ('deck_b_hash', deck_b_hash),
        ('format', fmt),
    ):
        if value is not None:
            clauses.append(f'{column} = ?')
            params.append(value)
    where = f'WHERE {" AND ".join(clauses)}' if clauses else ''

    db_path = _db_path(data_dir)
    with store.connect(db_path) as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            f"""
            SELECT matchup_key, deck_a_hash, deck_b_hash, seed, n_games, format,
                   forge_version, wins_a, wins_b, draws, created_at
            FROM sim_matchups
            {where}
            ORDER BY created_at DESC
            """,
            params,
        ).fetchall()
    return [
        MatchupRow(
            matchup_key=r[0],
            deck_a_hash=r[1],
            deck_b_hash=r[2],
            seed=r[3],
            n_games=r[4],
            format=r[5],
            forge_version=r[6],
            wins_a=r[7],
            wins_b=r[8],
            draws=r[9],
            created_at=str(r[10]),
        )
        for r in rows
    ]


def feature_stats(
    *,
    format: str | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """Aggregate over ``sim_game_features`` — proof the store is queryable for stats.

    Returns ``games`` (count), ``avg_kill_turn`` / ``median_kill_turn`` (over the
    non-null kill turns), and ``wincon_counts`` (a ``{wincon: count}`` map,
    excluding ``NULL``). Pass ``format`` to restrict to games from matchups of that
    format (joined via ``sim_matchups``). Empty store -> zeroed / empty result.
    """
    db_path = _db_path(data_dir)
    with store.connect(db_path) as conn:
        _ensure_tables(conn)

        where = ''
        params: list[object] = []
        if format is not None:
            where = 'WHERE m.format = ?'
            params = [format]

        agg = conn.execute(
            f"""
            SELECT
                count(*)               AS games,
                avg(f.kill_turn)       AS avg_kill_turn,
                median(f.kill_turn)    AS median_kill_turn
            FROM sim_game_features f
            JOIN sim_matchups m USING (matchup_key)
            {where}
            """,
            params,
        ).fetchone()
        games = int(agg[0]) if agg is not None else 0
        avg_kill_turn = float(agg[1]) if agg is not None and agg[1] is not None else None
        median_kill_turn = float(agg[2]) if agg is not None and agg[2] is not None else None

        wincon_rows = conn.execute(
            f"""
            SELECT f.wincon, count(*) AS n
            FROM sim_game_features f
            JOIN sim_matchups m USING (matchup_key)
            {where + (' AND' if where else 'WHERE')} f.wincon IS NOT NULL
            GROUP BY f.wincon
            """,
            params,
        ).fetchall()
        wincon_counts = {row[0]: int(row[1]) for row in wincon_rows}

    return {
        'games': games,
        'avg_kill_turn': avg_kill_turn,
        'median_kill_turn': median_kill_turn,
        'wincon_counts': wincon_counts,
    }


def _db_path(data_dir: str | os.PathLike[str] | None) -> str | None:
    """Resolve the DuckDB file path for an optional ``data_dir`` override.

    ``store.io.connect`` already honors ``MAKE_MAGIC_DATA_DIR`` when handed
    ``None``; an explicit ``data_dir`` points at that root's ``make_magic.duckdb``.
    """
    if data_dir is None:
        return None
    return str(store.StorePaths(data_dir=_as_path(data_dir)).db_path)


def _as_path(data_dir: str | os.PathLike[str]):
    from pathlib import Path

    return Path(data_dir)
