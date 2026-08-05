"""The deck access path — route collection deck reads/writes through the local store.

All deck access routes through the local ``DecksStore``; decks only (inventory /
chase / trades keep calling ``get_store`` directly and are not rerouted here).

Pull-TTL policy:
    A synced deck is pulled-current from its source on first access (the local
    copy is absent) or when a short TTL has elapsed since the last pull; the local
    copy is served thereafter. An ephemeral deck is served straight from the local
    store (no source, zero network). Edits go to the local store; a push to the
    source happens only at an explicit commit boundary (the ``push`` / ``sync``
    verbs), never on every edit.

This keeps a plain ``get-deck`` from going stale against the source without a
network round-trip on every read: the TTL bounds staleness while amortizing the
pull. A manual ``pull`` verb forces a refresh when the user wants one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from pipeline.decks import sync as _sync
from pipeline.decks.store import DeckRow, DecksError, DecksStore
from pipeline.decks.version import version

if TYPE_CHECKING:
    from pipeline.collection.store import CollectionStore
    from pipeline.contracts import Deck

__all__ = ('DeckAccess', 'deck_access')

#: How long a synced local copy is served before a read re-pulls it.
_PULL_TTL = timedelta(minutes=15)


class DeckAccess:
    """Route deck reads/writes through the local ``DecksStore`` over a source driver.

    Decks only. The ``driver`` is the active source-of-record ``CollectionStore``
    (built by ``get_store``); inventory/chase/trades are not handled here — callers
    use the driver directly for those.
    """

    def __init__(self, driver: CollectionStore, *, decks: DecksStore | None = None) -> None:
        self._driver = driver
        self._decks = decks if decks is not None else DecksStore()

    @property
    def backend(self) -> str:
        return self._driver.backend_name

    def resolve(self, name: str | None = None, *, id_prefix: str | None = None) -> str:
        """Resolve a deck name (or an ``--id`` prefix) to its ``deck_uuid`` — the choke point.

        The single resolution point every name-addressed verb calls. Names are
        labels; ``deck_uuid`` is the identity.

        - ``id_prefix`` set (the ``--id`` escape hatch, precedence over ``name``):
          prefix-match ``deck_uuid`` among live (non-consumed) rows — 0 -> error,
          1 -> that uuid, >1 -> an "ambiguous --id prefix" error.
        - ``name`` set: match live rows by name — 0 -> mint a new uuid (a create /
          a first read of a synced source deck about to be pulled), 1 -> that uuid,
          >1 -> refuse with the user-decided candidate list (a :class:`DecksError`
          that surfaces as the clean multi-line message, not a traceback).

        Exactly one of ``name`` / ``id_prefix`` must be given.
        """
        if id_prefix is not None:
            return self._resolve_id_prefix(id_prefix)
        if name is None:
            raise DecksError('resolve requires a deck name or an --id prefix')
        rows = self._decks.rows_for_name(name)
        if len(rows) == 1:
            return rows[0].deck_uuid
        if not rows:
            return uuid4().hex
        raise DecksError(self._ambiguous_name_message(name, rows))

    def _resolve_id_prefix(self, id_prefix: str) -> str:
        """Resolve an ``--id <prefix>`` to a single ``deck_uuid`` (0/1/>1 -> error/uuid/error)."""
        matches = self._decks.uuids_by_prefix(id_prefix)
        if not matches:
            raise DecksError(f'no deck with id prefix {id_prefix!r}')
        if len(matches) > 1:
            raise DecksError(
                f'--id prefix {id_prefix!r} is ambiguous ({len(matches)} decks); '
                'use more characters of the id.'
            )
        return matches[0]

    def _ambiguous_name_message(self, name: str, rows: list[DeckRow]) -> str:
        """Build the user-decided dup-name candidate list (copy-paste ``--id`` lines).

        Format: a header naming the count, then one
        ``  --id <6hex>   # <status>[,archived] · <source-backend>`` line per
        candidate, sorted synced-first then by name. The short id is the first 6
        hex of ``deck_uuid``; the source backend is ``airtable`` when the row
        carries an airtable external ref, else ``local``.
        """
        ordered = sorted(rows, key=lambda r: (r.sync_status != 'synced', r.name))
        lines = [f'{name!r} is ambiguous ({len(rows)} decks). Re-run with one of:']
        for row in ordered:
            status = row.sync_status
            if row.archived:
                status = f'{status},archived'
            lines.append(f'  --id {row.deck_uuid[:6]}   # {status} · {self._source_backend(row)}')
        return '\n'.join(lines)

    def _dead_binding_message(self, name: str, deck_uuid: str) -> str:
        """The dead-binding refusal, dup-aware: under dup names, name the ``--id``.

        A single-row dead deck recovers via ``save-deck "X"``; a dup-named dead deck must
        be recovered by ``save-deck "X" --id <prefix>`` (a bare name would refuse with the
        ambiguity list). Detect the dup and hand the executable ``--id`` form to the message.
        """
        siblings = self._decks.rows_for_name(name)
        dup = len(siblings) > 1
        return _sync.dead_binding_message(
            self._driver, name, dup_names=dup, id_prefix=deck_uuid[:6] if dup else None
        )

    def _alias_dead_row(self, name: str) -> tuple[str, str] | None:
        """A case-alias of ``name`` that binds a single dead row (anti-fork).

        Returns ``(deck_uuid, canonical_name)`` when ``name`` is a case variant of exactly
        one live synced row whose binding is dead — so a ``save-deck EMBER`` typo recovers
        the dead ``Ember`` row instead of forking a new one. Returns None when there is no
        such alias, an exact-name row already exists (handled by the normal path), or the
        case match is ambiguous (>1 candidate — refuse to guess which to recover).
        """
        key = ' '.join(name.split()).casefold()
        candidates: list[DeckRow] = []
        for row in self._decks.list_rows(sync_status='synced', include_archived=True):
            if row.source_ref is None:
                continue
            if row.name == name:
                return None  # an exact-name row exists — not an alias case.
            if ' '.join(row.name.split()).casefold() != key:
                continue
            if _sync.binding_is_dead(
                self._decks, self._driver, deck_uuid=row.deck_uuid, source_ref=row.source_ref
            ):
                candidates.append(row)
        if len(candidates) == 1:
            return candidates[0].deck_uuid, candidates[0].name
        return None

    def _source_backend(self, row: DeckRow) -> str:
        """The candidate's source backend label: ``airtable`` if bound there, else ``local``."""
        if 'airtable' in self._decks.bound_backends(row.deck_uuid):
            return 'airtable'
        return 'local'

    def has_local_row(self, deck_uuid: str) -> bool:
        """True iff a local decks-store row exists for ``deck_uuid`` (edit-path guard)."""
        return self._decks.exists(deck_uuid)

    # ----------------------------------------------------------------------- #
    # Local-copy recovery + inspection
    # ----------------------------------------------------------------------- #

    def save_from_local(self, name: str, *, id_prefix: str | None = None, allow_shrink: bool = False) -> str:
        """Recover a deck from its local copy to a fresh source.

        The dead-binding message advises ``save-deck "X"`` (or ``--id <prefix>`` under
        dup names). This makes that advice executable: read the deck's own local row's
        ``deck_json`` and re-save it — which routes through the forced-fresh, atomic
        recovery in :meth:`save_deck` (a dead binding recovers; a live binding is a
        normal authoritative re-save). No ``--from-json`` needed and no pull: the local
        copy is the payload. Returns the deck name saved (for the CLI's echo).

        Addressing:

        - ``id_prefix`` set: address the one row by its ``deck_uuid`` prefix (the dup-name
          escape hatch) — resolves to a single row or raises the clean --id error.
        - ``name`` only: resolve the single row for ``name``; a dup-name refuses with the
          candidate list (which names ``save-deck "X" --id <prefix>`` as the fix).
        """
        deck_uuid = self._resolve_local_for_recovery(name, id_prefix)
        local = self._decks.get(deck_uuid)
        if local is None:
            raise DecksError(f'no local copy to recover for {name!r} (nothing to save)')
        # Re-save the local copy under its own name (the row's canonical name) so a
        # case-alias payload can never fork the recovery to a new row. Pin the
        # write to the resolved row (``target_uuid``) so a dup-named recovery lands on the
        # row we resolved (by ``--id`` or by a unique name), never the oldest sibling.
        row = self._decks.get_row(deck_uuid)
        canonical = row.name if row is not None else name
        self.save_deck(
            local.model_copy(update={'name': canonical}),
            allow_shrink=allow_shrink, commit=True, target_uuid=deck_uuid,
        )
        return canonical

    def _resolve_local_for_recovery(self, name: str, id_prefix: str | None) -> str:
        """Resolve a ``save-deck <name>``/``--id`` recovery target to a single local row.

        Never mints on a miss (a bare ``resolve`` would): a recovery must address an
        existing local row (there is nothing to recover otherwise). A dup-name refuses
        with the candidate list (naming ``--id``, which ``save-deck`` accepts).
        """
        if id_prefix is not None:
            return self.resolve(id_prefix=id_prefix)
        rows = self._decks.rows_for_name(name)
        if len(rows) == 1:
            return rows[0].deck_uuid
        if len(rows) > 1:
            raise DecksError(self._ambiguous_name_message(name, rows))
        raise DecksError(f'no deck named {name!r} to recover')

    def read_local(self, name: str, *, id_prefix: str | None = None) -> Deck:
        """Serve the local copy directly — no pull, no source read (``--local``).

        The dead-binding read refuses (the source is gone); ``get-deck --local`` gives the
        agent a way to see its deck under a dead binding without pulling (a dead ref cannot
        be pulled) and without being forced to reconstruct the JSON from the same-named
        stranger file at the slug. Addresses an existing row only (name or ``--id``); a
        miss is a clean error, never a mint/pull.
        """
        deck_uuid = self._resolve_local_for_recovery(name, id_prefix)
        local = self._decks.get(deck_uuid)
        if local is None:
            raise DecksError(f'no local copy for {name!r}')
        return local

    def dead_bound_rows(self) -> list[DeckRow]:
        """Return the synced local rows whose bound source is dead.

        A dead-bound synced deck's source file/record is gone, so it drops out of the
        source ``list_decks`` enumeration and would vanish from ``list-decks`` entirely.
        Surface it (flagged ``source-missing``) so the agent can see + recover it. Only
        live (non-consumed, non-archived) synced rows are considered.
        """
        out: list[DeckRow] = []
        for row in self._decks.list_rows(sync_status='synced'):
            if row.source_ref is None:
                continue
            if _sync.binding_is_dead(
                self._decks, self._driver, deck_uuid=row.deck_uuid, source_ref=row.source_ref
            ):
                out.append(row)
        return out

    def is_binding_dead(self, deck_uuid: str, source_ref: str) -> bool:
        """True iff the row is bound for the active backend but its source read is dead."""
        return _sync.binding_is_dead(
            self._decks, self._driver, deck_uuid=deck_uuid, source_ref=source_ref
        )

    # ----------------------------------------------------------------------- #
    # Read (pull policy)
    # ----------------------------------------------------------------------- #

    def _needs_pull(self, deck_uuid: str) -> bool:
        """True when a synced deck should be (re-)pulled: absent OR TTL elapsed."""
        row = self._decks.get_row(deck_uuid)
        if row is None or self._decks.get(deck_uuid) is None:
            return True  # first access — no local copy yet.
        pulled_at = self._pulled_at(row.freshness)
        if pulled_at is None:
            return True
        return datetime.now(tz=UTC) - pulled_at >= _PULL_TTL

    @staticmethod
    def _pulled_at(freshness: str | None) -> datetime | None:
        """Parse the ``pulled_at`` stamp out of the local-only ``freshness`` JSON."""
        if not freshness:
            return None
        try:
            data = json.loads(freshness)
        except (json.JSONDecodeError, TypeError):
            return None
        raw = data.get('pulled_at') if isinstance(data, dict) else None
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def _stamp_pulled(self, deck_uuid: str) -> None:
        """Record the pull time in the local-only ``freshness`` column.

        Merge (not set): a plain re-pull must preserve the sibling ``assessment``
        stamp — ``set_freshness`` would clobber the whole blob and erase the
        cross-session assessment staleness at the very session boundary it exists
        for. Only the ``pulled_at`` key is touched.
        """
        self._decks.merge_freshness(deck_uuid, {'pulled_at': datetime.now(tz=UTC).isoformat()})

    def read_deck(self, name: str, *, id_prefix: str | None = None) -> Deck:
        """Return the deck for ``name`` (or ``--id`` prefix), applying the pull-TTL policy.

        Synced + (absent or stale) -> pull from the source (bound by the source's
        external ref, not by name), serve the fresh local copy. Synced + fresh ->
        serve the local copy. Ephemeral (no source) -> served straight from local.
        A deck not yet known locally is treated as synced and pulled.
        """
        # An explicit --id addresses an existing local row directly (no source
        # re-bind needed — the row already carries its binding).
        if id_prefix is not None:
            deck_uuid = self.resolve(id_prefix=id_prefix)
            return self._read_known_uuid(deck_uuid, name=name)

        deck_uuid = self.resolve(name)
        row = self._decks.get_row(deck_uuid)
        # Ephemeral decks (no source) are served straight from local.
        if row is not None and row.sync_status == 'ephemeral':
            local = self._decks.get(deck_uuid)
            if local is not None:
                return local
        if row is not None and not self._needs_pull(deck_uuid):
            local = self._decks.get(deck_uuid)
            if local is not None:
                return local
        # (Re-)pull: bind by the source's external ref so name aliases / renames
        # map to one local row (the bind-by-ref invariant). ``_pull_and_bind`` returns
        # the bound uuid.
        deck_uuid = self._pull_and_bind(name)
        self._stamp_pulled(deck_uuid)
        local = self._decks.get(deck_uuid)
        if local is None:  # pragma: no cover - pull just wrote it.
            return self._driver.get_deck(name)
        return local

    def resolve_for_write(self, name: str) -> str:
        """Resolve ``name`` to the one local ``deck_uuid`` it binds to (write-path).

        A case-alias / rename write must land on the same row a read binds to. The
        naive ``resolve(name)`` is exact-match against the canonical row name, so a
        cased alias misses and mints a fresh uuid — case-alias writes must land on the
        canonical row. This instead runs the read/bind path (``read_deck`` →
        ``_pull_and_bind`` binds the alias to the canonical row via the source's external
        ref) and returns the bound uuid, resolved by that external ref — never a
        freshly-minted hex.

        Ephemeral drafts and already-resolvable names short-circuit through the plain
        resolver; a genuine name-miss surfaces the resolver's clean error.
        """
        # A directly-resolvable name (an ephemeral draft, an already-bound synced
        # row, or a live source deck that resolves 1:1) needs no re-bind.
        rows = self._decks.rows_for_name(name)
        if len(rows) == 1:
            return rows[0].deck_uuid
        if len(rows) > 1:
            raise DecksError(self._ambiguous_name_message(name, rows))
        # No exact-name row: read the source (which binds the alias to the canonical
        # row by external ref) and return that bound uuid.
        source = self._read_source_for_name(name)
        ext = self._external_ref(source)
        if ext is not None:
            bound = self._decks.uuid_for_external_ref(*ext)
            if bound is not None:
                return self._refuse_alias_under_dup_names(name, bound)
        # Fall through to the read/bind path (mints + binds a fresh row for a
        # first-seen source deck), then resolve by the now-bound external ref.
        self.read_deck(name)
        if ext is not None:
            bound = self._decks.uuid_for_external_ref(*ext)
            if bound is not None:
                return self._refuse_alias_under_dup_names(name, bound)
        return self.resolve(name)

    def _refuse_alias_under_dup_names(self, name: str, bound: str) -> str:
        """Refuse a case-alias write that a slug read silently bound under dup names.

        The exact-name resolver refuses an ambiguous dup-name with the candidate list
        precisely because the system cannot know which row the user meant. A cased
        alias (``PRECIOUS`` vs two ``Precious`` rows) misses the exact match and would
        otherwise fall through the slug read to one row silently. Re-check the bound
        row's canonical name: if it now has >1 live candidate, refuse with the same
        candidate list instead of committing the alias to an arbitrary base pick.
        """
        row = self._decks.get_row(bound)
        if row is not None:
            siblings = self._decks.rows_for_name(row.name)
            if len(siblings) > 1:
                raise DecksError(self._ambiguous_name_message(row.name, siblings))
        return bound

    def _read_known_uuid(self, deck_uuid: str, *, name: str) -> Deck:
        """Serve a deck addressed by an existing uuid, applying the pull-TTL policy."""
        row = self._decks.get_row(deck_uuid)
        if row is not None and row.sync_status == 'ephemeral':
            local = self._decks.get(deck_uuid)
            if local is not None:
                return local
        if self._needs_pull(deck_uuid):
            source_ref = row.source_ref if row is not None and row.source_ref else name
            # ``_sync.pull`` reads the source through the one bound-read chokepoint:
            # a dup-named / renamed row addressed by --id pulls its own bound
            # file/record, never the base slug.
            _sync.pull(self._decks, self._driver, deck_uuid=deck_uuid, source_ref=source_ref)
            self._stamp_pulled(deck_uuid)
        local = self._decks.get(deck_uuid)
        if local is None:  # pragma: no cover
            return self._driver.get_deck(name)
        return local

    def _external_ref(self, source_deck: Deck) -> tuple[str, str] | None:
        """Return ``(backend, native_ref)`` — the source deck's stable binding key.

        Airtable: the recordId (globally unique, rename-proof). Local YAML: the
        in-file ``uuid``. The pair keys the one-row-per-ref binding. A source that
        carries neither (a legacy YAML that has not yet been assigned a uuid on read
        is impossible — ``get_deck`` always mints one) returns None.
        """
        if self.backend == 'airtable':
            rid = source_deck.airtable_record_id
            return ('airtable', rid) if rid else None
        return ('local', source_deck.uuid) if source_deck.uuid else None

    def _pull_and_bind(self, name: str) -> str:
        """Pull the source deck for ``name`` into the one local row bound to its ref.

        The bind-by-ref invariant: read the source, derive its external ref, and
        find-or-create the single local row for that ref — so two name aliases (case
        variants) or a file/name rename all resolve to the same row. Falls back to
        the name-resolved uuid when the source carries no external ref (should not
        happen for local YAML: ``get_deck`` always assigns a uuid).
        """
        source = self._read_source_for_name(name)
        ext = self._external_ref(source)
        if ext is not None:
            backend, native_ref = ext
            bound = self._decks.uuid_for_external_ref(backend, native_ref)
            if bound is None:
                # No row for this ref yet — reuse a name-resolved row if one exists
                # (e.g. a pre-binding local row), else mint from the source's own uuid
                # so the local identity matches the source's in-file uuid.
                bound = self._decks.uuid_for_name(name) or source.uuid
            # Persist the source we already read (bound by ref) rather than re-reading
            # by the ambiguous name in ``pull`` — a name/slug re-read could return an
            # unrelated same-named deck and clobber this bound row. ``version``
            # excludes uuid, so stamping the local PK leaves the baseline hash intact.
            stamped = source if source.uuid == bound else source.model_copy(update={'uuid': bound})
            self._decks.put(
                stamped,
                deck_uuid=bound,
                sync_status='synced',
                source_ref=name,
                synced_baseline=version(stamped),
                rationale=f'pull from {name}',
            )
            self._decks.set_external_id(bound, backend, native_ref)
            return bound
        deck_uuid = self.resolve(name)
        _sync.pull(self._decks, self._driver, deck_uuid=deck_uuid, source_ref=name)
        return deck_uuid

    def _read_source_for_name(self, name: str) -> Deck:
        """Read the source deck for ``name`` through the one bound-read chokepoint.

        When ``name`` resolves to a single existing local row, read that row's bound
        source (its Airtable recordId / local in-file uuid) — rename/slug-proof, so a
        promoted/dup-named/renamed row keeps reading its own file/record and never a
        same-named sibling. Only a genuine first-pull (no row for ``name`` yet) falls
        back to the controlled name read inside the chokepoint. A dead bound ref
        surfaces as the source's clean FileNotFoundError (source gone).
        """
        existing = self._decks.uuid_for_name(name)
        if existing is not None:
            source = _sync.read_bound_source(
                self._decks, self._driver, deck_uuid=existing, source_ref=name
            )
            if source is not None:
                return source
            # Distinguish a dead binding (a row exists and is bound, but its source is
            # gone/re-identified) from a genuine "no row" miss. Saying "no deck named
            # 'X'" would misdirect the user into re-creating it — the row and a local
            # copy exist; the source is what is gone. Point them at the one real
            # recovery: a fresh-identity ``save-deck`` (not "pull" — a dead ref cannot
            # be re-bound by pull; the chokepoint returns None by design).
            if _sync.binding_is_dead(self._decks, self._driver, deck_uuid=existing, source_ref=name):
                raise DecksError(self._dead_binding_message(name, existing))
            # The row is bound on another backend (a backend switch); its source
            # lives there, not as a same-named file here. Do not report "no deck"
            # (which would invite a clobbering recreate) — say where it is bound.
            if self._decks.bound_backends(existing) - {_sync._active_backend(self._driver)}:
                raise DecksError(
                    f"{name!r} is bound to a different backend than the active one ({self.backend}) — "
                    'switch back to its source backend to read it.'
                )
            raise DecksError(f'no deck named {name!r}')
        # Genuine first pull (no local row): the one controlled name-read fallback.
        # A miss is a clean "no deck named 'X'" — never the driver's tmp-fs path.
        try:
            return self._driver.get_deck(name)
        except FileNotFoundError:
            raise DecksError(f'no deck named {name!r}') from None

    # ----------------------------------------------------------------------- #
    # Write (local edits; push at the explicit commit boundary)
    # ----------------------------------------------------------------------- #

    def save_deck(
        self, deck: Deck, *, allow_shrink: bool = False, commit: bool = True, target_uuid: str | None = None
    ) -> None:
        """Write ``deck`` to the local store; push to the source at the commit boundary.

        ``save-deck`` is the user's authoritative write (arbitrary deck JSON), so it
        adopts the current source version as the baseline — the push is treated as
        our own authoritative write, not a drift (the shrink ceremony still guards
        it). Set ``commit=False`` to stage the local edit without touching the
        source.

        ``target_uuid`` (``save-deck --id`` recovery) pins the write to one
        already-resolved row (a dup-named dead row addressed by ``--id``), bypassing the
        name-based dup refusal (the caller already disambiguated by uuid). Only the pinned
        row is touched — it can never fork or clobber a sibling.
        """
        if target_uuid is None:
            # Refuse an ambiguous name (exact duplicate OR a case alias of one)
            # before anything is staged locally — content refused here must never
            # sit in the row for a later `sync` to land.
            self._refuse_dupname_save(deck.name)

        # Key the local row by the source's external ref when the source already
        # exists (bind to the one row), else by the deck's own uuid so a later read
        # binds by the same in-file uuid the push writes into the source (no dual
        # row). This is the write-side half of the bind-by-ref invariant.
        #
        # When a local row for this name already exists, read its current source
        # through the bound-read chokepoint: a re-save of a dup-named / renamed
        # deck derives its baseline from its own bound file/record, never the base
        # slug. Only a genuine first save (no row yet) name-reads the source to learn
        # whether one already exists on the backend.
        existing_uuid = target_uuid if target_uuid is not None else self._decks.uuid_for_name(deck.name)
        # A case-alias payload name on a dead row must not fork. Without this, a
        # ``save-deck EMBER`` while row ``Ember`` is dead has no exact-name row, slides
        # past recovery, and creates a new row+file, orphaning the dead zombie. Resolve
        # a case alias to its canonical dead-bound row and route through recovery of
        # that row (rename the payload to the canonical name) — a typo recovers, never
        # forks.
        if existing_uuid is None:
            aliased = self._alias_dead_row(deck.name)
            if aliased is not None:
                canonical_uuid, canonical_name = aliased
                existing_uuid = canonical_uuid
                if deck.name != canonical_name:
                    deck = deck.model_copy(update={'name': canonical_name})
        # An existing row bound on a different backend than the active one is not a
        # first-save create and not a same-backend recovery — its source lives on the
        # other backend. A save here would fall through to a first-save create that
        # adopts a same-named legacy file in place (a destruction path). Refuse with
        # the write-side cross-backend guard (mirrors the read's "switch back") before
        # recovery/first-save. This is the cross-backend clause only: a same-backend
        # dead binding is still the one thing save-deck recovers (below).
        active = _sync._active_backend(self._driver)
        if (
            existing_uuid is not None
            and (self._decks.bound_backends(existing_uuid) - {active})
            and not self._decks.external_ref(existing_uuid, active)
        ):
            raise DecksError(_sync.cross_backend_message(deck.name, self.backend))
        # Recovery: a save-deck onto a row whose bound source is gone/re-identified
        # recovers by creating a brand-new source at a fresh identity — it does not
        # refuse (edit/sync verbs still refuse; save-deck is the authoritative "write
        # this deck" verb and the one recovery path). Strip the stale external identity
        # so Airtable takes ``create_record`` (not update→422) and YAML mints a new
        # file; drop the row's dead ref; then fall through as a first-save create. It
        # can never adopt a same-named stranger because a dead binding reads back None
        # (source absent) — the name-read fallback is never entered for a bound-but-dead
        # row.
        recovering = existing_uuid is not None and _sync.binding_is_dead(
            self._decks, self._driver, deck_uuid=existing_uuid, source_ref=deck.name
        )
        if recovering:
            assert existing_uuid is not None
            deck = self._prepare_recovery(deck)
        # During recovery the source is (by definition) gone; do not name-read it — a
        # re-identified base slug would be a same-named stranger, and adopting it is
        # harmful. A fresh-identity create writes its own source.
        source = None if recovering else self._source_for_save(deck.name, existing_uuid)
        baseline = version(source) if source is not None else None
        deck_uuid = self._save_target_uuid(deck, source, existing_uuid=existing_uuid)
        pre_edit = self._decks.get(deck_uuid) if commit else None
        self._decks.put(deck, deck_uuid=deck_uuid, sync_status='synced', source_ref=deck.name,
                        synced_baseline=baseline, rationale='save-deck')
        # Bind to the source's ref when it exists. For a first save that is about to
        # commit, do not pre-bind to the deck's own (not-yet-written) uuid: the commit
        # ``push`` would then read the not-yet-created source as a dead binding and
        # refuse its own create. A committing first save binds after the push (the
        # re-bind block below). A stage-only (``commit=False``) first save keeps the
        # eager self-bind so a later read resolves the local identity.
        ext = self._external_ref(source) if source is not None else (
            None if commit else self._external_ref(deck)
        )
        if ext is not None:
            self._decks.set_external_id(deck_uuid, *ext)
        if commit:
            # Transactional: a refused commit must not leave the payload staged in the
            # row — roll the local edit back (or delete a first-save row) so a later
            # ``sync`` cannot land refused content.
            try:
                if recovering:
                    # Fresh-identity create — bypass ``push``'s current-source name-read
                    # (which would adopt a same-named stranger). The payload already
                    # carries a fresh identity (no ``airtable_record_id``, a fresh
                    # in-file uuid), so ``driver.save_deck`` creates a new record /
                    # mints a new disambiguated file rather than updating a dead ref.
                    self._commit_recovery(deck, deck_uuid, allow_shrink=allow_shrink)
                elif target_uuid is not None:
                    # A pinned save (``--id`` under dup names) must commit by the
                    # resolved uuid — ``push(deck.name)`` would re-refuse on name
                    # ambiguity, telling the user to do what they just did. Thread the
                    # pinned id through so ``--id`` composes end-to-end.
                    self.push(deck.name, allow_shrink=allow_shrink, id_prefix=deck_uuid)
                else:
                    self.push(deck.name, allow_shrink=allow_shrink)
            except Exception:
                self._rollback_save(deck_uuid, pre_edit)
                raise
            # Re-bind against the freshly-written source through the chokepoint (a
            # create mints/keeps the in-file uuid; Airtable assigns the recordId on
            # create) — never a base-slug name read. Recovery already bound the row to
            # the fresh identity in ``_commit_recovery`` (a name read here would adopt a
            # same-named stranger), so skip it.
            if not recovering:
                written = _sync.read_bound_source(
                    self._decks, self._driver, deck_uuid=deck_uuid, source_ref=deck.name
                )
                written_ext = self._external_ref(written) if written is not None else None
                if written_ext is not None:
                    self._decks.set_external_id(deck_uuid, *written_ext)

    def _refuse_dupname_save(self, name: str) -> None:
        """Refuse a dup-name ``save_deck`` up front — exact-name and case-alias.

        The exact-name check refuses ``save-deck Precious`` when two ``Precious`` rows
        exist. But a case alias (``save-deck PRECIOUS``) matches zero exact rows, would
        slide past the wall, and full-overwrite the base-slug file at exit 0. This
        resolves the alias to the one bound canonical row and refuses if that row's name
        has >1 live candidate — the same ``resolve_for_write``/``_refuse_alias_under_dup_names``
        wall the edit verbs use. An unambiguous name / a first-seen name passes through.
        """
        exact = self._decks.rows_for_name(name)
        if len(exact) > 1:
            raise DecksError(self._ambiguous_name_message(name, exact))
        if exact:
            return  # a single exact-name row — unambiguous, not an alias.
        # No exact-name row: does this name alias a bound canonical row that is one of a
        # dup set? Resolve the source's external ref and, if it binds a row whose name has
        # >1 live candidate, refuse with the candidate list (``_refuse_alias_under_dup_names``
        # raises). A genuine first-seen name (no source, or a source that binds a unique
        # row) must not be refused here — a first save is legitimate. So we swallow a
        # name-miss but re-raise a true ambiguity.
        try:
            source = self._read_source_for_name(name)
        except DecksError:
            return  # no source for this name yet — a genuine first save, not an alias.
        ext = self._external_ref(source)
        if ext is None:
            return
        bound = self._decks.uuid_for_external_ref(*ext)
        if bound is not None:
            self._refuse_alias_under_dup_names(name, bound)

    def _prepare_recovery(self, deck: Deck) -> Deck:
        """Return a fresh-identity payload for a recovery save.

        A ``save-deck`` onto a row whose bound source is gone/re-identified recovers by
        creating a brand-new source at a fresh identity:

        - drop ``airtable_record_id`` so the Airtable adapter takes ``create_record``
          (not ``update_record`` on the deleted record → 422);
        - mint a fresh in-file ``uuid`` so the local YAML adapter writes a new file.

        Atomic ref handling: this does not wipe the row's dead external ref up front.
        The wipe/replace happens only after the create succeeds (in
        ``_commit_recovery``). A create that fails therefore leaves the old dead ref
        intact, so ``binding_is_dead`` stays True and the honest refusal is restored —
        a retry cannot fall into the never-bound name-read fallback and adopt a
        same-named stranger. The create needs no wipe: this payload already carries a
        fresh identity (no ``airtable_record_id``, a fresh in-file uuid), so
        ``save_deck(force_fresh=True)`` creates rather than adopting the dead ref.
        """
        return deck.model_copy(update={'uuid': uuid4().hex, 'airtable_record_id': None})

    def _commit_recovery(self, deck: Deck, deck_uuid: str, *, allow_shrink: bool) -> None:
        """Create a fresh source for a recovery save, then bind the row to it.

        Bypasses ``push``'s current-source name-read (which would adopt a same-named
        stranger). The payload already carries a fresh identity, and ``force_fresh``
        forbids the adapter from adopting any existing same-named file (the local
        legacy-upgrade branch), so ``save_deck`` mints a new record / a new disambiguated
        file. The create runs first; only on success is the row's ref replaced with the
        fresh one — a failed create raises before any ref change, leaving the row
        bound-and-dead. Then re-read by the fresh identity (never a name read) to
        stamp the baseline.
        """
        from pipeline.decks.version import version as _version

        to_save = deck  # fresh identity already stamped by ``_prepare_recovery``.
        # Forced-fresh create — never adopt a same-named legacy backup in place.
        # This runs before any ref change; a failure raises here, ref untouched.
        self._driver.save_deck(to_save, allow_shrink=allow_shrink, force_fresh=True)
        expected = (
            to_save.airtable_record_id if self.backend == 'airtable' else to_save.uuid
        )
        written = _sync.read_bound_source(
            self._decks, self._driver, deck_uuid=deck_uuid,
            source_ref=deck.name, expected_ref=expected,
        )
        landed = written if written is not None else to_save
        # Create succeeded — now (and only now) replace the dead ref with the fresh one,
        # atomically with adopting the new source. A ``set_external_id`` merge would leave
        # the old dead ref alongside the new one, so replace the whole map with just the
        # fresh identity's ref (the ref transitions dead → fresh in one step).
        landed_ext = self._external_ref(landed)
        self._decks.put(
            landed, deck_uuid=deck_uuid, sync_status='synced', source_ref=deck.name,
            synced_baseline=_version(landed), rationale='save-deck (recover)',
        )
        if landed_ext is not None:
            self._decks.replace_external_ids(deck_uuid, {landed_ext[0]: landed_ext[1]})
        else:  # pragma: no cover - a landed source always carries an external ref.
            self._decks.replace_external_ids(deck_uuid, {})

    def _rollback_save(self, deck_uuid: str, pre_edit: Deck | None) -> None:
        """Undo a refused ``save_deck`` put: restore the pre-edit deck, else drop the row.

        A re-save over an existing row rolls back to its prior content (popping the
        poison ledger head, like the set-* verbs). A first save (no prior content)
        deletes the just-created row entirely so nothing refused lingers to be synced.
        """
        if pre_edit is not None:
            self._decks.rollback_failed_edit(deck_uuid, pre_edit)
        else:
            self._decks.delete(deck_uuid)

    def _source_for_save(self, name: str, existing_uuid: str | None) -> Deck | None:
        """Read the current source for a ``save_deck`` — bound when a row exists."""
        if existing_uuid is not None:
            return _sync.read_bound_source(
                self._decks, self._driver, deck_uuid=existing_uuid, source_ref=name
            )
        # Genuine first save: no local row yet, so a name read is the only address —
        # the chokepoint's own controlled first-pull fallback shape.
        try:
            return self._driver.get_deck(name)
        except FileNotFoundError:
            return None

    def _save_target_uuid(self, deck: Deck, source: Deck | None, *, existing_uuid: str | None = None) -> str:
        """Pick the local row uuid for a ``save_deck`` (bind to an existing ref if any).

        ``existing_uuid`` (an already-resolved row — a recovery target, possibly ``--id``
        pinned under dup names) takes precedence over the ambiguous ``uuid_for_name``
        tie-break so a dup-named recovery lands on the row the caller pinned, never the
        oldest same-named row.
        """
        if source is not None:
            ext = self._external_ref(source)
            if ext is not None:
                bound = self._decks.uuid_for_external_ref(*ext)
                if bound is not None:
                    return bound
        if existing_uuid is not None:
            return existing_uuid
        existing = self._decks.uuid_for_name(deck.name)
        if existing is not None:
            return existing
        return deck.uuid

    def set_strategy(self, name: str, text: str, *, commit: bool = True, id_prefix: str | None = None) -> None:
        deck_uuid, eff_name = self._target(name, id_prefix)
        pre_edit = self._decks.get(deck_uuid) if commit else None
        self._decks.set_strategy(deck_uuid, text, rationale='set-strategy')
        if commit:
            self._commit_transactional(deck_uuid, eff_name, pre_edit)

    def set_assessment(self, name: str, text: str, *, commit: bool = True, id_prefix: str | None = None) -> None:
        deck_uuid, eff_name = self._target(name, id_prefix)
        pre_edit = self._decks.get(deck_uuid) if commit else None
        self._decks.set_assessment(deck_uuid, text, rationale='set-assessment')
        if commit:
            self._commit_transactional(deck_uuid, eff_name, pre_edit)

    def set_focus_otags(
        self, name: str, otags: list[str], *, commit: bool = True, id_prefix: str | None = None
    ) -> None:
        deck_uuid, eff_name = self._target(name, id_prefix)
        pre_edit = self._decks.get(deck_uuid) if commit else None
        self._decks.set_focus_otags(deck_uuid, list(otags), rationale='set-focus-otags')
        if commit:
            self._commit_transactional(deck_uuid, eff_name, pre_edit)

    def _commit_transactional(self, deck_uuid: str, name: str, pre_edit: Deck | None) -> None:
        """Push a just-applied set-* edit; roll back the local edit if the push refuses.

        Aligns the ``set-*`` verbs with the ``deck-add`` family's transactional commit:
        a push-refused ``set-strategy`` (shrink guard / :class:`SyncDriftError`) must not
        leave the local edit half-applied under exit 1 — restore the pre-edit deck
        (popping the poison ledger head) and re-raise so the user sees why it was rejected.
        """
        try:
            self._commit(deck_uuid, name)
        except Exception:
            if pre_edit is not None:
                self._decks.rollback_failed_edit(deck_uuid, pre_edit)
            raise

    def _target(self, name: str | None, id_prefix: str | None) -> tuple[str, str]:
        """Resolve a verb's (name | --id) target to ``(deck_uuid, effective_name)``.

        Makes sure a local row exists (pull-current for a synced source deck) so a
        typed edit has a row to mutate. The effective name is the row's own name
        (the label under which the source push is addressed).
        """
        if id_prefix is not None:
            deck_uuid = self.resolve(id_prefix=id_prefix)
            row = self._decks.get_row(deck_uuid)
            eff_name = row.name if row is not None else (name or '')
            if row is None or (self._needs_pull(deck_uuid) and row.sync_status != 'ephemeral'):
                self._read_known_uuid(deck_uuid, name=eff_name)
            return deck_uuid, eff_name
        assert name is not None
        # Bind the (possibly aliased/renamed) name to the one canonical row and edit
        # that row (case-alias writes must land on the canonical row): ``resolve_for_write``
        # reads/binds by the source's external ref and returns the bound uuid, never a
        # freshly-minted hex.
        deck_uuid = self.resolve_for_write(name)
        return deck_uuid, name

    def _commit(self, deck_uuid: str, name: str, *, allow_shrink: bool = False) -> None:
        """Push a just-applied local edit to the source only when the deck is synced.

        The target's ephemerality decides: a synced deck commits through to its source
        of record at the edit boundary (else a later re-pull would silently revert the
        edit); an ephemeral draft has no source, so the edit stays purely local — that
        is the whole point of an exploration draft.
        """
        row = self._decks.get_row(deck_uuid)
        if row is not None and row.sync_status == 'synced' and row.source_ref is not None:
            _sync.push(self._decks, self._driver, deck_uuid=deck_uuid, allow_shrink=allow_shrink)

    # ----------------------------------------------------------------------- #
    # Manual sync verbs
    # ----------------------------------------------------------------------- #

    def pull(self, name: str, *, id_prefix: str | None = None) -> None:
        """Force a pull of ``name`` (or ``--id``) from the source into the local store."""
        if id_prefix is not None:
            deck_uuid = self.resolve(id_prefix=id_prefix)
            row = self._decks.get_row(deck_uuid)
            source_ref = row.source_ref if row is not None and row.source_ref else name
            if source_ref is None:
                raise DecksError(f'deck id prefix {id_prefix!r} has no source_ref to pull from')
            _sync.pull(self._decks, self._driver, deck_uuid=deck_uuid, source_ref=source_ref)
            self._stamp_pulled(deck_uuid)
            return
        # The name path must refuse a dup-name ambiguity, else ``pull Precious`` under
        # two Precious rows would silently re-pull the oldest. Refuse with the candidate
        # list (the same wall push/sync/save-deck hit).
        dup_rows = self._decks.rows_for_name(name)
        if len(dup_rows) > 1:
            raise DecksError(self._ambiguous_name_message(name, dup_rows))
        deck_uuid = self._pull_and_bind(name)
        self._stamp_pulled(deck_uuid)

    def push(
        self,
        name: str,
        *,
        allow_shrink: bool = False,
        id_prefix: str | None = None,
    ) -> None:
        """Push the local deck ``name`` (or ``--id``) to the source through the ceremony.

        A dead binding refuses. The one recovery is a fresh-identity ``save-deck`` —
        there is no ``--recreate`` override.
        """
        deck_uuid = self._resolve_existing(name, id_prefix)
        _sync.push(self._decks, self._driver, deck_uuid=deck_uuid, allow_shrink=allow_shrink)

    def _resolve_existing(self, name: str, id_prefix: str | None) -> str:
        """Resolve a push/sync target to an existing row — a name-miss is a clean error.

        A bare ``resolve(name)`` mints a fresh uuid on a miss, so ``push``/``sync`` of
        an unknown name would surface ``no deck with id '<hex>'`` — leaking a uuid the
        user never saw. An unknown name here is instead ``no deck named 'X'``.
        """
        if id_prefix is not None:
            return self.resolve(id_prefix=id_prefix)
        rows = self._decks.rows_for_name(name)
        if len(rows) == 1:
            return rows[0].deck_uuid
        if len(rows) > 1:
            raise DecksError(self._ambiguous_name_message(name, rows))
        raise DecksError(f'no deck named {name!r}')

    def sync(
        self,
        name: str,
        *,
        allow_shrink: bool = False,
        id_prefix: str | None = None,
    ) -> None:
        """Reconcile ``name`` (or ``--id``) against its source — pull / push / drift.

        Reconcile-not-destroy: compares local / baseline / current source and pulls
        (only source moved), pushes (only local moved), no-ops (neither), or raises
        ``SyncDriftError`` (both moved — nothing changed, both preserved). Never
        pull-clobbers an unpushed local edit. A dead binding refuses — the one recovery
        is a fresh-identity ``save-deck``, not a ``--recreate`` flag.
        """
        deck_uuid = self._resolve_existing(name, id_prefix)
        _sync.sync_reconcile(self._decks, self._driver, deck_uuid=deck_uuid, allow_shrink=allow_shrink)


def deck_access(driver: CollectionStore, *, decks: DecksStore | None = None) -> DeckAccess:
    """Construct a :class:`DeckAccess` over the given source driver (decks only)."""
    return DeckAccess(driver, decks=decks)
