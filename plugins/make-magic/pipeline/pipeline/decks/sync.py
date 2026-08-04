"""Sync between the local ``DecksStore`` and a source-of-record ``CollectionStore``.

Two thin operations over the EXISTING source drivers (design §4) — no new I/O,
no new ceremony:

- :func:`pull` (source -> local): read the source deck, write it into the local
  store, stamp ``synced_baseline = version(source)``. Brings the local copy
  current.
- :func:`push` (local -> source), the GUARDED one:
    1. DRIFT CHECK — if the source moved since last sync (``version(current
       source) != synced_baseline``) AND that new source content is NOT already
       what we are about to write, refuse with :class:`SyncDriftError` (surface,
       don't clobber). The source's change is preserved (we do NOT save).
    2. Write THROUGH the existing ceremony: ``driver.save_deck`` — the shrink
       guard fires; its error surfaces unswallowed.
    3. Update ``synced_baseline = version(pushed deck)``.
- :func:`promote` — ephemeral -> synced: attach a ``source_ref``, mark synced,
  and push (create-through-ceremony).

The version primitive hashes the SAME canonical ``Deck`` read the same way on
both sides, so drift detection is correct even under name canonicalization (the
R3-B3 fix — see the sync tests' real canonicalizing resolver).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline.decks.version import version

if TYPE_CHECKING:
    from pipeline.collection.store import CollectionStore
    from pipeline.decks.store import DecksStore

__all__ = ('SyncDriftError', 'promote', 'pull', 'push')


class SyncDriftError(Exception):
    """The source moved since last sync — push refuses rather than clobber it.

    Raised by :func:`push` when ``version(current source) != synced_baseline`` and
    the source's new content is not already what we are about to write. The source
    of record is left untouched; the caller reconciles (re-pull, re-apply) before
    pushing again.
    """


def _require_row(decks: DecksStore, deck_uuid: str) -> tuple[object, str]:
    """Return ``(deck, source_ref)`` for a synced ``deck_uuid`` or raise a clear error."""
    from pipeline.decks.store import DecksError

    row = decks.get_row(deck_uuid)
    if row is None:
        raise DecksError(f'no deck with id {deck_uuid!r}')
    if row.source_ref is None:
        raise DecksError(f'deck {deck_uuid!r} has no source_ref (ephemeral); promote it before syncing')
    deck = decks.get(deck_uuid)
    if deck is None:
        raise DecksError(f'no deck_json for id {deck_uuid!r}')
    return deck, row.source_ref


def pull(decks: DecksStore, driver: CollectionStore, *, deck_uuid: str, source_ref: str) -> None:
    """Pull the source deck into the local store and stamp the baseline.

    ``driver.get_deck(source_ref)`` -> ``decks.put(..., sync_status='synced',
    source_ref=..., synced_baseline=version(source))``. Brings the local copy
    current with the source of record. Overwrites the local ``deck_json`` (a pull
    is "take the source's version"), preserving nothing local — the caller pulls
    deliberately (first access / explicit ``pull`` verb).
    """
    source = driver.get_deck(source_ref)
    # Stamp the stored deck's own ``uuid`` to the local PK so ``Deck.uuid`` is the
    # stable local identity (a source with a per-read/meaningless uuid — e.g. an
    # Airtable read minting a fresh one — must not leak into the local row's
    # identity). ``version`` excludes uuid, so the baseline hash is unaffected.
    stamped = source if source.uuid == deck_uuid else source.model_copy(update={'uuid': deck_uuid})
    decks.put(
        stamped,
        deck_uuid=deck_uuid,
        sync_status='synced',
        source_ref=source_ref,
        synced_baseline=version(stamped),
        rationale=f'pull from {source_ref}',
    )


def push(decks: DecksStore, driver: CollectionStore, *, deck_uuid: str, allow_shrink: bool = False) -> None:
    """Push the local deck to the source THROUGH the ceremony — drift-guarded.

    1. DRIFT: read the current source; if ``version(current source) !=
       synced_baseline`` the source moved. Allow ONLY when the current source
       already equals the local deck (``version(local) == version(current
       source)`` — our own prior write / idempotent re-push); otherwise raise
       :class:`SyncDriftError` WITHOUT saving (the source's change is preserved).
    2. ``driver.save_deck(local, allow_shrink=allow_shrink)`` — the shrink
       ceremony fires; any :class:`CollectionError` surfaces unswallowed.
    3. Update ``synced_baseline = version(local)``.
    """
    local, source_ref = _require_row(decks, deck_uuid)
    row = decks.get_row(deck_uuid)
    assert row is not None  # _require_row already validated it exists.

    local_version = version(local)  # type: ignore[arg-type]
    try:
        current_source = driver.get_deck(source_ref)
    except FileNotFoundError:
        # The source does not exist yet — this push CREATES it through the
        # ceremony (nothing to drift against). Skip the drift check.
        current_source = None
    current_source_version = version(current_source) if current_source is not None else None

    if (
        current_source_version is not None
        and current_source_version != row.synced_baseline
        and current_source_version != local_version
    ):
        raise SyncDriftError(
            f'refusing to push {deck_uuid!r}: the source {source_ref!r} moved since last sync '
            f'(baseline {row.synced_baseline}, now {current_source_version}). '
            'Re-pull and re-apply your change, then push again.'
        )

    driver.save_deck(local, allow_shrink=allow_shrink)  # type: ignore[arg-type]

    # Baseline := the source AS RE-READ after the write, so a later drift check
    # compares like-for-like (the ceremony may canonicalize names on save; the
    # baseline must be what ``get_deck`` reads back, not our pre-write local hash).
    written_version = version(driver.get_deck(source_ref))
    decks.put(
        local,  # type: ignore[arg-type]
        deck_uuid=deck_uuid,
        sync_status='synced',
        source_ref=source_ref,
        synced_baseline=written_version,
        rationale=f'push to {source_ref}',
    )


def promote(decks: DecksStore, driver: CollectionStore, *, deck_uuid: str, source_ref: str) -> None:
    """Promote an EPHEMERAL local deck to SYNCED: attach a source_ref + push.

    The ephemeral -> synced lifecycle (design §4): a clean-slate draft has no
    source; committing it attaches ``source_ref``, marks it synced, and pushes
    (create-through-ceremony). The source may not exist yet, so there is nothing
    to drift against — we set the baseline to the ephemeral deck's own version so
    the immediate push is treated as our own write (no false drift).
    """
    from pipeline.decks.store import DecksError

    deck = decks.get(deck_uuid)
    if deck is None:
        raise DecksError(f'no deck with id {deck_uuid!r}')
    # The source of record is keyed by deck NAME, so committing an exploration draft
    # back onto a differently-named target (e.g. 'Gruul (explore)' --to 'Gruul') must
    # save the content UNDER the target name — otherwise the save creates a new source
    # deck instead of landing on the original. version() excludes the name, so the
    # baseline hashes are unaffected by the rename.
    to_source = deck if deck.name == source_ref else deck.model_copy(update={'name': source_ref})
    # Attach the source_ref + mark synced; baseline = our own version so the push
    # below sees "source == local" (or a not-yet-existing source) and proceeds.
    decks.put(
        deck,
        deck_uuid=deck_uuid,
        sync_status='synced',
        source_ref=source_ref,
        synced_baseline=version(deck),
        rationale=f'promote -> {source_ref}',
    )
    driver.save_deck(to_source, allow_shrink=False)
    # Re-stamp against the freshly-written source so the baseline reflects the
    # ceremony's canonical read (create-through-ceremony may canonicalize).
    written = driver.get_deck(source_ref)
    decks.put(
        deck,
        deck_uuid=deck_uuid,
        sync_status='synced',
        source_ref=source_ref,
        synced_baseline=version(written),
        rationale=f'promote committed -> {source_ref}',
    )
