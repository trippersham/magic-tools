"""Normalize Commander Spellbook combos and detect them in a decklist.

Reads ``raw/combos`` (variant records — see ``sources.spellbook``), projects the
fields we need, and materializes a flat ``normalized/combo`` table:

    variant_id, card_names (list), card_oracle_ids (list), result (str)

Detection is exact named-card matching only. Template matching
(``requires[].template`` — "any permanent that can be cast using {C}") is
out of scope: those variants require a Scryfall search to resolve and
would produce speculative hits. We only report a combo when every concrete
``uses`` card is present in the deck (by oracle_id when available, else by
normalized name).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from pipeline import store

log = logging.getLogger('make_magic.transforms.combo_detect')

RAW_SOURCE = 'combos'
NORMALIZED_TABLE = 'combo'


@dataclass(frozen=True)
class Combo:
    """A normalized combo: its variant id, the concrete cards it uses, the result."""

    variant_id: str
    card_names: tuple[str, ...]
    card_oracle_ids: tuple[str, ...]
    result: str


def _norm_name(name: str) -> str:
    """Case/space-insensitive card-name key for matching."""
    return ' '.join((name or '').split()).casefold()


# --------------------------------------------------------------------------- #
# Normalize: raw variants -> flat combo rows.
# --------------------------------------------------------------------------- #


def _combo_from_variant(variant: dict) -> Combo | None:
    """Project one raw variant into a ``Combo`` (concrete ``uses`` cards only).

    Returns None if the variant has no concrete used cards (e.g. purely
    template-driven), since we cannot exact-match it.
    """
    names: list[str] = []
    oracle_ids: list[str] = []
    for use in variant.get('uses') or []:
        card = use.get('card') if isinstance(use, dict) else None
        if not card:
            continue
        name = card.get('name')
        if name:
            names.append(str(name))
            oid = card.get('oracleId')
            oracle_ids.append(str(oid) if oid is not None else '')
    if not names:
        return None
    results = [
        p['feature']['name']
        for p in (variant.get('produces') or [])
        if isinstance(p, dict) and p.get('feature') and p['feature'].get('name')
    ]
    return Combo(
        variant_id=str(variant.get('id')),
        card_names=tuple(names),
        card_oracle_ids=tuple(oracle_ids),
        result='; '.join(results),
    )


def normalize_variants(variants: list[dict]) -> list[Combo]:
    """Project raw variants into ``Combo`` rows (dropping template-only ones)."""
    out: list[Combo] = []
    for variant in variants:
        combo = _combo_from_variant(variant)
        if combo is not None:
            out.append(combo)
    return out


# --------------------------------------------------------------------------- #
# Detection: exact named-card matching against a deck.
# --------------------------------------------------------------------------- #


def combos_in_deck(
    oracle_ids_or_names: set[str],
    combos: list[Combo],
) -> list[Combo]:
    """Return every combo whose cards are all present in the deck (exact match).

    Args:
        oracle_ids_or_names: The deck's identity set — a mix of oracle_ids and/or
            card names is fine; names are matched case/space-insensitively.
        combos: Normalized combos (from ``normalize_variants`` / ``load_combos``).

    A combo matches iff every one of its concrete cards is in the deck, matched
    by oracle_id when the combo knows one, otherwise by normalized name. Template
    (non-concrete) requirements are ignored by construction (they never became
    ``Combo`` cards), so a combo with a template piece can still match on its
    concrete pieces — which is the intended "you have the named cards" signal.
    """
    have = set(oracle_ids_or_names)
    have_norm = {_norm_name(x) for x in oracle_ids_or_names}
    matched: list[Combo] = []
    for combo in combos:
        ok = True
        for name, oid in zip(combo.card_names, combo.card_oracle_ids, strict=False):
            if oid and oid in have:
                continue
            if _norm_name(name) in have_norm:
                continue
            ok = False
            break
        if ok:
            matched.append(combo)
    return matched


# --------------------------------------------------------------------------- #
# I/O — read raw variants, materialize normalized combos, load them back.
# --------------------------------------------------------------------------- #


def _load_raw_variants() -> list[dict]:
    """Read ``raw/combos`` back as dicts (id/uses/produces)."""
    with store.connect() as conn:
        rel = store.read_parquet(conn, 'raw', RAW_SOURCE)
        rows = rel.select('id, uses, produces').fetchall()
    variants: list[dict] = []
    for vid, uses, produces in rows:
        variants.append({'id': vid, 'uses': uses or [], 'produces': produces or []})
    return variants


def _materialize(combos: list[Combo]) -> Path:
    """Materialize normalized combos to ``normalized/combo``."""
    payload = [
        {
            'variant_id': c.variant_id,
            'card_names': list(c.card_names),
            'card_oracle_ids': list(c.card_oracle_ids),
            'result': c.result,
        }
        for c in combos
    ]
    with store.connect() as conn:
        norm_dir = store.StorePaths.resolve().layer_dir('normalized', create=True)
        tmp = norm_dir / f'_{NORMALIZED_TABLE}.tmp.json'
        tmp.write_text(json.dumps(payload), encoding='utf-8')
        try:
            if payload:
                rel = conn.read_json(str(tmp))
            else:
                rel = conn.sql(
                    'SELECT NULL::VARCHAR AS variant_id, '
                    '[]::VARCHAR[] AS card_names, '
                    '[]::VARCHAR[] AS card_oracle_ids, '
                    'NULL::VARCHAR AS result WHERE 1=0'
                )
            path = store.write_parquet(conn, rel, 'normalized', NORMALIZED_TABLE)
        finally:
            tmp.unlink(missing_ok=True)
    return path


def load_combos() -> list[Combo]:
    """Load the landed ``normalized/combo`` table back into ``Combo`` objects."""
    with store.connect() as conn:
        rel = store.read_parquet(conn, 'normalized', NORMALIZED_TABLE)
        rows = rel.select('variant_id, card_names, card_oracle_ids, result').fetchall()
    return [
        Combo(
            variant_id=str(vid),
            card_names=tuple(names or []),
            card_oracle_ids=tuple(oids or []),
            result=result or '',
        )
        for vid, names, oids, result in rows
    ]


def build() -> Path:
    """Read ``raw/combos``, normalize, materialize ``normalized/combo``; return the path."""
    variants = _load_raw_variants()
    combos = normalize_variants(variants)
    path = _materialize(combos)
    log.info('combo: normalized %d variants into %d combos.', len(variants), len(combos))
    return path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    path = build()
    print(f'materialized combo -> {path}')


if __name__ == '__main__':
    main()
