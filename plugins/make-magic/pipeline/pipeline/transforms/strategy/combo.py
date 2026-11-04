"""Combo detection + the game-WIN litmus (T5 correction A).

Consumes the shipped detector contract semantics
(``pipeline.transforms.combo_detect``): exact named-card / oracle-id matching,
the ``is_game_win_result`` litmus, and the deck-aware mana-sink promotion
(an infinite-mana loop + an in-deck lethal mana sink reads as a win-combo).

This module is READ-ONLY with respect to the lake. The win flag is recomputed
at runtime from each combo's ``result`` string because the landed
``normalized/combo`` projection predates the per-feature ``produces.win`` flag.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# The win litmus assumes LETHAL-SCALE phrasing: a ``result`` counts as a game
# win only when it states an actual game-ending outcome — someone wins / loses
# the game, infinite (or arbitrarily-large "near-infinite") damage / life loss,
# a lethal POISON count (ten / 10+ poison counters, or a "poison counters equal
# …" scaler), or a mill of a WHOLE opponent library. Bare "poison" and "mill
# for N" describe a *magnitude*, not a win (e.g. "add a poison counter", "mill
# for three"), so they deliberately do NOT match.
_WIN_RESULT_RE = re.compile('|'.join((
    r'win the game', r'wins the game', r'\bwin\b', r'\bwins\b',
    r'lose the game', r'loses the game', r'losing the game',
    r'each opponent loses the game',
    r'infinite damage', r'\blethal\b', r'infinite turns',
    r'\blifeloss\b', r'(?<!self-)infinite mill',
    r'ten poison', r'\b\d{2,} poison', r'poison counters? equal',
    r"mill[^;.]*\b(?:entire|all|their|each opponent'?s?)\b[^;.]*\blibrar",
    r"exile (?:target |each )?opponent(?:'s|s') librar",
)), re.IGNORECASE)
_INFINITE_MANA_RE = re.compile(r'infinite\b[^;]*\bmana\b', re.IGNORECASE)

#: X-spell / mana-sink finishers: an infinite-mana loop plus one of these in the
#: deck promotes the loop to a win-combo (deck-aware mana-sink promotion).
LETHAL_MANA_SINK_NAMES: tuple[str, ...] = (
    'Fireball', 'Comet Storm', 'Crackle with Power', "Jaya's Immolating Inferno", 'Banefire',
    'Fall of the Titans', 'Rolling Thunder', "Red Sun's Zenith", 'Exsanguinate',
    'Torment of Hailfire', 'Debt to the Deathless', 'Profane Source', "Blue Sun's Zenith",
    'Stroke of Genius', 'Mind Grind', 'Villainous Wealth', 'Walking Ballista',
    'Finale of Devastation', 'Finale of Revelation', 'Hydroid Krasis', 'Helix Pinnacle',
    'Aetherflux Reservoir',
)


def combo_norm(name: str) -> str:
    """Case/space-insensitive card-name key (``combo_detect._norm_name``)."""
    return ' '.join((name or '').split()).casefold()


LETHAL_MANA_SINKS: frozenset[str] = frozenset(combo_norm(n) for n in LETHAL_MANA_SINK_NAMES)


def is_game_win_result(result: str) -> bool:
    return bool(_WIN_RESULT_RE.search(result or ''))


def is_infinite_mana_result(result: str) -> bool:
    return bool(_INFINITE_MANA_RE.search(result or ''))


def combo_label(result: str) -> str:
    """Name a combo by its ``produces`` result: prefer the lethal clause, else the first."""
    clauses = [c.strip() for c in (result or '').split(';') if c.strip()]
    if not clauses:
        return 'combo'
    win = next((c for c in clauses if _WIN_RESULT_RE.search(c)), None)
    return win or clauses[0]


def normalize_combo(variant_id: Any, names: Any, oids: Any, result: Any) -> dict:
    """Build a runtime combo dict (with ``win``/``mana`` flags) from raw lake fields."""
    names_l = list(names or [])
    oids_l = [str(o) for o in (oids or [])]
    res = str(result or '')
    return {
        'variant_id': str(variant_id),
        'card_names': names_l,
        'oids': oids_l,
        'norm_names': [combo_norm(n) for n in names_l],
        'result': res,
        'win': is_game_win_result(res),
        'mana': is_infinite_mana_result(res),
    }


def load_combos_from_lake(oracle_parquet_dir: Path | None = None) -> list[dict]:
    """READ-ONLY load of ``normalized/combo.parquet`` → runtime combo dicts.

    Resolves the lake path the same way the rest of the pipeline does (via
    ``pipeline.store`` when available, else the ``MAKE_MAGIC_DATA_DIR`` / XDG
    default). Import of duckdb is deferred so the pure functions above (and the
    whole test suite) never require it.
    """
    import duckdb

    combo_path = _combo_parquet_path(oracle_parquet_dir)
    con = duckdb.connect(':memory:')
    rows = con.execute(
        'SELECT variant_id, card_names, card_oracle_ids, result '
        f"FROM read_parquet('{combo_path}')"
    ).fetchall()
    return [normalize_combo(vid, names, oids, result) for vid, names, oids, result in rows]


def _combo_parquet_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return Path(explicit) / 'normalized' / 'combo.parquet'
    try:  # prefer the pipeline's own resolved store root
        from pipeline import store

        return store.StorePaths.resolve().layer_dir('normalized') / 'combo.parquet'
    except Exception:
        import os

        base = os.environ.get('MAKE_MAGIC_DATA_DIR')
        root = Path(base) if base else (Path.home() / '.local/share/make-magic')
        return root / 'normalized' / 'combo.parquet'


def combos_in_deck(have_oid: set[str], have_norm: set[str], combos: list[dict]) -> list[dict]:
    """Every combo whose concrete pieces are ALL in the deck (oracle_id else name).

    Template pieces are never materialized; this is exact-match detection only
    (``combo_detect.combos_in_deck``).
    """
    matched: list[dict] = []
    for c in combos:
        ok = True
        oids = c['oids']
        for i, nn in enumerate(c['norm_names']):
            oid = oids[i] if i < len(oids) else ''
            if oid and oid in have_oid:
                continue
            if nn in have_norm:
                continue
            ok = False
            break
        if ok:
            matched.append(c)
    return matched
