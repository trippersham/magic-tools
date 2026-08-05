"""Sources dispatcher: ``python -m pipeline.sources.run <source> [args...]``.

A no-orchestrator CLI: each source is a
plain module with a ``main()``; this dispatcher just routes to it so a stage can
run as either ``python -m pipeline.sources.run oracle_tags`` or directly
``python -m pipeline.sources.oracle_tags``.
"""

from __future__ import annotations

import sys

from pipeline.sources import airtable, oracle_tags, scryfall_bulk, spellbook

SOURCES = {
    'oracle_tags': oracle_tags.main,
    'combos': spellbook.main,
    'spellbook': spellbook.main,
    'oracle_cards': scryfall_bulk.main,
    'scryfall_bulk': scryfall_bulk.main,
    'airtable': airtable.main,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in SOURCES:
        avail = ', '.join(sorted(SOURCES))
        print(
            f'usage: python -m pipeline.sources.run <source>\n  sources: {avail}',
            file=sys.stderr,
        )
        raise SystemExit(2)
    source = sys.argv[1]
    # Hand remaining args to the target module's argparse (if any).
    sys.argv = [f'pipeline.sources.{source}', *sys.argv[2:]]
    SOURCES[source]()


if __name__ == '__main__':
    main()
