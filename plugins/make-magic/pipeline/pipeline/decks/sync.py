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

__all__ = ('SyncDriftError', 'promote', 'pull', 'push', 'read_source_bound', 'sync_reconcile')


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


def read_source_bound(
    decks: DecksStore,
    driver: CollectionStore,
    *,
    deck_uuid: str,
    source_ref: str,
    expected_ref: str | None = None,
) -> Deck | None:
    """THE single source-of-record read chokepoint — always by BOUND external ref (P9).

    Round 6 was one class of defect: a source read went by NAME instead of by the
    row's bound external ref, so a dup-named / renamed source read the WRONG object
    (clobber, spurious drift, or a write to an unrelated Airtable record). This is
    the one function every source read routes through, so reading a source BY NAME
    becomes impossible to write outside this function's OWN controlled fallback.

    Resolution order:

    1. ``expected_ref`` set (reread-after-create, B4): read STRICTLY by that ref —
       the just-created recordId (Airtable ``get_deck_by_record_id``) / just-written
       in-file uuid (local ``get_deck_by_uuid``). NEVER a name read; None on miss.
    2. The row's BOUND ref in ``external_ids`` — ``airtable`` -> ``get_deck_by_record_id``
       (M2), ``local`` -> ``get_deck_by_uuid``. When bound, read by THAT ref; if it
       resolves to nothing (deleted file/record) return None ("source gone") — never
       fall through to a name read of a DIFFERENT same-named object.
    3. No bound ref at all (a genuine first pull of a never-bound deck): fall back to
       the NAME read, then BIND the row from what was read so the next read is bound.

    Returns the source :class:`Deck`, or None when the source is genuinely absent
    (a not-yet-created source, or a dead bound ref). Never raises FileNotFoundError.
    """
    backend = getattr(driver, 'backend_name', None)

    # (1) expected_ref — reread by the just-written identity, strictly.
    if expected_ref:
        return _read_by_ref(driver, backend, expected_ref)

    # (2) the row's bound external ref — read by THAT ref; a dead ref returns None.
    if backend == 'airtable':
        bound = _external_ref_value(decks, deck_uuid, 'airtable')
        if bound:
            return _read_by_ref(driver, backend, bound)
    else:
        bound = _external_ref_value(decks, deck_uuid, 'local')
        if bound:
            return _read_by_ref(driver, backend, bound)

    # (3) genuine first pull — no bound ref: NAME read (the ONE controlled fallback),
    # then bind the row so every subsequent read is bound.
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
    """Bind the row to the source's external ref after a first-pull name read (P9 step 3).

    Best-effort: a first pull may not have MATERIALIZED the row yet (``pull`` itself
    ``put``s it right after this read), so a missing row is not an error — the caller
    binds explicitly after the put. Only an existing row is bound here.
    """
    import contextlib

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
    # Read the source through the ONE bound-read chokepoint (P9): a dup-named /
    # renamed row reads ITS OWN bound file/record, never the base slug (r6-B1). A
    # dead bound ref returns None — treat as "source gone" rather than clobbering
    # the local copy with an unrelated same-named deck.
    source = read_source_bound(decks, driver, deck_uuid=deck_uuid, source_ref=source_ref)
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
    # Read the CURRENT source slug-proof: a dup-named local deck makes a name/slug read
    # ambiguous, so prefer the row's bound in-file uuid (``get_deck_by_uuid``) — reading
    # the base slug could pick an UNRELATED same-named deck and spuriously drift (F4).
    current_source = read_source_bound(decks, driver, deck_uuid=deck_uuid, source_ref=source_ref)
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
    # baseline must be what the bound read reads back, not our pre-write local hash).
    reread = read_source_bound(decks, driver, deck_uuid=deck_uuid, source_ref=source_ref)
    written_version = version(reread) if reread is not None else local_version
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

    # Read the parent's CURRENT source now — its own external identity (in-file uuid /
    # recordId) is what the save must be stamped with, mirroring ``push`` (F2/F3). The
    # local row PK is NOT that identity: in a migrated store the PK diverges from the
    # source file's in-file uuid, and a ``--from`` draft deliberately NULLs the
    # recordId, so stamping the PK forks the file (F2) / creates a junk duplicate
    # Airtable row (F3). We reuse this read for the drift guard below.
    #
    # BOUND read (P9, r6-B2): read the parent's source by ITS bound ref, not by name.
    # A renamed parent FILE (a supported persona) 404s on a name read -> None ->
    # the drift guard would silently disengage and DESTROY a concurrent foreign edit.
    current_source = read_source_bound(decks, driver, deck_uuid=parent_uuid, source_ref=source_ref)

    # The content to land = the draft's cards/metadata, but stamped with the PARENT
    # SOURCE's OWN external identity so the save binds to the parent's FILE/RECORD:
    #   - name: the parent's name (address the same source deck, not the draft's name);
    #   - uuid: the source file's in-file uuid (local YAML slug-collision guard writes
    #     back to the SAME file) — fall back to the local row's bound 'local' ref, then
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

    # SAVE FIRST (M6) — any shrink/ceremony refusal surfaces here, before any flip.
    # ``save_deck`` may STAMP a created recordId onto ``to_source`` in place (Airtable
    # create branch); we reread by that identity below (never by name).
    driver.save_deck(to_source, allow_shrink=False)

    # Success: refresh the parent's canonical local row + baseline (bound by ref),
    # then CONSUME the draft (retire the exploration copy). Reread by the SOURCE's
    # own external identity through the chokepoint (r6-B4) — the parent's recordId
    # (Airtable) or the just-written in-file uuid (local), never a name read.
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
    """Reread a just-written source by its OWN external identity through the chokepoint.

    After a save the source's identity is known exactly: the recordId ``save_deck``
    stamped onto ``written`` (Airtable create), the parent's already-bound recordId
    (Airtable update), or the just-written in-file uuid (local). Reread by THAT ref
    (``expected_ref``) so the baseline reflects THIS deck, never a same-named sibling
    (r6-B4). Falls back to the written deck itself if the reread comes back empty.
    """
    backend = getattr(driver, 'backend_name', None)
    if backend == 'airtable':
        expected = written.airtable_record_id or _external_ref_value(decks, deck_uuid, 'airtable')
    else:
        expected = written.uuid
    reread = read_source_bound(
        decks, driver, deck_uuid=deck_uuid, source_ref=source_ref, expected_ref=expected
    )
    return reread if reread is not None else written


