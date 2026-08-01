"""Resolve an opponent set (a "gauntlet") for AI-vs-AI simulation.

A gauntlet is just a list of :class:`GauntletDeck` — a ``(name, dck_text)`` pair
per opponent — that :mod:`pipeline.sim.core` runs a candidate deck against.
Several sources feed it:

  * **curated** — a MODEST, ROBUST v1 set of ``.dck`` files that ship as plugin
    data under ``pipeline/data/gauntlet/<constructed|commander>/``. Every deck is
    built from time-tested Forge staples (basics + classic commons) so it loads
    cleanly in Forge 2.0.13. Loaded straight off disk (no network, no creds).
  * **named bundles** — an opt-in tier set shipped as a SUB-directory of the
    format dir (e.g. ``.../constructed/guilds/`` — the 10-guild x weak/mid/strong
    40-card matrix). ``resolve_gauntlet('guilds', 'constructed')`` loads it; the
    default ``curated`` bundle (the flat ``.dck`` files at the format root) is
    unaffected, so bundles are strictly additive and runs opt in by name.
    :func:`gauntlet_sources` enumerates the valid ``source`` values for a format
    (the core sources + whatever bundle dirs ship).
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
    from importlib.resources.abc import Traversable

    from pipeline.collection import CollectionStore

__all__ = (
    'GauntletDeck',
    'gauntlet_sources',
    'resolve_gauntlet',
)

#: Core gauntlet sources (named bundles extend this per format — see
#: :func:`gauntlet_sources`).
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


def _gauntlet_root(fmt: str) -> Traversable:
    """The packaged gauntlet dir for ``fmt`` (may not exist for an unknown format)."""
    return resources.files('pipeline') / 'data' / 'gauntlet' / _fmt_dir(fmt)


def _load_dck_dir(root: Traversable) -> list[GauntletDeck]:
    """Load every ``.dck`` DIRECTLY under ``root`` into ``GauntletDeck``s (sorted).

    Read via :mod:`importlib.resources` so it works from an installed wheel or an
    editable checkout. A non-existent ``root`` yields ``[]`` (never raises).
    Sub-directories are skipped — they are named bundles loaded on request, not
    part of the flat set — so a bundle dir never leaks into the default pool.
    """
    if not root.is_dir():
        return []

    decks: list[GauntletDeck] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name.endswith('.dck'):
            decks.append(GauntletDeck(name=entry.name[: -len('.dck')], dck_text=entry.read_text()))
    return decks


def _curated(fmt: str) -> list[GauntletDeck]:
    """Load the default curated bundle: the flat ``.dck`` files at the format root."""
    return _load_dck_dir(_gauntlet_root(fmt))


def _bundle_names(fmt: str) -> tuple[str, ...]:
    """Discover the named bundles that ship for ``fmt`` (sub-dirs of the format root).

    Each sub-directory holding ``.dck`` files is a named bundle whose directory
    name is the ``source`` that selects it. An absent/empty format root yields
    ``()``.
    """
    root = _gauntlet_root(fmt)
    if not root.is_dir():
        return ()
    return tuple(sorted(entry.name for entry in root.iterdir() if entry.is_dir()))


def _bundle(fmt: str, name: str) -> list[GauntletDeck]:
    """Load a named bundle's ``.dck`` files from ``<format root>/<name>/``."""
    return _load_dck_dir(_gauntlet_root(fmt) / name)


def gauntlet_sources(fmt: str) -> tuple[str, ...]:
    """All valid ``source`` values for ``fmt``: the core sources + named bundles.

    Used by the CLI to present ``--gauntlet`` choices and by
    :func:`resolve_gauntlet` to validate a requested source.
    """
    return (*_SOURCES, *_bundle_names(fmt))


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

    ``source`` is one of ``curated`` / ``mine`` / ``both``, or the name of a
    packaged bundle for ``fmt`` (e.g. ``guilds``; see :func:`gauntlet_sources`).
    ``curated`` loads the default flat bundle; a named bundle loads its sub-dir
    (and is standalone — not merged with ``mine``); ``mine`` renders the user's
    decks from ``store`` (which is REQUIRED for ``mine`` / ``both``); ``both``
    merges curated + mine (curated first). ``fmt`` selects the constructed vs
    commander pool. ``data_dir`` is accepted for symmetry with the rest of the
    sim API (the curated data ships with the package, so it is currently unused)
    — reserved for a future on-disk override.

    Raises ``ValueError`` for a source that is neither a core source nor a bundle
    shipped for ``fmt``, or when ``mine`` / ``both`` is requested without a
    ``store``.
    """
    del data_dir  # reserved: curated data is packaged, not read from a data dir.

    valid = gauntlet_sources(fmt)
    if source not in valid:
        raise ValueError(f'unknown gauntlet source {source!r} for format {fmt!r}; choose from {valid}')

    decks: list[GauntletDeck] = []
    if source in ('curated', 'both'):
        decks.extend(_curated(fmt))
    elif source not in _SOURCES:  # a named bundle (already validated above)
        decks.extend(_bundle(fmt, source))
    if source in ('mine', 'both'):
        if store is None:
            raise ValueError(f'gauntlet source {source!r} needs a CollectionStore (store=...)')
        decks.extend(_mine(fmt, store))
    return decks
