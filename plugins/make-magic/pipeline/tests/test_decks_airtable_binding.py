"""Airtable source binding — promote / push target the BOUND recordId, never a
same-named sibling.

Proven against the adapter's CONTRACT DOUBLE, which implements the adapter's own
``update_record`` / ``create_record`` targeting rule verbatim (update when the deck
carries a recordId, else create + stamp the new recordId back in place) under the
REAL store / sync / access code:

- an exploration promote takes the ``update_record`` branch on the parent's
  recordId — no duplicate Decks row is created;
- a clean-slate promote ``--to`` an existing Airtable name binds the promoted row
  to the NEW record and later updates THAT record, never the unrelated original;
- a correctly-bound row pushes to its recordId, never spuriously drift-refusing by
  comparing against a same-named sibling.

ZERO prod writes; no network; no ``delete_record``.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from _decks_helpers import MockAirtableDecks, commander_deck

from pipeline.contracts import Deck, DeckCard
from pipeline.decks import DecksStore
from pipeline.decks.access import DeckAccess
from pipeline.decks.sync import promote, push
from pipeline.decks.version import version


def test_exploration_promote_updates_parent_record(data_dir: Path) -> None:
    """Exploration promote takes the update_record branch on the parent's recordId.

    NO duplicate Decks row is created; the parent record receives the edit; the
    user reads the improvement back. Proven against the adapter's contract double.
    """
    decks = DecksStore()
    driver = MockAirtableDecks()
    access = DeckAccess(driver, decks=decks)  # type: ignore[arg-type]

    cards = [DeckCard(name='Krenko, Mob Boss', role='commander')] + [DeckCard(name=f'Card {i}') for i in range(99)]
    driver.save_deck(Deck(name='Ozai', format='Commander', cards=cards))
    driver.log.clear()

    parent = access.read_deck('Ozai')
    parent_uuid = access.resolve('Ozai')
    draft = parent.model_copy(update={'name': 'Ozai (explore)', 'airtable_record_id': None, 'uuid': uuid4().hex})
    draft_uuid = decks.create_ephemeral(draft, derived_from=parent_uuid)
    decks.swap(draft_uuid, add=DeckCard(name='Improvement'), cut='Card 0')

    promote(decks, driver, deck_uuid=draft_uuid)  # type: ignore[arg-type]

    # Exactly one Decks row named 'Ozai' — no junk duplicate created.
    ozai_rows = [r for r, d in driver.records.items() if d.name == 'Ozai']
    assert ozai_rows == ['rec00001'], f'duplicate Decks row created: {driver.records}'
    assert any('update_record(rec00001' in line for line in driver.log)
    assert not any('create_record' in line for line in driver.log)
    # The parent record received the improvement; the user reads it back.
    assert any(c.name == 'Improvement' for c in driver.records['rec00001'].cards)
    seen = access.read_deck('Ozai')
    assert any(c.name == 'Improvement' for c in seen.cards)


def test_dup_name_promote_binds_and_updates_new_record(data_dir: Path) -> None:
    """A clean-slate promote --to an EXISTING Airtable name must bind the promoted
    row to the NEW record and later update THAT record, never the unrelated original."""
    decks = DecksStore()
    driver = MockAirtableDecks()
    access = DeckAccess(driver, decks=decks)  # type: ignore[arg-type]
    driver.save_deck(commander_deck('Ozai', filler=99))  # rec00001

    access.read_deck('Ozai')  # row A -> rec00001
    draft = Deck(
        name='Junk', format='Commander',
        cards=[DeckCard(name='Grumgully, the Generous', role='commander'), DeckCard(name='Junk 1')],
    )
    d_uuid = decks.create_ephemeral(draft)
    promote(decks, driver, deck_uuid=d_uuid, to_name='Ozai')  # type: ignore[arg-type]

    # The promoted row is bound to the NEW record (rec00002), not rec00001.
    prow = decks.get_row(d_uuid)
    assert prow is not None
    ext = json.loads(prow.external_ids or '{}')
    assert ext.get('airtable') == 'rec00002'
    pdeck = decks.get(d_uuid)
    assert pdeck is not None
    assert sum(c.quantity for c in pdeck.cards) == 2

    # One ordinary edit + push must update rec00002, never touch rec00001.
    decks.add_card(d_uuid, DeckCard(name='My New Card'), rationale='user edit')
    driver.log.clear()
    push(decks, driver, deck_uuid=d_uuid)  # type: ignore[arg-type]
    assert any('rec00002' in c for c in driver.log)
    assert all('rec00001' not in c for c in driver.log)
    assert any(c.name == 'My New Card' for c in driver.records['rec00002'].cards)
    assert not any(c.name == 'My New Card' for c in driver.records['rec00001'].cards)


def test_correctly_bound_row_pushes_without_spurious_drift(data_dir: Path) -> None:
    """A row correctly bound to rec00002 must push to rec00002, never spuriously
    drift-refuse by comparing against a same-named rec00001."""
    decks = DecksStore()
    driver = MockAirtableDecks()
    driver.save_deck(commander_deck('Azula', filler=49))  # rec00001 unrelated dup
    driver.save_deck(commander_deck('Azula', filler=99))  # rec00002 the deck

    p_uuid = uuid4().hex
    src = driver.get_deck_by_record_id('rec00002')
    decks.put(src.model_copy(update={'uuid': p_uuid}), deck_uuid=p_uuid, sync_status='synced',
              source_ref='Azula', synced_baseline=version(src), rationale='pull')
    decks.set_external_id(p_uuid, 'airtable', 'rec00002')
    decks.add_card(p_uuid, DeckCard(name='My Edit'), rationale='edit')

    driver.log.clear()
    push(decks, driver, deck_uuid=p_uuid)  # type: ignore[arg-type]  # must NOT raise
    assert any('rec00002' in c for c in driver.log)
    assert any(c.name == 'My Edit' for c in driver.records['rec00002'].cards)
    assert not any(c.name == 'My Edit' for c in driver.records['rec00001'].cards)
