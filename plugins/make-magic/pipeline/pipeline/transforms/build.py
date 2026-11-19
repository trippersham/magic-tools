"""``python -m pipeline.transforms.build`` — (re)build the normalized marts.

Ensures the raw sources exist (running the ingest pullers, which fail-open to
the bundled snapshots offline) and then materializes every normalized table via
the ``driver.TABLES`` map (card_otag + combo), followed by the second-tier
combo-edge store derived from those two marts (ADR 0021 B1).

    python -m pipeline.transforms.build              # ensure raw, build all
    python -m pipeline.transforms.build --no-ingest  # build from existing raw only

The pullers are fail-open, so this runs OFFLINE against the committed snapshots;
with network it refreshes from live Scryfall / Commander Spellbook first.
"""

from __future__ import annotations

import argparse
import logging

from pipeline import store
from pipeline.sources import oracle_tags, spellbook
from pipeline.transforms import driver

log = logging.getLogger('make_magic.transforms.build')


def ensure_raw() -> None:
    """Ensure ``raw/oracle_tags`` and ``raw/combos`` exist (pull if missing).

    The pullers are cursor-gated + fail-open, so this is cheap when raw is
    fresh and safe offline (loads the bundled snapshot on any fetch failure).
    """
    if not store.table_exists('raw', oracle_tags.SOURCE):
        oracle_tags.sync()
    if not store.table_exists('raw', spellbook.SOURCE):
        spellbook.sync()


def _materialize_combo_edges() -> str:
    """Materialize the combo-edge store from the normalized marts (ADR 0021 B1).

    Second-tier derived asset, built AFTER ``driver.build_all()`` because it reads
    the ``combo`` + ``card_otag`` marts those builds land.  Lowers each combo's
    fid-classified recursion events into within-deck ``EventEffect`` couplings and
    persists them to the :class:`ProvisionalAttributeStore` the ``engine_report``
    path loads — the #65 ship-materialized pattern (process-once, cached, no
    runtime re-derive).

    Reproducible overwrite: the deterministic spellbook (combo-edge) layer is
    rebuilt clean from the CURRENT lake every materialization — matching the
    parquet marts' ``write_parquet`` overwrite — so a stale partial population
    from an older lake can never persist (the ``derivation_version`` skip key
    only guards LOGIC changes, not lake-content drift, so an append would keep
    stale combos).  Non-combo records (haiku provenance, when role-B lands) are
    preserved.  This feeds ONLY ``engine_report`` — the byte-parity resolve /
    compute_facts path (ADR 0003 / 0021) never consults the store, so the P0 SHAs
    are unmoved by this materialization.
    """
    from pipeline.transforms.strategy import machine_vocab
    from pipeline.transforms.strategy.combo_edges import (
        COMBO_EDGE_SOURCE,
        populate_combo_edges,
    )

    store_path = machine_vocab._DEFAULT_STORE_DIR / machine_vocab._STORE_FILENAME
    if store_path.exists():
        existing = machine_vocab.load_store(machine_vocab._DEFAULT_STORE_DIR)
        kept = [a for a in existing.attributes if a.provenance.source != COMBO_EDGE_SOURCE]
        store_path.unlink()
        if kept:
            machine_vocab.persist_batch(kept, store_dir=machine_vocab._DEFAULT_STORE_DIR)
    populate_combo_edges()
    return str(store_path)


def run(*, ingest: bool = True) -> dict[str, str]:
    """Build the normalized marts + the derived combo-edge store; return ``name -> path``.

    ``ingest`` gates the raw pull.  The combo-edge store (ADR 0021 B1) is a
    second-tier asset lowered from the ``combo`` + ``card_otag`` marts, so it is
    materialized after them.
    """
    if ingest:
        ensure_raw()
    built = driver.build_all()
    out = {name: str(path) for name, path in built.items()}
    out['combo_edges'] = _materialize_combo_edges()
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    parser = argparse.ArgumentParser(description='Build the normalized marts (card_otag + combo).')
    parser.add_argument(
        '--no-ingest',
        action='store_true',
        help='Skip ensuring raw sources; build from existing raw/ only.',
    )
    args = parser.parse_args()
    built = run(ingest=not args.no_ingest)
    for name, path in built.items():
        print(f'built {name} -> {path}')


if __name__ == '__main__':
    main()
