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
    :func:`~pipeline.sim.telemetry.split_games` with CLOCKOUT segments excluded
    (:func:`~pipeline.sim.runner.is_clockout_segment`), so its ``game_index`` lines
    up 1:1 with ``sim_game_features`` (which excludes the same games). Read on
    demand via :func:`get_game_logs` — NOT loaded on the hot cache path.

The read-through hook is :func:`get_cached` (returns ``None`` on a miss); the
``--force`` bypass is the CALLER'S concern (they simply skip :func:`get_cached`).
Tables are created idempotently on first write, honoring ``MAKE_MAGIC_DATA_DIR``
so tests point at a tmp db.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pipeline import store
from pipeline.sim.runner import MatchResult
from pipeline.sim.telemetry import GameFeatures, decided_game_segments

if TYPE_CHECKING:
    import os

    import duckdb

log = logging.getLogger('make_magic.sim.store')

__all__ = (
    'CachedMatchup',
    'GoldfishGameRow',
    'GoldfishRecord',
    'MatchupMeta',
    'MatchupRow',
    'deck_hash',
    'feature_stats',
    'find_matchups',
    'get_cached',
    'get_game_logs',
    'get_goldfish',
    'get_goldfish_log',
    'goldfish_features',
    'goldfish_key',
    'matchup_key',
    'parse_goldfish_games',
    'persist_goldfish_run',
    'store_goldfish',
    'store_matchup',
)

