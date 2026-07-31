"""Backend-agnostic deck-integrity guard helpers (Phase 4 — PREVENTION).

Pure functions over the :class:`~pipeline.collection.store.CollectionStore` port
and the `Deck`/`DeckCard` contracts — no CLI, no Airtable, no I/O beyond the
`store.list_decks()` / `store.get_deck()` reads the caller already has. These are
the primitives the CLI (and a defensive `save_deck`) use to make deleting a
LINKED inventory row, and shrinking a deck UNDER its target, a conscious act.

The incident this prevents: a naive ``remove_card`` HARD-DELETED a shared
Inventory row that many decks linked; Airtable's link cascade silently stripped
that card from EVERY deck at once, dropping several below 100. :func:`remove_impact`
enumerates exactly that blast radius before a delete; :func:`shrink_check` catches
a save that drops a deck currently AT target below it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from pipeline.collection.store import CollectionStore
    from pipeline.contracts import Deck

__all__ = ('DeckImpact', 'check_remove_allowed', 'decks_linking', 'remove_impact', 'shrink_check')


class DeckImpact(BaseModel):
    """The effect of removing one card ref on a single deck.

    Produced by :func:`remove_impact`, one per deck that links the ref, so the CLI
    can enumerate the full cascade blast radius before a delete.
    """

    model_config = ConfigDict(extra='forbid')

    deck_name: str = Field(description='Name of the affected deck.')
    current_size: int = Field(description='Deck size now (Σ card quantities).')
    target: int | None = Field(description='Deck target size (None = untargeted).')
    size_after: int = Field(description='Deck size after the ref is unlinked (current_size - qty of ref).')
    goes_under_target: bool = Field(
        description='True iff target is set AND size_after drops below it — the dangerous case.'
    )


def _deck_size(deck: Deck) -> int:
    """Σ card quantities — the deck's current total size."""
    return sum(c.quantity for c in deck.cards)


def _ref_quantity(deck: Deck, ref: str) -> int:
    """Σ quantity of cards in ``deck`` whose name matches ``ref`` (case-insensitive)."""
    needle = ref.strip().casefold()
    return sum(c.quantity for c in deck.cards if c.name.strip().casefold() == needle)


def decks_linking(store: CollectionStore, ref: str) -> list[Deck]:
    """Every deck whose cards (commander included) include a card matching ``ref``.

    Matches on card NAME, case-insensitively (whitespace-trimmed). An O(decks)
    scan via ``store.list_decks()`` — the name-only list read is enough (names,
    roles, quantities), no per-card hydration.

    Args:
        store: The collection store to scan.
        ref: The card name to match against every deck's cards.

    Returns:
        The decks that link ``ref``, in ``list_decks()`` order.
    """
    needle = ref.strip().casefold()
    return [deck for deck in store.list_decks() if any(c.name.strip().casefold() == needle for c in deck.cards)]


def remove_impact(store: CollectionStore, ref: str) -> list[DeckImpact]:
    """Compute the per-deck impact of removing (unlinking) ``ref`` from the base.

    One :class:`DeckImpact` per deck that links ``ref``. ``size_after`` subtracts
    that deck's quantity of ``ref``; ``goes_under_target`` is True only when the
    deck has a target AND ``size_after`` falls below it — the case that would
    silently break a legal deck via Airtable's link cascade.

    Args:
        store: The collection store to scan.
        ref: The card name whose removal impact is being measured.

    Returns:
        A :class:`DeckImpact` list (empty when ``ref`` links no deck).
    """
    impacts: list[DeckImpact] = []
    for deck in decks_linking(store, ref):
        current = _deck_size(deck)
        after = current - _ref_quantity(deck, ref)
        target = deck.target_size
        impacts.append(
            DeckImpact(
                deck_name=deck.name,
                current_size=current,
                target=target,
                size_after=after,
                goes_under_target=target is not None and after < target,
            )
        )
    return impacts


def check_remove_allowed(store: CollectionStore, ref: str, *, force: bool) -> None:
    """Port-level cascade guard shared by every adapter's ``remove_card``.

    Enumerates the decks that link ``ref`` (via :func:`remove_impact`); if any do
    and ``force`` is False, raises :class:`~pipeline.collection.errors.CollectionError`
    naming each affected deck (and flagging those that drop UNDER target) BEFORE
    any delete. When ``force`` is True, or ``ref`` links no deck, it returns
    quietly and the caller may delete. This is the single place the abort message
    is built, so both adapters (and the CLI, which pre-checks the same way) agree.

    Args:
        store: The collection store whose decks are scanned.
        ref: The card name about to be hard-deleted.
        force: When True, skip the guard (caller has confirmed the cascade).

    Raises:
        CollectionError: When ``ref`` is linked to >=1 deck and ``force`` is False.
    """
    if force:
        return
    impacts = remove_impact(store, ref)
    if not impacts:
        return
    # Local import: keep guards.py free of an import-time errors dependency and
    # avoid any risk of a cycle (errors is tiny, but this is the only raise site).
    from pipeline.collection.errors import CollectionError

    lines = [
        f"remove_card '{ref}' is LINKED to {len(impacts)} deck(s) — deleting the shared "
        'Inventory row would cascade it out of ALL of them at once:'
    ]
    for imp in impacts:
        flag = '  ** DROPS UNDER TARGET **' if imp.goes_under_target else ''
        target = imp.target if imp.target is not None else '—'
        lines.append(f'  - {imp.deck_name}: {imp.current_size} -> {imp.size_after} (target {target}){flag}')
    lines.append('Pass force=True to remove it anyway (this will unlink it from every deck above).')
    raise CollectionError('\n'.join(lines))


def shrink_check(prior_deck: Deck, new_deck: Deck) -> bool:
    """True iff a save DROPS a deck that currently MEETS its target below it.

    The precise rule — deliberately narrow to avoid false positives while a deck
    is being BUILT up:

    - the deck must have a target (``target_size`` not None), AND
    - it must currently MEET target (``prior_size >= target``), AND
    - the incoming save must drop it BELOW (``new_size < target``).

    So a build (0 -> 99, prior never met target) never triggers, and a deck
    already UNDER target (prior_size < target) is NOT re-guarded here (its removals
    are still enumerated by :func:`remove_impact`). Only a deck that is legal NOW
    and would become illegal by this save is flagged.

    Args:
        prior_deck: The deck as it exists BEFORE the save.
        new_deck: The incoming deck being saved.

    Returns:
        True when the save shrinks an at-target deck under target; else False.
    """
    target = prior_deck.target_size
    if target is None:
        return False
    return _deck_size(prior_deck) >= target and _deck_size(new_deck) < target