def _external_ref_value(decks: DecksStore, deck_uuid: str, backend: str) -> str | None:
    """Return the parent row's bound ``external_ids[backend]`` native ref (or None)."""
    import json as _json

    raw = decks.external_ids(deck_uuid)
    if not raw:
        return None
    try:
        ext = _json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(ext, dict):
        return None
    ref = ext.get(backend)
    return ref if isinstance(ref, str) and ref else None


def _source_local_uuid(decks: DecksStore, parent_uuid: str, current_source: Deck | None) -> str | None:
    """The parent SOURCE's in-file uuid: the source read's own uuid, else the bound 'local' ref."""
    if current_source is not None and current_source.uuid:
        return current_source.uuid
    return _external_ref_value(decks, parent_uuid, 'local')


def _source_record_id(decks: DecksStore, parent_uuid: str, current_source: Deck | None) -> str | None:
    """The parent SOURCE's Airtable recordId: the source read's own, else the bound 'airtable' ref."""
    if current_source is not None and current_source.airtable_record_id:
        return current_source.airtable_record_id
    return _external_ref_value(decks, parent_uuid, 'airtable')


def _promote_clean_slate(
    decks: DecksStore, driver: CollectionStore, *, deck_uuid: str, draft: Deck, to_name: str
) -> None:
    """Create a NEW source deck for a clean-slate draft; the draft's row becomes synced."""
    # A clean-slate promote CREATES a fresh source deck — a fresh external ref (the
    # draft's own uuid) that never binds to an existing same-named deck. Save the
    # draft under ``to_name`` FIRST (M6). The slug-collision guard in the local
    # adapter makes a dup name land on its own file rather than clobber the existing.
    to_source = draft if draft.name == to_name else draft.model_copy(update={'name': to_name})
    # ``save_deck`` may STAMP the created recordId onto ``to_source`` in place
    # (Airtable create branch) so we can reread THIS record, never a name sibling.
    driver.save_deck(to_source, allow_shrink=False)

    # Reread by the JUST-CREATED identity through the chokepoint (r6-B4): the created
    # recordId (Airtable) / the draft's own in-file uuid (local). The row is not yet
    # bound, so pass the identity as ``expected_ref`` — never fall back to a name read.
    written = _reread_after_write(decks, driver, deck_uuid, to_name, to_source)
    decks.put(
        written,
        deck_uuid=deck_uuid,
        sync_status='synced',
        source_ref=to_name,
        synced_baseline=version(written),
        rationale=f'promote (clean-slate) -> {to_name}',
    )
    # BIND the promoted row to ITS OWN source's external ref (F4): otherwise a later
    # name-addressed read/pull of ``to_name`` would re-read the BASE slug file (an
    # unrelated same-named deck) and rebind this row to it, redirecting every edit
    # onto the wrong deck. The disambiguated file carries the draft's own in-file
    # uuid; on Airtable a create just assigned a recordId.
    if driver.backend_name == 'airtable' and written.airtable_record_id:
        decks.set_external_id(deck_uuid, 'airtable', written.airtable_record_id)
    elif written.uuid:
        decks.set_external_id(deck_uuid, 'local', written.uuid)


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
    # BOUND read (P9, r6-B1): reconcile against the row's OWN bound source, never the
    # base slug (a dup-named / renamed row must not compare against an unrelated deck).
    source = read_source_bound(decks, driver, deck_uuid=deck_uuid, source_ref=source_ref)
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
