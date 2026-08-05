"""One-shot `copy_collection` — read every record from one backend, write to the
other via the domain-typed `CollectionStore` port.

This is the interop bridge behind the ``collection copy`` verb. It is a one-shot
migration, not a live sync: it reads every record (inventory, decks, chase,
trades) through the source adapter and writes it through the destination
adapter, so the copy is transport-agnostic (local YAML <-> Airtable records) and
keys on the domain identity — card/deck names and the ``oracle_id`` /
``airtable_record_id`` round-trip identity carried on the contracts — rather than
any backend-specific row shape.

Ordering matters because Airtable link fields are record ids: a deck's
Commander/Cards links, a chase card's Target Decks link, and a trade's card/deck
links can only resolve to ids for rows that already exist. The copy therefore
runs inventory -> decks -> chase -> trades so every referenced row is present
before a link that points at it is written.

Writing to Airtable requires a ``writes_enabled=True`` destination store (the
adapter's guarded-write ethos); the ``collection copy`` verb additionally gates
any ``--to airtable`` behind an explicit ``--confirm`` so a live production write
is never accidental. This module does not itself know about ``--confirm`` — it
just drives the port; the guard lives at the CLI edge + the adapter write guard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from pipeline.collection.store import CollectionStore


class CopyReport(BaseModel):
    """How many records of each kind the copy wrote to the destination."""

    model_config = ConfigDict(extra='forbid')

    inventory: int = Field(default=0, description='Inventory cards copied.')
    decks: int = Field(default=0, description='Decks copied.')
    chase: int = Field(default=0, description='Chase cards copied.')
    trades: int = Field(default=0, description='Trades copied.')


def copy_collection(source: CollectionStore, dest: CollectionStore) -> CopyReport:
    """Copy every record from ``source`` to ``dest`` (one-shot, not a live sync).

    Runs inventory -> decks -> chase -> trades so link targets exist before the
    links that reference them are written. Returns a :class:`CopyReport` with the
    per-kind counts written.

    ``dest`` must permit writes (an Airtable destination requires
    ``writes_enabled=True``); the first mutating call otherwise raises the
    adapter's read-only guard.
    """
    report = CopyReport()

    # --- inventory (must precede decks/trades — link targets) ----------------- #
    for card in source.list_inventory():
        dest.add_card(
            card.name,
            card.owned,
            condition=card.condition or None,
            foil=card.foil,
            sets=card.sets or None,
            sources=card.sources or None,
        )
        report.inventory += 1

    # --- decks (must precede chase/trades — Target Decks / From/To (Deck)) ----- #
    for deck in source.list_decks():
        # Drop the source record id so the destination creates a fresh row rather
        # than trying to PATCH an id that only exists in the source base.
        dest.save_deck(deck.model_copy(update={'airtable_record_id': None}))
        report.decks += 1

    # --- chase ---------------------------------------------------------------- #
    for chase in source.list_chase():
        first_deck = chase.for_decks[0] if chase.for_decks else None
        dest.add_chase(
            chase.name,
            for_deck=first_deck,
            priority=chase.priority,
            status=chase.status,
            target_price=chase.target_price,
        )
        # Any additional target decks beyond the first are appended.
        for extra_deck in chase.for_decks[1:]:
            dest.add_chase(chase.name, for_deck=extra_deck)
        report.chase += 1

    # --- trades --------------------------------------------------------------- #
    for trade in source.list_trades():
        dest.log_trade(trade.model_copy(update={'airtable_record_id': None}))
        report.trades += 1

    return report
