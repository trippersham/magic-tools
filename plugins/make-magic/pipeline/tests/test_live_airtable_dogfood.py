"""LIVE dogfood: read a REAL deck from the real Airtable base and prove it's
hydrated end-to-end — the gate that FakeAirtable doubles + stub resolvers missed.

Deselected by default (`addopts = -m "not live"` in pyproject) so the normal
suite stays OFFLINE + fast. Run it explicitly, with creds sourced:

    set -a && . ../.env && set +a          # exports AIRTABLE_API_KEY
    uv run --extra dev pytest -m live -q

It is READ-ONLY (never writes to the base) but DOES hit the live Airtable base +
Scryfall (real hydration). The point: a deck READ must come back HYDRATED
(type/CMC/oracle_id present, not name-only) so the fact sheet has real inputs —
the exact failure a real deck analysis surfaced that 319 offline tests did not.
"""

from __future__ import annotations

import os

import pytest

_HAS_CREDS = bool(os.getenv('AIRTABLE_API_KEY'))


@pytest.mark.live
@pytest.mark.skipif(not _HAS_CREDS, reason='needs AIRTABLE_API_KEY (source plugins/make-magic/.env first)')
def test_live_real_deck_reads_are_hydrated() -> None:
    from pipeline.collection.adapters.airtable_collection import AirtableCollectionStore

    store = AirtableCollectionStore.from_settings(os.environ['AIRTABLE_API_KEY'], writes_enabled=False)

    decks = store.list_decks()
    assert decks, 'the live base returned no decks'

    # A real deck with a commander + a meaningful card count.
    candidates = [d for d in decks if d.commanders and len(d.cards) >= 20]
    assert candidates, 'no deck with a commander and >=20 cards on the live base'
    deck = store.get_deck(candidates[0].name)

    nonbasic = [c for c in deck.cards if not (c.type_line or '').startswith('Basic Land')]
    assert nonbasic, f'{deck.name!r} has no non-basic cards'

    # The commander must be hydrated — the name-only regression made it name-only.
    cmdr = deck.commanders[0]
    assert cmdr.type_line, f'commander {cmdr.name!r} is name-only (deck reads not hydrated)'
    assert cmdr.oracle_id, f'commander {cmdr.name!r} has no oracle_id (otag layer needs it)'

    # A non-trivial number of the deck's cards must carry real enrichment. The
    # name-only bug yields ZERO; a real deck resolves at least its staples. This
    # is an ABSOLUTE floor (not a fraction) so it's robust to new-set coverage.
    with_type = [c for c in nonbasic if c.type_line]
    with_oid = [c for c in nonbasic if c.oracle_id]
    assert len(with_type) >= 10, f'only {len(with_type)} cards hydrated a type_line — deck reads look name-only'
    assert len(with_oid) >= 10, f'only {len(with_oid)} cards carry oracle_id'

    # The fact-sheet INPUTS are real (a curve exists), not all-zero.
    cmcs = [c.mana_value for c in with_type if c.mana_value is not None]
    assert cmcs and sum(cmcs) / len(cmcs) > 0, 'no real mana values — the curve would be all-zero'
