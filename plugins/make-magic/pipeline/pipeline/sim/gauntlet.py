"""Resolve an opponent set (a "gauntlet") for AI-vs-AI simulation.

A gauntlet is just a list of :class:`GauntletDeck` — a ``(name, dck_text)`` pair
per opponent — that :mod:`pipeline.sim.core` runs a candidate deck against. Two
hybrid sources feed it:

  * **curated** — a MODEST, ROBUST v1 set of ``.dck`` files that ship as plugin
    data under ``pipeline/data/gauntlet/<constructed|commander>/``. Every deck is
    built from time-tested Forge staples (basics + classic commons) so it loads
    cleanly in Forge 2.0.13. Loaded straight off disk (no network, no creds).
  * **mine** — the user's OWN decks, pulled from the collection
    :class:`~pipeline.collection.CollectionStore` (``get_deck`` -> ``Deck``) and
    rendered to ``.dck`` via the Forge exporter. This path is LIVE (Airtable
    needs creds); it is structured here and unit-tested with a MOCKED store, and
    only exercised against the real base under ``-m live`` / ``-m forge``.

``both`` merges curated + mine. :func:`resolve_gauntlet` is the single entry
point; a bad ``source`` (or ``mine`` without a store) is a loud ``ValueError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING

from pipeline.destinations.deck_export import get_exporter

if TYPE_CHECKING:
    import os

    from pipeline.collection import CollectionStore

__all__ = (
    'GauntletDeck',
    'resolve_gauntlet',
)

#: Valid gauntlet sources.
_SOURCES = ('curated', 'mine', 'both')
#: The commander format label (everything else is treated as constructed).
_COMMANDER = 'commander'


@dataclass(frozen=True)
class GauntletDeck:
    """One opponent in a gauntlet: a name + already-rendered Forge ``.dck`` text.

    This is exactly the ``(name, dck_text)`` shape
    :func:`~pipeline.sim.runner.run_matchup` consumes, so a gauntlet drops
    straight into the sim core without further rendering.
    """

    name: str
    dck_text: str


def _fmt_dir(fmt: str) -> str:
    """The curated sub-dir for ``fmt`` (only ``constructed`` / ``commander`` ship).

    Returns ``fmt`` verbatim so an unshipped format (e.g. ``pauper``) points at a
    non-existent dir and :func:`_curated` yields ``[]`` — no silent fall-through
    to the constructed pool for a format we don't curate.
    """
    return fmt


def _curated(fmt: str) -> list[GauntletDeck]:
    """Load every bundled ``.dck`` for ``fmt`` from the packaged gauntlet data.

    Read via :mod:`importlib.resources` so it works from an installed wheel or an
    editable checkout. An absent format dir (e.g. an unknown format) yields ``[]``
    rather than raising — a caller asking for a format we don't ship just gets no
    curated opponents.
    """
    fmt_dir = _fmt_dir(fmt)
    root = resources.files('pipeline') / 'data' / 'gauntlet' / fmt_dir
    if not root.is_dir():
        return []

    decks: list[GauntletDeck] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name.endswith('.dck'):
            decks.append(GauntletDeck(name=entry.name[: -len('.dck')], dck_text=entry.read_text()))
    return decks


def _mine(fmt: str, store: CollectionStore) -> list[GauntletDeck]:
    """Render the user's own decks (from ``store``) into gauntlet opponents.

    Pulls every deck via ``store.list_decks`` (name-only) then ``get_deck`` (fully
    hydrated) and renders each to ``.dck`` with the Forge exporter. Filters to the
    requested format: commander -> decks WITH commanders; anything else ->
    non-commander decks. A deck whose format can't be matched is simply skipped.
    """
    exporter = get_exporter('forge_dck')
    want_commander = fmt == _COMMANDER

    decks: list[GauntletDeck] = []
    for stub in store.list_decks():
        deck = store.get_deck(stub.name)
        is_commander = bool(deck.commanders) or (deck.format or '').strip().lower() in (
            'commander',
            'edh',
        )
        if is_commander != want_commander:
            continue
        decks.append(GauntletDeck(name=deck.name, dck_text=exporter.export(deck)))
    return decks


def resolve_gauntlet(
    source: str,
    fmt: str,
    *,
    store: CollectionStore | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> list[GauntletDeck]:
    """Resolve the opponent set for ``source`` + ``fmt`` into gauntlet decks.

    ``source`` is one of ``curated`` / ``mine`` / ``both``. ``curated`` loads the
    bundled ``.dck`` data; ``mine`` renders the user's decks from ``store`` (which
    is REQUIRED for ``mine`` / ``both``); ``both`` merges the two (curated first,
    then mine). ``fmt`` selects the constructed vs commander pool. ``data_dir`` is
    accepted for symmetry with the rest of the sim API (the curated data ships
    with the package, so it is currently unused) — reserved for a future
    on-disk override.

    Raises ``ValueError`` for an unknown ``source`` or when ``mine`` / ``both`` is
    requested without a ``store``.
    """
    del data_dir  # reserved: curated data is packaged, not read from a data dir.

    if source not in _SOURCES:
        raise ValueError(f'unknown gauntlet source {source!r}; choose from {_SOURCES}')

    decks: list[GauntletDeck] = []
    if source in ('curated', 'both'):
        decks.extend(_curated(fmt))
    if source in ('mine', 'both'):
        if store is None:
            raise ValueError(f'gauntlet source {source!r} needs a CollectionStore (store=...)')
        decks.extend(_mine(fmt, store))
    return decks
