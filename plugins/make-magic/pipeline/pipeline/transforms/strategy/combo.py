"""Combo detection + deck-aware win classification (T5 correction A, ADR 0016).

Consumes the shipped detector contract semantics
(``pipeline.transforms.combo_detect``): exact named-card / oracle-id matching.

Win classification is now performed by the closure-mechanism win-walk
(``win_walk.py``, ADR 0016) which is deck-aware — a combo's win-ness depends
on the deck's card composition. The regex path (``is_game_win_result`` +
``LETHAL_MANA_SINK_NAMES``) was deleted in the Phase 2B step 5b-ii flip.

This module is READ-ONLY with respect to the lake.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def combo_norm(name: str) -> str:
    """Case/space-insensitive card-name key (``combo_detect._norm_name``)."""
    return ' '.join((name or '').split()).casefold()


def combo_label(result: str) -> str:
    """Name a combo by its ``produces`` result: prefer the terminal clause, else the first.

    Uses the fid adapter's route classification to find the terminal/win clause,
    replacing the deleted regex heuristic.
    """
    clauses = [c.strip() for c in (result or '').split(';') if c.strip()]
    if not clauses:
        return 'combo'
    # Use the fid adapter to find a terminal/lethal clause
    from pipeline.transforms.strategy.win_walk import fid_route, resolve_fids_from_feature_names

    fids = resolve_fids_from_feature_names(clauses)
    _TERMINAL_ROUTES = frozenset({'terminal-win', 'sba-drain', 'deckout-opp'})
    for clause, fid in zip(clauses, fids, strict=False):
        if fid:
            r = fid_route(fid)
            if r and r['route'] in _TERMINAL_ROUTES:
                return clause
    return clauses[0]


def normalize_combo(variant_id: Any, names: Any, oids: Any, result: Any) -> dict:
    """Build a runtime combo dict from raw lake fields.

    The ``win`` flag is a placeholder (False) — the authoritative win
    determination is deck-dependent and computed by ``facts.py`` via the
    closure walk (ADR 0016). The ``mana`` field is removed.
    """
    names_l = list(names or [])
    oids_l = [str(o) for o in (oids or [])]
    res = str(result or '')
    return {
        'variant_id': str(variant_id),
        'card_names': names_l,
        'oids': oids_l,
        'norm_names': [combo_norm(n) for n in names_l],
        'result': res,
        'win': False,  # placeholder — set by facts.py via closure walk
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
        f"SELECT variant_id, card_names, card_oracle_ids, result FROM read_parquet('{combo_path}')"
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
