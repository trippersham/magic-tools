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
- :func:`promote` — ephemeral -> synced, redesigned around LINEAGE (design §4):
  an exploration draft (``derived_from`` set) commits onto its PARENT's external
  ref and is then CONSUMED; a clean-slate draft (``derived_from`` NULL) CREATES a
  new source deck (dup names allowed — never bind-by-name to an existing deck).
  Both save THROUGH the ceremony FIRST and flip state only on success (M6).
- :func:`sync_reconcile` — reconcile-not-destroy (M2): compares local /
  ``synced_baseline`` / current source and picks pull / push / drift-error so an
  unpushed local edit is never silently clobbered.

The version primitive hashes the SAME canonical ``Deck`` read the same way on
both sides, so drift detection is correct even under name canonicalization (the
R3-B3 fix — see the sync tests' real canonicalizing resolver).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline.decks.version import version

if TYPE_CHECKING:
    from pipeline.collection.store import CollectionStore
    from pipeline.contracts import Deck
    from pipeline.decks.store import DecksStore

__all__ = ('SyncDriftError', 'promote', 'pull', 'push', 'sync_reconcile')


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

    # Land ON the existing source's FILE: stamp the local content with the SOURCE's
    # in-file uuid so the slug-collision guard writes back to the SAME file rather
    # than disambiguating to a new one ("same external ref = same deck", design §4).
    # version() excludes uuid, so the baseline hashes are unaffected. A not-yet-
    # existing source keeps the local uuid (this push CREATES it).
    to_save = local  # type: ignore[assignment]
    if current_source is not None and current_source.uuid and current_source.uuid != local.uuid:  # type: ignore[union-attr]
        to_save = local.model_copy(update={'uuid': current_source.uuid})  # type: ignore[union-attr]
    driver.save_deck(to_save, allow_shrink=allow_shrink)  # type: ignore[arg-type]

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


def promote(
    decks: DecksStore, driver: CollectionStore, *, deck_uuid: str, to_name: str | None = None
) -> None:
    """Commit an EPHEMERAL draft — by LINEAGE, save-before-flip, then consume (design §4).

    Decided by the draft's ``derived_from``:

    - **Exploration draft** (``derived_from`` set): commit its content onto the
      PARENT's source, bound by the parent row's EXTERNAL REF (not its name), via
      the drift-guarded ``push`` ceremony. On success REFRESH the parent's canonical
      local row and CONSUME the draft (``consumed`` + archived). Net: the parent row
      is the single synced row for that ref (≤1-synced-row-per-ref); the draft is
      retired. ``to_name`` is ignored (the target is the parent, by lineage).
    - **Clean-slate draft** (``derived_from`` NULL): CREATE a NEW source deck named
      ``to_name`` (mint a fresh external ref = the draft's own uuid). A deck with
      that name may already exist — that is FINE (dup names, uuid identity); we
      create a SEPARATE deck and NEVER bind-by-name to an existing one. The draft's
      OWN row becomes the synced deck (it IS the deck now — not consumed).

    Save-before-flip (M6): ``driver.save_deck`` runs FIRST; state flips only after
    the write succeeds. If the ceremony refuses (e.g. a shrink), the draft stays a
    clean ephemeral draft — no half-synced zombie.
    """
    from pipeline.decks.store import DecksError

    draft = decks.get(deck_uuid)
    if draft is None:
        raise DecksError(f'no deck with id {deck_uuid!r}')
    row = decks.get_row(deck_uuid)
    assert row is not None  # get() already proved the row exists.
    if row.sync_status == 'consumed':
        raise DecksError(
            f'{row.name!r} is a consumed draft (already promoted) and cannot be promoted again'
        )

    if row.derived_from:
        _promote_exploration(decks, driver, deck_uuid=deck_uuid, draft=draft, parent_uuid=row.derived_from)
    else:
        _promote_clean_slate(decks, driver, deck_uuid=deck_uuid, draft=draft, to_name=to_name or draft.name)


def _promote_exploration(
    decks: DecksStore, driver: CollectionStore, *, deck_uuid: str, draft: Deck, parent_uuid: str
) -> None:
    """Commit an exploration draft onto its parent's ref, then consume it (design §4)."""
    from pipeline.decks.store import DecksError

    parent_row = decks.get_row(parent_uuid)
    if parent_row is None or parent_row.source_ref is None:
        raise DecksError(
            f'cannot promote exploration draft {deck_uuid!r}: its parent {parent_uuid!r} '
            'is not a synced source deck (was it consumed or never synced?)'
        )
    source_ref = parent_row.source_ref
    parent_deck = decks.get(parent_uuid)
    assert parent_deck is not None

    # The content to land = the draft's cards/metadata, but stamped with the PARENT's
    # identity (uuid + name) so the save binds to the PARENT's source file/ref — not
    # the draft's name. version() excludes uuid + name, so the baseline is unaffected.
    to_source = draft.model_copy(update={'uuid': parent_deck.uuid, 'name': parent_deck.name})

    # DRIFT GUARD against the parent's current source (reuse the push ceremony's
    # semantics): if the source moved since the parent's baseline AND the new content
    # is not already what we would write, refuse WITHOUT saving (M6 leaves the draft
    # a clean ephemeral draft — no state was flipped).
    try:
        current_source = driver.get_deck(source_ref)
    except FileNotFoundError:
        current_source = None
    current_version = version(current_source) if current_source is not None else None
    to_source_version = version(to_source)
    if (
        current_version is not None
        and current_version != parent_row.synced_baseline
        and current_version != to_source_version
    ):
        raise SyncDriftError(
            f'refusing to promote onto {source_ref!r}: it moved since last sync '
            f'(baseline {parent_row.synced_baseline}, now {current_version}). '
            'Re-pull the parent and rebuild the draft, then promote again.'
        )

    # SAVE FIRST (M6) — any shrink/ceremony refusal surfaces here, before any flip.
    driver.save_deck(to_source, allow_shrink=False)

    # Success: refresh the parent's canonical local row + baseline (bound by ref),
    # then CONSUME the draft (retire the exploration copy).
    written = _reread_source(driver, source_ref, to_source.uuid)
    decks.put(
        written,
        deck_uuid=parent_uuid,
        sync_status='synced',
        source_ref=source_ref,
        synced_baseline=version(written),
        rationale=f'promote (exploration {deck_uuid[:6]}) -> {source_ref}',
    )
    decks.consume(deck_uuid)


def _promote_clean_slate(
    decks: DecksStore, driver: CollectionStore, *, deck_uuid: str, draft: Deck, to_name: str
) -> None:
    """Create a NEW source deck for a clean-slate draft; the draft's row becomes synced."""
    # A clean-slate promote CREATES a fresh source deck — a fresh external ref (the
    # draft's own uuid) that never binds to an existing same-named deck. Save the
    # draft under ``to_name`` FIRST (M6). The slug-collision guard in the local
    # adapter makes a dup name land on its own file rather than clobber the existing.
    to_source = draft if draft.name == to_name else draft.model_copy(update={'name': to_name})
    driver.save_deck(to_source, allow_shrink=False)

    written = _reread_source(driver, to_name, to_source.uuid)
    decks.put(
        written,
        deck_uuid=deck_uuid,
        sync_status='synced',
        source_ref=to_name,
        synced_baseline=version(written),
        rationale=f'promote (clean-slate) -> {to_name}',
    )


def _reread_source(driver: CollectionStore, source_ref: str, expected_uuid: str) -> Deck:
    """Re-read a just-written source deck, preferring the uuid-bound read when available.

    A dup name means a name read is ambiguous, so re-read by the deck's in-file uuid
    when the driver supports it (``get_deck_by_uuid`` — local YAML) so the baseline
    reflects THIS deck, not a same-named sibling. Falls back to the name read.
    """
    by_uuid = getattr(driver, 'get_deck_by_uuid', None)
    if by_uuid is not None:
        try:
            return by_uuid(expected_uuid)
        except FileNotFoundError:
            pass
    return driver.get_deck(source_ref)


def sync_reconcile(
    decks: DecksStore, driver: CollectionStore, *, deck_uuid: str, allow_shrink: bool = False
) -> None:
    """Reconcile a synced deck against its source — pull / push / drift (design §4, M2).

    Compare ``version(local)`` / ``synced_baseline`` / ``version(current source)``:

    - only SOURCE moved (``local == baseline``) → PULL (take the source's version).
    - only LOCAL moved (``source == baseline``) → PUSH (send the local edit).
    - neither moved (all equal) → no-op.
    - BOTH moved (``local != baseline`` AND ``source != baseline`` AND
      ``local != source``) → raise :class:`SyncDriftError`; change NOTHING (both
      preserved). Never pull-clobber an unpushed local edit.
    """
    local, source_ref = _require_row(decks, deck_uuid)
    row = decks.get_row(deck_uuid)
    assert row is not None

    local_version = version(local)  # type: ignore[arg-type]
    try:
        source = driver.get_deck(source_ref)
    except FileNotFoundError:
        source = None
    source_version = version(source) if source is not None else None
    baseline = row.synced_baseline

    # Nothing on the source yet, or all in agreement → push (create) / no-op.
    if source_version is None or (source_version == baseline and local_version == baseline):
        if source_version is None or local_version != baseline:
            push(decks, driver, deck_uuid=deck_uuid, allow_shrink=allow_shrink)
        return

    local_moved = local_version != baseline
    source_moved = source_version != baseline

    if source_moved and not local_moved:
        pull(decks, driver, deck_uuid=deck_uuid, source_ref=source_ref)
        return
    if local_moved and not source_moved:
        push(decks, driver, deck_uuid=deck_uuid, allow_shrink=allow_shrink)
        return
    if local_moved and source_moved and local_version != source_version:
        raise SyncDriftError(
            f'refusing to sync {deck_uuid!r}: BOTH local and source {source_ref!r} moved since '
            f'last sync (baseline {baseline}; local {local_version}, source {source_version}). '
            'Nothing changed — reconcile by hand (re-pull into a --from draft, re-apply, promote).'
        )
    # local_version == source_version (both converged to the same content): adopt it
    # as the new baseline via an idempotent push (our own write, no clobber).
    push(decks, driver, deck_uuid=deck_uuid, allow_shrink=allow_shrink)