#: DDL for the matchup cache — one row per content-addressed matchup.
#: ``engine`` + ``engine_version`` (replacing the old single ``forge_version``
#: column) identify the backend that produced the row; both are also folded into
#: :func:`matchup_key` so a Forge and an XMage run of the same inputs never
#: collide. Pre-1.3 DBs carry the old ``forge_version`` column instead and are
#: migrated forward in :func:`_ensure_tables` (see :func:`_migrate_matchups`).
_MATCHUPS_DDL = """
CREATE TABLE IF NOT EXISTS sim_matchups (
    matchup_key    TEXT PRIMARY KEY,
    deck_a_hash    TEXT,
    deck_b_hash    TEXT,
    seed           INT,
    n_games        INT,
    format         TEXT,
    engine         TEXT,
    engine_version TEXT,
    wins_a         INT,
    wins_b         INT,
    draws          INT,
    created_at     TIMESTAMP
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
#: log for that game (``game_index`` aligns with ``sim_game_features``). Storage
#: note: a verbose game log is ~40 KB, so a 300-game gauntlet candidate retains
#: ~12 MB of TEXT — cheap for DuckDB, but not free; prune old runs if it grows.
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
    engine: str
    engine_version: str


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
    engine: str
    engine_version: str
    wins_a: int
    wins_b: int
    draws: int
    created_at: str

    @property
    def forge_version(self) -> str:
        """Back-compat alias for :attr:`engine_version` (the ``log`` verb's ``--forge``).

        The store no longer keeps a distinct ``forge_version`` column — the
        backend version lives in :attr:`engine_version`. This alias keeps the
        existing ``simulate log --forge`` filter/display working unchanged.
        """
        return self.engine_version


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
    engine: str,
    engine_version: str,
    driver: tuple[str, str] | None = None,
) -> str:
    """A stable content hash identifying a matchup by its exact inputs.

    Combines the two per-deck hashes with the run params (seed, game count,
    format, sim ENGINE, and that engine's version) into one sha256. Deterministic
    and order-sensitive on ``(deck_a, deck_b)`` — swapping the decks yields a
    different key (Ai(1) vs Ai(2) is not symmetric). A deck edit, an
    engine-version bump, OR a change of engine (``'forge'`` vs ``'xmage'``)
    changes the key — so the same decks/seed/n under two DIFFERENT backends hash
    to DIFFERENT keys and never collide in the content cache, guaranteeing a miss
    for changed inputs.

    ``driver`` folds a PlayerA per-deck driver (its ``fqcn``) into the key so a
    DRIVEN run and the driverless run of the same deck/opponent/seed never collide
    (AC6 — the driven read must not be served a stale driverless cache row).
    ``None`` keeps the key byte-identical to the pre-driver shape.
    """
    parts = (
        deck_hash(deck_a_dck),
        deck_hash(deck_b_dck),
        str(seed),
        str(n_games),
        fmt,
        engine,
        engine_version,
    )
    if driver is not None:
        parts = (*parts, f'driver={driver[1]}')
    payload = '\x00'.join(parts).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the store tables if absent (idempotent — safe to call every op).

    Also forward-migrates a pre-1.3 ``sim_matchups`` (the old ``forge_version``
    column, no ``engine``/``engine_version``) — see :func:`_migrate_matchups`.
    """
    conn.execute(_MATCHUPS_DDL)
    _migrate_matchups(conn)
    conn.execute(_FEATURES_DDL)
    conn.execute(_LOGS_DDL)


def _migrate_matchups(conn: duckdb.DuckDBPyConnection) -> None:
    """Forward-migrate an old-schema ``sim_matchups`` in place — never crash on open.

    Migration strategy (chosen for the sim cache): ADDITIVE ``ALTER TABLE ADD
    COLUMN``. A pre-1.3 DB carries a ``forge_version`` column and NO
    ``engine``/``engine_version``. Because ``_MATCHUPS_DDL`` is
    ``CREATE TABLE IF NOT EXISTS``, that stale table would survive untouched and
    every new column read/write would then fail. So on open we detect the
    missing columns and add them, backfilling existing rows to
    ``engine='forge'`` and ``engine_version = <old forge_version>`` (the only
    backend that could have produced a pre-1.3 row). The legacy ``forge_version``
    column is left in place — harmless, and dropping it is not needed for
    correctness (reads/writes go through the named columns). Non-destructive: no
    row is lost, and every old row reads back correctly forge-tagged.
    """
    # PRAGMA table_info -> (cid, name, type, notnull, dflt_value, pk); name is [1].
    cols = {row[1] for row in conn.execute('PRAGMA table_info(sim_matchups)').fetchall()}
    if 'engine' in cols and 'engine_version' in cols:
        return  # already current
    if 'engine' not in cols:
        conn.execute('ALTER TABLE sim_matchups ADD COLUMN engine TEXT')
    if 'engine_version' not in cols:
        conn.execute('ALTER TABLE sim_matchups ADD COLUMN engine_version TEXT')
    # Backfill legacy rows: tag as forge, carry the old forge_version forward.
    if 'forge_version' in cols:
        conn.execute("UPDATE sim_matchups SET engine = 'forge' WHERE engine IS NULL")
        conn.execute('UPDATE sim_matchups SET engine_version = forge_version WHERE engine_version IS NULL')
    else:
        conn.execute("UPDATE sim_matchups SET engine = 'forge' WHERE engine IS NULL")


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
                 engine, engine_version, wins_a, wins_b, draws, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                key,
                meta.deck_a_hash,
                meta.deck_b_hash,
                meta.seed,
                meta.n_games,
                meta.format,
                meta.engine,
                meta.engine_version,
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
        # EXCLUDE clockout segments to stay 1:1 with `features` (which
        # `extract_match_features` derives from the SAME helper) — a clocked-out
        # game has a fabricated result and no forensic value, so dropping its log
        # keeps `game_index` aligned across the two tables (B1b). Using the shared
        # `decided_game_segments` is what makes the fresh piloting pool and this
        # persisted cache measure the identical games (fresh == cached, M1).
        game_logs = decided_game_segments(result.raw_log)
        # Invariant: log rows either line up 1:1 with feature rows (both derive from
        # the SAME clockout-excluded split) OR are absent — a result-less/elided log
        # (e.g. tests that pass a placeholder raw_log) yields 0 segments. Any OTHER
        # count means `features` and `raw_log` came from different matchups and the
        # two tables would silently desync on `game_index`. A real raise (not
        # `assert`, which `python -O` strips) — this guards persisted data.
        if len(game_logs) not in (0, len(features)):
            raise ValueError(
                f'log/feature game_index desync: {len(game_logs)} log segments vs '
                f'{len(features)} feature rows for {key} (features and raw_log must be from the same run)'
            )
        for game_index, game_log in enumerate(game_logs):
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
    via :func:`deck_hash`, no engine needed) to locate every run of a deck pair
    across seeds / game-counts / engines / engine versions, optionally narrowed
    by ``fmt``. No filter -> every stored matchup. Empty store -> ``[]``.
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
                   engine, engine_version, wins_a, wins_b, draws, created_at
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
            engine=r[6],
            engine_version=r[7],
            wins_a=r[8],
            wins_b=r[9],
            draws=r[10],
            created_at=str(r[11]),
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


# --------------------------------------------------------------------------- #
# Solo-goldfish persistence — the gate/compare own-turn clock, retained per run.
# --------------------------------------------------------------------------- #
#
# The solo goldfish path (:meth:`~pipeline.sim.engines.xmage.XMageEngine.goldfish_output`)
# runs a deck against a do-nothing passer and prints per-game ``GOLDFISH GAME`` lines +
# a ``GOLDFISH SUMMARY`` line. Every gate/compare goldfish was previously thrown away;
# these three tables retain the FULL run for retrospective analysis, mirroring the
# ``sim_matchups`` idiom (content-key upsert, DELETE-then-INSERT child rows):
#
#   * ``sim_goldfish``       — one row per RUN (a content key + summary scalars).
#   * ``sim_goldfish_games`` — one row per GAME (own_turn, killed, ms, …).
#   * ``sim_goldfish_logs``  — one row per RUN holding the raw stdout+stderr for replay.

_GOLDFISH_DDL = """
CREATE TABLE IF NOT EXISTS sim_goldfish (
    goldfish_key      TEXT PRIMARY KEY,
    deck_hash         TEXT,
    driver_fqcn       TEXT,
    alpha             INT,
    format            TEXT,
    games             INT,
    median_kills_own  DOUBLE,
    bricks            INT,
    max_turn          INT,
    fire_count        INT,
    reachable_count   INT,
    registered        BOOLEAN,
    mean_ms_per_game  DOUBLE,
    engine            TEXT,
    created_at        TIMESTAMP
)
"""

_GOLDFISH_GAMES_DDL = """
CREATE TABLE IF NOT EXISTS sim_goldfish_games (
    goldfish_key TEXT,
    game_index   INT,
    own_turn     INT,
    killed       BOOLEAN,
    global_turn  INT,
    ms           INT,
    fired        BOOLEAN
)
"""

_GOLDFISH_LOGS_DDL = """
CREATE TABLE IF NOT EXISTS sim_goldfish_logs (
    goldfish_key TEXT,
    raw_log      TEXT
)
"""

#: The per-game ``GOLDFISH GAME g/games deck=... killed=<bool> ownKillTurn=<int|NONE>
#: globalTurn=<int> lifeB=<int> ms=<int>`` line the Java harness prints (XMageBatch.runSolo).
_GOLDFISH_GAME_RE = re.compile(
    r'GOLDFISH GAME\s+(?P<idx>\d+)/(?P<games>\d+)\b.*?'
    r'\bkilled=(?P<killed>true|false)\b.*?'
    r'\bownKillTurn=(?P<own>NONE|-?\d+)\b.*?'
    r'\bglobalTurn=(?P<global>-?\d+)\b.*?'
    r'\bms=(?P<ms>\d+)\b'
)
#: The proactive-macro real-execution marker (counted for ``fire_count``).
_MACRO_FIRE_REAL = 'MACRO_FIRE_REAL'
#: The reachable-in-search marker (counted for ``reachable_count``).
_MACRO_REACHABLE = 'DRIVER_MACRO_FIRED'
#: The registration marker (presence → ``registered``).
_DRIVER_REGISTERED = 'DRIVER_REGISTERED'


@dataclass(frozen=True)
class GoldfishGameRow:
    """One solo-goldfish GAME's telemetry (one ``GOLDFISH GAME`` line).

    ``own_turn`` is the capped OWN kill turn (``None`` when the game did not kill —
    the ``ownKillTurn=NONE`` sentinel). ``fired`` records whether the driver's macro
    really executed in THAT game when derivable (``None`` when the raw log does not
    tag fire markers per game — the standalone solo harness does not, so it stays
    ``None`` rather than guessing)."""

    own_turn: int | None
    killed: bool
    global_turn: int | None
    ms: int | None
    fired: bool | None = None


@dataclass(frozen=True)
class GoldfishRecord:
    """A stored goldfish run: the summary scalars + its ordered per-game rows."""

    goldfish_key: str
    deck_hash: str
    driver_fqcn: str | None
    alpha: int | None
    format: str
    games: int
    median_kills_own: float
    bricks: int | None
    max_turn: int | None
    fire_count: int
    reachable_count: int
    registered: bool
    mean_ms_per_game: float | None
    engine: str
    created_at: str
    per_game: list[GoldfishGameRow] = field(default_factory=list)


def parse_goldfish_games(raw_log: str) -> list[GoldfishGameRow]:
    """Parse the per-game ``GOLDFISH GAME`` lines out of a ``--solo`` run's raw output.

    One :class:`GoldfishGameRow` per line, in emission order (game 1..N). A run with no
    such lines (an elided/placeholder log) yields ``[]`` — never a raise. ``fired`` is
    left ``None`` (the solo harness does not tag fire markers per game)."""
    rows: list[GoldfishGameRow] = []
    for m in _GOLDFISH_GAME_RE.finditer(raw_log):
        own_raw = m.group('own')
        rows.append(
            GoldfishGameRow(
                own_turn=None if own_raw == 'NONE' else int(own_raw),
                killed=m.group('killed') == 'true',
                global_turn=int(m.group('global')),
                ms=int(m.group('ms')),
            )
        )
    return rows


def goldfish_key(
    deck_text: str,
    *,
    driver_fqcn: str | None,
    alpha: int | None,
    fmt: str,
    games: int,
    salt: str = '',
) -> str:
    """A stable content hash identifying a solo-goldfish RUN by its exact inputs.

    Combines the deck hash with the driver identity (``fqcn``+``alpha``; ``None`` = the
    driverless CP7 baseline), format, and game-count. A driven and driverless run of the
    SAME deck therefore hash to DIFFERENT keys (the driver fqcn differs), and re-running
    the identical inputs upserts rather than duplicates. ``salt`` is an optional
    run-scoped discriminator (default ``''`` keeps re-runs collapsing onto one row)."""
    parts = (
        deck_hash(deck_text),
        driver_fqcn or 'none',
        str(alpha) if alpha is not None else 'none',
        fmt,
        str(games),
        salt,
    )
    return hashlib.sha256('\x00'.join(parts).encode('utf-8')).hexdigest()


def _ensure_goldfish_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the goldfish store tables if absent (idempotent — safe every op)."""
    conn.execute(_GOLDFISH_DDL)
    conn.execute(_GOLDFISH_GAMES_DDL)
    conn.execute(_GOLDFISH_LOGS_DDL)


def store_goldfish(
    key: str,
    *,
    deck_hash: str,
    driver_fqcn: str | None,
    alpha: int | None,
    fmt: str,
    result: object,
    per_game_rows: list[GoldfishGameRow],
    raw_log: str,
    engine: str,
    fire_count: int = 0,
    reachable_count: int = 0,
    registered: bool = False,
    data_dir: str | os.PathLike[str] | None = None,
) -> None:
    """Upsert the goldfish RUN row + REPLACE its per-game + log rows under ``key``.

    Idempotent by key (delete-then-insert, mirroring :func:`store_matchup`). ``result``
    is duck-typed for ``median_kills_own`` / ``games`` / ``max_turn`` / ``bricks`` (a
    :class:`~pipeline.sim.engines.xmage.GoldfishResult`). ``mean_ms_per_game`` is derived
    from ``per_game_rows``. The child rows are cleared then re-inserted so a re-store
    never leaves duplicates."""
    deck_h = deck_hash
    median = float(result.median_kills_own)  # type: ignore[attr-defined]
    games = int(result.games)  # type: ignore[attr-defined]
    max_turn = getattr(result, 'max_turn', None)
    bricks = getattr(result, 'bricks', None)
    ms_values = [r.ms for r in per_game_rows if r.ms is not None]
    mean_ms = (sum(ms_values) / len(ms_values)) if ms_values else None

    db_path = _db_path(data_dir)
    with store.connect(db_path) as conn:
        _ensure_goldfish_tables(conn)
        conn.execute('DELETE FROM sim_goldfish WHERE goldfish_key = ?', [key])
        conn.execute(
            """
            INSERT INTO sim_goldfish
                (goldfish_key, deck_hash, driver_fqcn, alpha, format, games,
                 median_kills_own, bricks, max_turn, fire_count, reachable_count,
                 registered, mean_ms_per_game, engine, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                key,
                deck_h,
                driver_fqcn,
                alpha,
                fmt,
                games,
                median,
                bricks,
                max_turn,
                fire_count,
                reachable_count,
                registered,
                mean_ms,
                engine,
                datetime.now(UTC),
            ],
        )
        conn.execute('DELETE FROM sim_goldfish_games WHERE goldfish_key = ?', [key])
        for game_index, gr in enumerate(per_game_rows):
            conn.execute(
                """
                INSERT INTO sim_goldfish_games
                    (goldfish_key, game_index, own_turn, killed, global_turn, ms, fired)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [key, game_index, gr.own_turn, gr.killed, gr.global_turn, gr.ms, gr.fired],
            )
        conn.execute('DELETE FROM sim_goldfish_logs WHERE goldfish_key = ?', [key])
        conn.execute(
            'INSERT INTO sim_goldfish_logs (goldfish_key, raw_log) VALUES (?, ?)',
            [key, raw_log],
        )


def get_goldfish(
    key: str,
    *,
    data_dir: str | os.PathLike[str] | None = None,
) -> GoldfishRecord | None:
    """Read back a stored goldfish run (summary + ordered per-game rows), or ``None``.

    ``None`` on a miss (unknown key / fresh db). The raw log is retained separately —
    read it via :func:`get_goldfish_log` (kept off this hot read)."""
    db_path = _db_path(data_dir)
    with store.connect(db_path) as conn:
        _ensure_goldfish_tables(conn)
        row = conn.execute(
            """
            SELECT goldfish_key, deck_hash, driver_fqcn, alpha, format, games,
                   median_kills_own, bricks, max_turn, fire_count, reachable_count,
                   registered, mean_ms_per_game, engine, created_at
            FROM sim_goldfish WHERE goldfish_key = ?
            """,
            [key],
        ).fetchone()
        if row is None:
            return None
        game_rows = conn.execute(
            """
            SELECT own_turn, killed, global_turn, ms, fired
            FROM sim_goldfish_games WHERE goldfish_key = ?
            ORDER BY game_index
            """,
            [key],
        ).fetchall()
    per_game = [
        GoldfishGameRow(own_turn=gr[0], killed=bool(gr[1]), global_turn=gr[2], ms=gr[3], fired=gr[4])
        for gr in game_rows
    ]
    return GoldfishRecord(
        goldfish_key=row[0],
        deck_hash=row[1],
        driver_fqcn=row[2],
        alpha=row[3],
        format=row[4],
        games=row[5],
        median_kills_own=row[6],
        bricks=row[7],
        max_turn=row[8],
        fire_count=row[9],
        reachable_count=row[10],
        registered=bool(row[11]),
        mean_ms_per_game=row[12],
        engine=row[13],
        created_at=str(row[14]),
        per_game=per_game,
    )


def get_goldfish_log(
    key: str,
    *,
    data_dir: str | os.PathLike[str] | None = None,
) -> str | None:
    """The retained raw stdout+stderr for a goldfish run (full retrospective replay)."""
    db_path = _db_path(data_dir)
    with store.connect(db_path) as conn:
        _ensure_goldfish_tables(conn)
        row = conn.execute(
            'SELECT raw_log FROM sim_goldfish_logs WHERE goldfish_key = ?', [key]
        ).fetchone()
    return row[0] if row is not None else None


def goldfish_features(
    *,
    fmt: str | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """Aggregate over ``sim_goldfish`` — proof the goldfish store is queryable.

    Returns ``runs`` (count), ``games`` (summed), ``avg_median_kills_own`` /
    ``median_median_kills_own`` (over the non-sentinel, ``>= 0`` medians), ``driven_runs``
    (fqcn present), and ``total_fires`` (summed ``fire_count``). Pass ``fmt`` to restrict.
    Empty store -> zeroed result."""
    db_path = _db_path(data_dir)
    with store.connect(db_path) as conn:
        _ensure_goldfish_tables(conn)
        where = ''
        params: list[object] = []
        if fmt is not None:
            where = 'WHERE format = ?'
            params = [fmt]
        agg = conn.execute(
            f"""
            SELECT
                count(*)                                                   AS runs,
                coalesce(sum(games), 0)                                    AS games,
                avg(CASE WHEN median_kills_own >= 0 THEN median_kills_own END)     AS avg_med,
                median(CASE WHEN median_kills_own >= 0 THEN median_kills_own END)  AS med_med,
                count(*) FILTER (WHERE driver_fqcn IS NOT NULL)            AS driven,
                coalesce(sum(fire_count), 0)                               AS fires
            FROM sim_goldfish
            {where}
            """,
            params,
        ).fetchone()
    assert agg is not None
    return {
        'runs': int(agg[0]),
        'games': int(agg[1]),
        'avg_median_kills_own': float(agg[2]) if agg[2] is not None else None,
        'median_median_kills_own': float(agg[3]) if agg[3] is not None else None,
        'driven_runs': int(agg[4]),
        'total_fires': int(agg[5]),
    }


def persist_goldfish_run(
    deck_text: str,
    *,
    driver_fqcn: str | None,
    alpha: int | None,
    fmt: str,
    games: int,
    result: object,
    raw_log: str,
    engine: str,
    salt: str = '',
    data_dir: str | os.PathLike[str] | None = None,
) -> str | None:
    """BEST-EFFORT: parse + persist a full solo-goldfish run; never raise.

    The single wiring point the sim path calls after a goldfish completes. Derives the
    content key, parses the per-game rows + the fire/reachable/registered markers out of
    ``raw_log``, and writes all three tables. Persistence is a SIDE-CHANNEL — any failure
    is logged and swallowed so a store error can never break the sim (the sim result is
    the product). Returns the content key on success, ``None`` on a swallowed failure."""
    try:
        key = goldfish_key(deck_text, driver_fqcn=driver_fqcn, alpha=alpha, fmt=fmt, games=games, salt=salt)
        per_game = parse_goldfish_games(raw_log)
        fire_count = raw_log.count(_MACRO_FIRE_REAL)
        reachable_count = raw_log.count(_MACRO_REACHABLE)
        registered = _DRIVER_REGISTERED in raw_log
        store_goldfish(
            key,
            deck_hash=deck_hash(deck_text),
            driver_fqcn=driver_fqcn,
            alpha=alpha,
            fmt=fmt,
            result=result,
            per_game_rows=per_game,
            raw_log=raw_log,
            engine=engine,
            fire_count=fire_count,
            reachable_count=reachable_count,
            registered=registered,
            data_dir=data_dir,
        )
        return key
    except Exception as exc:  # side-channel: a store failure never breaks the sim.
        log.warning('goldfish persistence failed (non-fatal): %s', exc)
        return None


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
