"""Sync between the local ``DecksStore`` and a source-of-record ``CollectionStore``.

Two thin operations over the existing source drivers — no new I/O, no new ceremony:

- :func:`pull` (source -> local): read the source deck, write it into the local
  store, stamp ``synced_baseline = version(source)``. Brings the local copy
  current.
- :func:`push` (local -> source), the guarded one:
    1. Drift check — if the source moved since last sync (``version(current
       source) != synced_baseline``) and that new source content is not already
       what we are about to write, refuse with :class:`SyncDriftError` (surface,
       don't clobber). The source's change is preserved (no save happens).
    2. Write through the existing ceremony: ``driver.save_deck`` — the shrink
       guard fires; its error surfaces unswallowed.
    3. Update ``synced_baseline = version(pushed deck)``.
- :func:`promote` — ephemeral -> synced, by lineage: an exploration draft
  (``derived_from`` set) commits onto its parent's external ref and is then
  consumed; a clean-slate draft (``derived_from`` NULL) creates a new source deck
  (dup names allowed — never bind-by-name to an existing deck). Both save through
  the ceremony first and flip state only on success.
- :func:`sync_reconcile` — reconcile, not destroy: compares local /
  ``synced_baseline`` / current source and picks pull / push / drift-error so an
  unpushed local edit is never silently clobbered.

The version primitive hashes the same canonical ``Deck`` read the same way on
both sides, so drift detection is correct even under name canonicalization.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Literal

from pipeline.decks.version import version

if TYPE_CHECKING:
    from pipeline.collection.store import CollectionStore
    from pipeline.contracts import Deck
    from pipeline.decks.store import DecksStore

__all__ = (
    'CrossBackendBindingError',
    'DeadBindingError',
    'SyncDriftError',
    'binding_is_dead',
    'cross_backend_message',
    'dead_binding_message',
    'promote',
    'pull',
    'push',
    'read_bound_source',
    'require_writable_binding',
    'sync_reconcile',
)


class SyncDriftError(Exception):
    """The source moved since last sync — push refuses rather than clobber it.

    Raised by :func:`push` when ``version(current source) != synced_baseline`` and
    the source's new content is not already what we are about to write. The source
    of record is left untouched; the caller reconciles (re-pull, re-apply) before
    pushing again.
    """


class CrossBackendBindingError(Exception):
    """A write targeted a row whose source lives on a different backend.

    A row with an external ref for another backend but none for the active one is
    not a first push — its source of record exists, just elsewhere. Treating it as
    a create would adopt (and overwrite) an unrelated same-named file on this
    backend. Reads and writes both refuse with the same message: switch back to
    the source backend. Distinct from :class:`DeadBindingError` (bound here, but
    the source is gone) and from a genuine first push (no ref anywhere → create).
    """


class DeadBindingError(Exception):
    """The row is bound for the active backend but its source read came back None.

    A row carrying an ``external_ids`` ref for the active backend whose
    :func:`read_bound_source` returns None means the bound file/record was deleted
    or re-identified (an in-place legacy restore, an out-of-band delete, a
    hand-edited in-file uuid). A write must not read that None as "first push,
    nothing to drift against" and silently create / adopt an existing slug file /
    fork — that destroys or buries the out-of-band content. Edit/sync verbs refuse
    with this instead.

    The one recovery is a fresh-identity ``save-deck`` of the deck: it writes a
    brand-new source at a fresh identity and rebinds the row. The refusal message
    names only that — it does not tell the user to ``pull`` (a dead ref cannot be
    re-bound by pull — the chokepoint returns None by design). See
    :func:`dead_binding_message` for the backend-aware wording.

    Distinct from a genuine first push: a row with no bound ref for the active
    backend read back as None is "not yet created" and still creates.
    """


def _active_backend(driver: CollectionStore) -> Literal['local', 'airtable']:
    """The active source backend as a literal — ``'airtable'`` or ``'local'``."""
    return 'airtable' if driver.backend_name == 'airtable' else 'local'


def binding_is_dead(decks: DecksStore, driver: CollectionStore, *, deck_uuid: str, source_ref: str) -> bool:
    """True iff the row is bound for the active backend but the bound source read is None.

    Splits the two meanings of ``read_bound_source(...) is None``: "None because the
    source was never created" (no bound ref → a genuine first push, create is correct)
    vs "None because the bound ref is dead" (a ref exists but its file/record no longer
    matches it → refuse). The caller knows whether the row had a ref; this asks the
    store + chokepoint for it.
    """
    backend = _active_backend(driver)
    bound = decks.external_ref(deck_uuid, backend)
    if not bound:
        return False  # no bound ref for the active backend → never created, not dead.
    source = read_bound_source(decks, driver, deck_uuid=deck_uuid, source_ref=source_ref)
    return source is None


def require_writable_binding(
    decks: DecksStore,
    driver: CollectionStore,
    *,
    deck_uuid: str,
    source_ref: str,
) -> None:
    """Refuse a source write against a dead binding — the write-side chokepoint.

    Called at the top of every source edit/sync write (:func:`push`, :func:`promote`,
    the ``set-*``/``deck-*``/``undo`` commit path). When the row is bound for the active
    backend but the bound source is gone/re-identified, raise :class:`DeadBindingError`
    rather than let the write fall through to a silent create/adopt/fork.

    There is no ``recreate`` override here. The one recovery is a fresh-identity
    ``save-deck``, handled in ``access.save_deck`` (which creates a brand-new source at
    a fresh identity and rebinds the row). The error names only ``save-deck``
    (:func:`dead_binding_message`).
    """
    # A row bound on a different backend than the active one is not a genuine first
    # push (it has a source, just elsewhere) — refuse with the same "switch back to its
    # source backend" the read already emits, before the dead-binding check (which
    # would read "no ref for the active backend → never created" and let the create
    # fall through to adopt a same-named file in place).
    active = _active_backend(driver)
    if (decks.bound_backends(deck_uuid) - {active}) and not decks.external_ref(deck_uuid, active):
        raise CrossBackendBindingError(cross_backend_message(source_ref, active))
    if not binding_is_dead(decks, driver, deck_uuid=deck_uuid, source_ref=source_ref):
        return
    # Make the refusal dup-aware. Under dup names a bare ``save-deck "X"`` would only
    # re-refuse on ambiguity, so name the executable ``save-deck "X" --id <prefix>``
    # that pins the dead row instead.
    siblings = decks.rows_for_name(source_ref)
    dup = len(siblings) > 1
    raise DeadBindingError(
        dead_binding_message(driver, source_ref, dup_names=dup, id_prefix=deck_uuid[:6] if dup else None)
    )


def cross_backend_message(source_ref: str, active_backend: str) -> str:
    """Build the write-side cross-backend refusal — mirrors the read chokepoint's wording.

    A row bound on another backend has its source there; the fix is to switch the active
    backend back, not to recreate (which would clobber a same-named file here). An agent
    reads this literally, so it names the safe corrective action, never a destructive one.
    """
    return (
        f'{source_ref!r} is bound to a different backend than the active one ({active_backend}) — '
        'switch back to its source backend to write to it. Your local copy is intact.'
    )


def dead_binding_message(
    driver: CollectionStore, source_ref: str, *, dup_names: bool = False, id_prefix: str | None = None
) -> str:
    """Build the dead-binding refusal message.

    Every command it names is real, safe, and copy-pasteable — agents run these
    messages literally.

    - Recovery: ``save-deck "X"`` re-saves the deck from its local copy to a fresh
      source (forced-fresh + atomic — it can never adopt/overwrite a same-named
      stranger). Under dup names the recovery is pinned by ``--id <prefix>``.
    - Inspection: ``get-deck "X" --local`` serves the local copy so the agent can see
      its deck without a pull (a dead ref cannot be pulled).

    It must not tell the user to ``pull`` (circular: a dead ref cannot be re-bound by
    pull — the read chokepoint returns None by design). Each ends "Your local copy is
    intact."
    """
    save_cmd = f'collection save-deck "{source_ref}"'
    if dup_names and id_prefix:
        save_cmd = f'collection save-deck "{source_ref}" --id {id_prefix}'
    view = f'collection get-deck "{source_ref}" --local'
    if driver.backend_name == 'airtable':
        return (
            f'the Airtable record for {source_ref!r} was deleted. Re-save it with: '
            f'{save_cmd}  — this creates a new Decks record from your local copy. '
            f'(View the local copy first with: {view}.) '
            'Your local copy is intact.'
        )
    return (
        f'the deck file for {source_ref!r} is missing or its id changed (it was deleted, '
        "moved out of the collection, or its 'uuid:' line was changed). Re-save it with: "
        f'{save_cmd}  — this writes a fresh deck file from your local copy. '
        f'(View the local copy first with: {view}.) '
        'Your local copy is intact.'
    )


def _require_row(decks: DecksStore, deck_uuid: str) -> tuple[Deck, str]:
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


def read_bound_source(
    decks: DecksStore,
    driver: CollectionStore,
    *,
    deck_uuid: str,
    source_ref: str,
    expected_ref: str | None = None,
) -> Deck | None:
    """Read a row's source of record by its bound external ref — the single read path.

    Names are ambiguous (duplicates, renames, case aliases); external refs are not.
    Every source read routes through here so a read by name is impossible outside
    this function's one controlled fallback (a genuine first pull of a never-bound
    deck, which then binds the row).

    Resolution order:

    1. ``expected_ref`` set (reread-after-create): read strictly by that ref —
       the just-created recordId (Airtable ``get_deck_by_record_id``) / just-written
       in-file uuid (local ``get_deck_by_uuid``). Never a name read; None on miss.
    2. The row's bound ref in ``external_ids`` — ``airtable`` -> ``get_deck_by_record_id``,
       ``local`` -> ``get_deck_by_uuid``. When bound, read by that ref; if it
       resolves to nothing (deleted file/record) return None ("source gone") — never
       fall through to a name read of a different same-named object.
    3. No bound ref at all (a genuine first pull of a never-bound deck): fall back to
       the name read, then bind the row from what was read so the next read is bound.

    Returns the source :class:`Deck`, or None when the source is genuinely absent
    (a not-yet-created source, or a dead bound ref). Never raises FileNotFoundError.
    """
    backend = _active_backend(driver)

    # (1) expected_ref — reread by the just-written identity, strictly.
    if expected_ref:
        return _read_by_ref(driver, backend, expected_ref)

    # (2) the row's bound external ref — read by THAT ref; a dead ref returns None.
    bound = decks.external_ref(deck_uuid, backend)
    if bound:
        return _read_by_ref(driver, backend, bound)

    # (3) genuine first pull — no bound ref: name read (the one controlled fallback),
    # then bind the row so every subsequent read is bound.
    #
    # But if the row is already bound on a different backend (e.g. only
    # ``{'airtable': rec}`` while the active backend is local), a name read here would
    # silently adopt an unrelated same-named file for this backend and clobber the
    # row's content. That is not a genuine first pull — the row has a source, just on
    # another backend. Return None (source absent here) rather than auto-bind a
    # same-named stranger.
    if decks.bound_backends(deck_uuid) - {backend}:
        return None
    try:
        source = driver.get_deck(source_ref)
    except FileNotFoundError:
        return None
    _bind_from_source(decks, deck_uuid, backend, source)
    return source


def _read_by_ref(driver: CollectionStore, backend: str | None, native_ref: str) -> Deck | None:
    """Read a source deck by its native external ref (recordId / in-file uuid); None on miss."""
    reader = (
        getattr(driver, 'get_deck_by_record_id', None)
        if backend == 'airtable'
        else getattr(driver, 'get_deck_by_uuid', None)
    )
    if reader is None:
        return None
    try:
        return reader(native_ref)
    except FileNotFoundError:
        return None


def _bind_from_source(decks: DecksStore, deck_uuid: str, backend: str | None, source: Deck) -> None:
    """Bind the row to the source's external ref after a first-pull name read.

    Best-effort: a first pull may not have materialized the row yet (``pull`` itself
    ``put``s it right after this read), so a missing row is not an error — the caller
    binds explicitly after the put. Only an existing row is bound here.
    """
    from pipeline.decks.store import DecksError

    ref = ('airtable', source.airtable_record_id) if backend == 'airtable' else ('local', source.uuid)
    if not ref[1]:
        return
    if decks.get_row(deck_uuid) is None:
        return
    with contextlib.suppress(DecksError):
        decks.set_external_id(deck_uuid, ref[0], ref[1])


def pull(decks: DecksStore, driver: CollectionStore, *, deck_uuid: str, source_ref: str) -> None:
    """Pull the source deck into the local store and stamp the baseline.

    ``driver.get_deck(source_ref)`` -> ``decks.put(..., sync_status='synced',
    source_ref=..., synced_baseline=version(source))``. Brings the local copy
    current with the source of record. Overwrites the local ``deck_json`` (a pull
    is "take the source's version"), preserving nothing local — the caller pulls
    deliberately (first access / explicit ``pull`` verb).
    """
    # Read the source through the one bound-read chokepoint: a dup-named / renamed
    # row reads its own bound file/record, never the base slug. A dead bound ref
    # returns None — treat as "source gone" rather than clobbering the local copy
    # with an unrelated same-named deck.
    source = read_bound_source(decks, driver, deck_uuid=deck_uuid, source_ref=source_ref)
    if source is None:
        from pipeline.decks.store import DecksError

        raise DecksError(f'cannot pull {deck_uuid!r}: its source {source_ref!r} no longer exists')
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


def push(
    decks: DecksStore,
    driver: CollectionStore,
    *,
    deck_uuid: str,
    allow_shrink: bool = False,
) -> None:
    """Push the local deck to the source through the ceremony — drift-guarded.

    1. Drift: read the current source; if ``version(current source) !=
       synced_baseline`` the source moved. Allow only when the current source
       already equals the local deck (``version(local) == version(current
       source)`` — our own prior write / idempotent re-push); otherwise raise
       :class:`SyncDriftError` without saving (the source's change is preserved).
    2. ``driver.save_deck(local, allow_shrink=allow_shrink)`` — the shrink
       ceremony fires; any :class:`CollectionError` surfaces unswallowed.
    3. Update ``synced_baseline = version(local)``.
    """
    local, source_ref = _require_row(decks, deck_uuid)
    row = decks.get_row(deck_uuid)
    assert row is not None  # _require_row already validated it exists.

    # A dead binding (bound ref for this backend, source read None) must refuse —
    # never fall through the ``current_source is None`` guard-skip below and silently
    # recreate a deleted source or fork next to a re-uuid'd file. A genuine first push
    # (no bound ref) is not dead and proceeds to create.
    require_writable_binding(decks, driver, deck_uuid=deck_uuid, source_ref=source_ref)

    local_version = version(local)
    # Read the current source slug-proof: a dup-named local deck makes a name/slug read
    # ambiguous, so prefer the row's bound in-file uuid (``get_deck_by_uuid``) — reading
    # the base slug could pick an unrelated same-named deck and spuriously drift.
    current_source = read_bound_source(decks, driver, deck_uuid=deck_uuid, source_ref=source_ref)
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

    # Land on the existing source's file: stamp the local content with the source's
    # in-file uuid so the slug-collision guard writes back to the same file rather
    # than disambiguating to a new one (same external ref = same deck). version()
    # excludes uuid, so the baseline hashes are unaffected. A not-yet-existing source
    # keeps the local uuid (this push creates it).
    to_save = local
    if current_source is not None and current_source.uuid and current_source.uuid != local.uuid:
        to_save = local.model_copy(update={'uuid': current_source.uuid})
    driver.save_deck(to_save, allow_shrink=allow_shrink)

    # Baseline := the source as re-read after the write, so a later drift check
    # compares like-for-like (the ceremony may canonicalize names on save; the
    # baseline must be what the bound read reads back, not our pre-write local hash).
    reread = read_bound_source(decks, driver, deck_uuid=deck_uuid, source_ref=source_ref)
    written_version = version(reread) if reread is not None else local_version
    decks.put(
        local,
        deck_uuid=deck_uuid,
        sync_status='synced',
        source_ref=source_ref,
        synced_baseline=written_version,
        rationale=f'push to {source_ref}',
    )


def promote(decks: DecksStore, driver: CollectionStore, *, deck_uuid: str, to_name: str | None = None) -> None:
    """Commit an ephemeral draft — by lineage, save-before-flip, then consume.

    Decided by the draft's ``derived_from``:

    - **Exploration draft** (``derived_from`` set): commit its content onto the
      parent's source, bound by the parent row's external ref (not its name), via
      the drift-guarded ``push`` ceremony. On success refresh the parent's canonical
      local row and consume the draft (``consumed`` + archived). Net: the parent row
      is the single synced row for that ref; the draft is retired. ``to_name`` is
      ignored (the target is the parent, by lineage).
    - **Clean-slate draft** (``derived_from`` NULL): create a new source deck named
      ``to_name`` (mint a fresh external ref = the draft's own uuid). A deck with
      that name may already exist — that is fine (dup names, uuid identity); we
      create a separate deck and never bind-by-name to an existing one. The draft's
      own row becomes the synced deck (it is the deck now — not consumed).

    Save-before-flip: ``driver.save_deck`` runs first; state flips only after the
    write succeeds. If the ceremony refuses (e.g. a shrink), the draft stays a clean
    ephemeral draft — no half-synced zombie.
    """
    from pipeline.decks.store import DecksError

    draft = decks.get(deck_uuid)
    if draft is None:
        raise DecksError(f'no deck with id {deck_uuid!r}')
    row = decks.get_row(deck_uuid)
    assert row is not None  # get() already proved the row exists.
    if row.sync_status == 'consumed':
        raise DecksError(f'{row.name!r} is a consumed draft (already promoted) and cannot be promoted again')

    if row.derived_from:
        _promote_exploration(decks, driver, deck_uuid=deck_uuid, draft=draft, parent_uuid=row.derived_from)
    else:
        _promote_clean_slate(decks, driver, deck_uuid=deck_uuid, draft=draft, to_name=to_name or draft.name)


def _promote_exploration(
    decks: DecksStore, driver: CollectionStore, *, deck_uuid: str, draft: Deck, parent_uuid: str
) -> None:
    """Commit an exploration draft onto its parent's ref, then consume it."""
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

    # Refuse if the parent's binding is dead (its source file/record is gone or
    # re-identified) — promoting onto it would silently recreate/fork the parent source.
    require_writable_binding(decks, driver, deck_uuid=parent_uuid, source_ref=source_ref)

    # Read the parent's current source now — its own external identity (in-file uuid /
    # recordId) is what the save must be stamped with, mirroring ``push``. The local
    # row PK is not that identity: in a migrated store the PK diverges from the source
    # file's in-file uuid, and a ``--from`` draft deliberately NULLs the recordId, so
    # stamping the PK forks the file / creates a junk duplicate Airtable row. We reuse
    # this read for the drift guard below.
    #
    # Bound read: read the parent's source by its bound ref, not by name. A renamed
    # parent file 404s on a name read -> None -> the drift guard would silently
    # disengage and destroy a concurrent foreign edit.
    current_source = read_bound_source(decks, driver, deck_uuid=parent_uuid, source_ref=source_ref)

    # The content to land = the draft's cards/metadata, but stamped with the parent
    # source's own external identity so the save binds to the parent's file/record:
    #   - name: the parent's name (address the same source deck, not the draft's name);
    #   - uuid: the source file's in-file uuid (local YAML slug-collision guard writes
    #     back to the same file) — fall back to the local row's bound 'local' ref, then
    #     the parent PK only as a last resort;
    #   - airtable_record_id: the parent record's recordId (adapter takes the
    #     update_record branch — no duplicate create). version() excludes uuid + name +
    #     record id, so the baseline hash is unaffected.
    source_uuid = _source_local_uuid(decks, parent_uuid, current_source) or parent_deck.uuid
    record_id = _source_record_id(decks, parent_uuid, current_source)
    to_source = draft.model_copy(
        update={'uuid': source_uuid, 'name': parent_deck.name, 'airtable_record_id': record_id}
    )
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

    # Save first — any shrink/ceremony refusal surfaces here, before any flip.
    # ``save_deck`` may stamp a created recordId onto ``to_source`` in place (Airtable
    # create branch); we reread by that identity below (never by name).
    driver.save_deck(to_source, allow_shrink=False)

    # Success: refresh the parent's canonical local row + baseline (bound by ref),
    # then consume the draft (retire the exploration copy). Reread by the source's
    # own external identity through the chokepoint — the parent's recordId (Airtable)
    # or the just-written in-file uuid (local), never a name read.
    written = _reread_after_write(decks, driver, parent_uuid, source_ref, to_source)
    decks.put(
        written,
        deck_uuid=parent_uuid,
        sync_status='synced',
        source_ref=source_ref,
        synced_baseline=version(written),
        rationale=f'promote (exploration {deck_uuid[:6]}) -> {source_ref}',
    )
    decks.consume(deck_uuid)


def _reread_after_write(
    decks: DecksStore, driver: CollectionStore, deck_uuid: str, source_ref: str, written: Deck
) -> Deck:
    """Reread a just-written source by its own external identity through the chokepoint.

    After a save the source's identity is known exactly: the recordId ``save_deck``
    stamped onto ``written`` (Airtable create), the parent's already-bound recordId
    (Airtable update), or the just-written in-file uuid (local). Reread by that ref
    (``expected_ref``) so the baseline reflects this deck, never a same-named sibling.
    Falls back to the written deck itself if the reread comes back empty.
    """
    if _active_backend(driver) == 'airtable':
        expected = written.airtable_record_id or decks.external_ref(deck_uuid, 'airtable')
    else:
        expected = written.uuid
    reread = read_bound_source(decks, driver, deck_uuid=deck_uuid, source_ref=source_ref, expected_ref=expected)
    return reread if reread is not None else written


def _source_local_uuid(decks: DecksStore, parent_uuid: str, current_source: Deck | None) -> str | None:
    """The parent source's in-file uuid: the source read's own uuid, else the bound 'local' ref."""
    if current_source is not None and current_source.uuid:
        return current_source.uuid
    return decks.external_ref(parent_uuid, 'local')


def _source_record_id(decks: DecksStore, parent_uuid: str, current_source: Deck | None) -> str | None:
    """The parent source's Airtable recordId: the source read's own, else the bound 'airtable' ref."""
    if current_source is not None and current_source.airtable_record_id:
        return current_source.airtable_record_id
    return decks.external_ref(parent_uuid, 'airtable')


def _promote_clean_slate(
    decks: DecksStore, driver: CollectionStore, *, deck_uuid: str, draft: Deck, to_name: str
) -> None:
    """Create a new source deck for a clean-slate draft; the draft's row becomes synced."""
    # A clean-slate promote creates a fresh source deck — a fresh external ref (the
    # draft's own uuid) that never binds to an existing same-named deck. Save the
    # draft under ``to_name`` first. The slug-collision guard in the local adapter
    # makes a dup name land on its own file rather than clobber the existing.
    to_source = draft if draft.name == to_name else draft.model_copy(update={'name': to_name})
    # ``save_deck`` may stamp the created recordId onto ``to_source`` in place
    # (Airtable create branch) so we can reread this record, never a name sibling.
    driver.save_deck(to_source, allow_shrink=False)

    # Reread by the just-created identity through the chokepoint: the created recordId
    # (Airtable) / the draft's own in-file uuid (local). The row is not yet bound, so
    # pass the identity as ``expected_ref`` — never fall back to a name read.
    written = _reread_after_write(decks, driver, deck_uuid, to_name, to_source)
    decks.put(
        written,
        deck_uuid=deck_uuid,
        sync_status='synced',
        source_ref=to_name,
        synced_baseline=version(written),
        rationale=f'promote (clean-slate) -> {to_name}',
    )
    # Bind the promoted row to its own source's external ref: otherwise a later
    # name-addressed read/pull of ``to_name`` would re-read the base slug file (an
    # unrelated same-named deck) and rebind this row to it, redirecting every edit
    # onto the wrong deck. The disambiguated file carries the draft's own in-file
    # uuid; on Airtable a create just assigned a recordId.
    if driver.backend_name == 'airtable' and written.airtable_record_id:
        decks.set_external_id(deck_uuid, 'airtable', written.airtable_record_id)
    elif written.uuid:
        decks.set_external_id(deck_uuid, 'local', written.uuid)


def sync_reconcile(
    decks: DecksStore,
    driver: CollectionStore,
    *,
    deck_uuid: str,
    allow_shrink: bool = False,
) -> None:
    """Reconcile a synced deck against its source — pull / push / drift.

    Compare ``version(local)`` / ``synced_baseline`` / ``version(current source)``:

    - only source moved (``local == baseline``) → pull (take the source's version).
    - only local moved (``source == baseline``) → push (send the local edit).
    - neither moved (all equal) → no-op.
    - both moved (``local != baseline`` and ``source != baseline`` and
      ``local != source``) → raise :class:`SyncDriftError`; change nothing (both
      preserved). Never pull-clobber an unpushed local edit.
    """
    local, source_ref = _require_row(decks, deck_uuid)
    row = decks.get_row(deck_uuid)
    assert row is not None

    # A dead binding refuses here too — otherwise the ``source_version is None`` branch
    # below reads it as "nothing on the source yet → push (create)" and resurrects/forks
    # the gone source. Recovery is a fresh-identity ``save-deck``.
    require_writable_binding(decks, driver, deck_uuid=deck_uuid, source_ref=source_ref)

    local_version = version(local)
    # Bound read: reconcile against the row's own bound source, never the base slug (a
    # dup-named / renamed row must not compare against an unrelated deck).
    source = read_bound_source(decks, driver, deck_uuid=deck_uuid, source_ref=source_ref)
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
