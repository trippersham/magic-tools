"""``python -m pipeline.transforms.build`` — (re)build the normalized marts.

Ensures the raw sources exist (running the ingest pullers, which fail-open to
the bundled snapshots offline) and then materializes every normalized table via
the ``driver.TABLES`` map (card_otag + combo).

    python -m pipeline.transforms.build              # ensure raw, build all
    python -m pipeline.transforms.build --no-ingest  # build from existing raw only

The pullers are fail-open, so this runs OFFLINE against the committed snapshots;
with network it refreshes from live Scryfall / Commander Spellbook first.
"""

from __future__ import annotations

import argparse
import logging

from pipeline import store
from pipeline.ingest import oracle_tags, spellbook
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


def run(*, ingest: bool = True) -> dict[str, str]:
    """Build the normalized marts; return ``name -> path``. ``ingest`` gates raw pull."""
    if ingest:
        ensure_raw()
    built = driver.build_all()
    return {name: str(path) for name, path in built.items()}


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
