"""The deck ACCESS path — route collection deck reads/writes through the local store.

Design §2/§8 + W1/W4: ALL deck access routes through the local ``DecksStore``,
but ONLY decks (inventory / chase / trades keep calling ``get_store`` directly —
they were never the brittle part and are NOT rerouted here).

Pull policy (W4 — the chosen policy):
    A SYNCED deck is pulled-current from its source on FIRST access (the local
    copy is absent) OR when a short TTL has elapsed since the last pull; the local
    copy is served thereafter. An EPHEMERAL deck is served straight from the local
    store (no source, zero network). Edits go to the LOCAL store; a PUSH to the
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

from pipeline.decks import sync as _sync
from pipeline.decks.store import DeckRow, DecksError, DecksStore
from pipeline.decks.version import version

if TYPE_CHECKING:
    from pipeline.collection.store import CollectionStore
    from pipeline.contracts import Deck

__all__ = ('DeckAccess', 'deck_access')

#: How long a synced local copy is served before a read re-pulls it (W4 TTL).
_PULL_TTL = timedelta(minutes=15)


class DeckAccess:
    """Route deck reads/writes through the local ``DecksStore`` over a source driver.

    Decks only (W1). The ``driver`` is the active source-of-record
    ``CollectionStore`` (built by ``get_store``); inventory/chase/trades are NOT
    handled here — callers use the driver directly for those.
    """

    def __init__(self, driver: CollectionStore, *, decks: DecksStore | None = None) -> None:
        self._driver = driver
        self._decks = decks if decks is not None else DecksStore()

    @property
    def backend(self) -> str:
        return self._driver.backend_name

    def resolve(self, name: str | None = None, *, id_prefix: str | None = None, create: bool = False) -> str:
        """Resolve a deck NAME (or an ``--id`` prefix) to its ``deck_uuid`` — the choke point.

        The single P2 resolution point every name-addressed verb calls (design
        §2/§3). Names are labels; ``deck_uuid`` is the identity.

        - ``id_prefix`` set (the ``--id`` escape hatch, PRECEDENCE over ``name``):
          prefix-match ``deck_uuid`` among live (non-consumed) rows — 0 -> error,
          1 -> that uuid, >1 -> an "ambiguous --id prefix" error.
        - ``name`` set: match live rows by name — 0 -> mint a NEW uuid (a create /
          a first read of a synced source deck about to be pulled), 1 -> that uuid,
          >1 -> REFUSE with the USER-DECIDED candidate list (a :class:`DecksError`
          that surfaces as the clean multi-line message, not a traceback).

        ``create`` is accepted for call-site intent symmetry; a miss mints either
        way. Exactly one of ``name`` / ``id_prefix`` must be given.
        """
        if id_prefix is not None:
            return self._resolve_id_prefix(id_prefix)
        if name is None:
            raise DecksError('resolve requires a deck name or an --id prefix')
        rows = self._decks.rows_for_name(name)
        if len(rows) == 1:
            return rows[0].deck_uuid
        if not rows:
            from uuid import uuid4

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
        """Build the USER-DECIDED dup-name candidate list (copy-paste ``--id`` lines).

        Format (verbatim, design §2): a header naming the count, then one
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
        """The dead-binding refusal, dup-aware (r9-M1): under dup names, name the ``--id``.

        A single-row dead deck recovers via ``save-deck "X"``; a DUP-named dead deck must
        be recovered by ``save-deck "X" --id <prefix>`` (a bare name would refuse with the
        ambiguity list). Detect the dup and hand the executable ``--id`` form to the message.
        """
        siblings = self._decks.rows_for_name(name)
        dup = len(siblings) > 1
        return _sync.dead_binding_message(
            self._driver, name, dup_names=dup, id_prefix=deck_uuid[:6] if dup else None
        )

    def _alias_dead_row(self, name: str) -> tuple[str, str] | None:
        """A case-alias of ``name`` that binds a SINGLE dead row (r9-m1 anti-fork).

        Returns ``(deck_uuid, canonical_name)`` when ``name`` is a case variant of exactly
        one live synced row whose binding is DEAD — so a ``save-deck EMBER`` typo recovers
        the dead ``Ember`` row instead of forking a new one. Returns None when there is no
        such alias, an EXACT-name row already exists (handled by the normal path), or the
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

    @staticmethod
    def _source_backend(row: DeckRow) -> str:
        """The candidate's source backend label: ``airtable`` if bound there, else ``local``."""
        try:
            ext = json.loads(row.external_ids) if row.external_ids else {}
        except (json.JSONDecodeError, TypeError):
            ext = {}
        if isinstance(ext, dict) and ext.get('airtable'):
            return 'airtable'
        return 'local'

    def has_local_row(self, deck_uuid: str) -> bool:
        """True iff a local decks-store row exists for ``deck_uuid`` (edit-path guard)."""
        return self._decks.exists(deck_uuid)

    # ----------------------------------------------------------------------- #
    # Local-copy recovery + inspection (r9-M1 — give the local copy an exit)
    # ----------------------------------------------------------------------- #

    def save_local(self, name: str, *, id_prefix: str | None = None, allow_shrink: bool = False) -> str:
        """RECOVER a deck from its LOCAL copy to a fresh source (r9-M1 — advised path).

        The dead-binding message advises ``save-deck "X"`` (or ``--id <prefix>`` under
        dup names). This makes that advice EXECUTABLE: read the deck's own local row's
        ``deck_json`` and re-save it — which routes through the forced-fresh, atomic
        recovery in :meth:`save_deck` (a dead binding recovers; a live binding is a
        normal authoritative re-save). NO ``--from-json`` needed and NO pull: the local
        copy IS the payload. Returns the deck name saved (for the CLI's echo).

        Addressing:

        - ``id_prefix`` set: address the ONE row by its ``deck_uuid`` prefix (the dup-name
          escape hatch) — resolves to a single row or raises the clean --id error.
        - ``name`` only: resolve the single row for ``name``; a dup-name refuses with the
          candidate list (which now names ``save-deck "X" --id <prefix>`` as the fix).
        """
        deck_uuid = self._resolve_local_for_recovery(name, id_prefix)
        local = self._decks.get(deck_uuid)
        if local is None:
            raise DecksError(f'no local copy to recover for {name!r} (nothing to save)')
        # Re-save the local copy under its OWN name (the row's canonical name) so a
        # case-alias payload can never fork the recovery to a new row (r9-m1). PIN the
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
        """Resolve a ``save-deck <name>``/``--id`` recovery target to a SINGLE local row.

        Never MINTS on a miss (a bare ``resolve`` would): a recovery must address an
        EXISTING local row (there is nothing to recover otherwise). A dup-name refuses
        with the candidate list (naming ``--id`` — which ``save-deck`` now accepts).
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
        """Serve the LOCAL copy directly — NO pull, NO source read (r9-M1 ``--local``).

        The dead-binding read refuses (the source is gone); ``get-deck --local`` gives the
        agent a way to SEE its deck under a dead binding without pulling (a dead ref cannot
        be pulled) and without being forced to reconstruct the JSON from the same-named
        stranger file at the slug. Addresses an EXISTING row only (name or ``--id``); a
        miss is a clean error, never a mint/pull.
        """
        deck_uuid = self._resolve_local_for_recovery(name, id_prefix)
        local = self._decks.get(deck_uuid)
        if local is None:
            raise DecksError(f'no local copy for {name!r}')
        return local

    def dead_bound_rows(self) -> list[DeckRow]:
        """Return the synced local rows whose bound source is DEAD (r9-M1 — list-decks).

        A dead-bound synced deck's source file/record is gone, so it drops out of the
        source ``list_decks`` enumeration and would VANISH from ``list-decks`` entirely.
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

        MERGE (not set): a plain re-pull must PRESERVE the sibling ``assessment``
        stamp (F8) — ``set_freshness`` would clobber the whole blob and erase the
        M7 cross-session assessment staleness at the very session boundary it exists
        for. Only the ``pulled_at`` key is touched.
        """
        self._decks.merge_freshness(deck_uuid, {'pulled_at': datetime.now(tz=UTC).isoformat()})

    def read_deck(self, name: str, *, id_prefix: str | None = None) -> Deck:
        """Return the deck for ``name`` (or ``--id`` prefix), applying the pull policy (W4).

        Synced + (absent or stale) -> pull from the source (BOUND by the source's
        external ref, not by name), serve the fresh local copy. Synced + fresh ->
        serve the local copy. Ephemeral (no source) -> served straight from local.
        A deck not yet known locally is treated as synced and pulled.
        """
        # An explicit --id addresses an EXISTING local row directly (no source
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
        # map to ONE local row (kills M4). ``_pull_bound`` returns the bound uuid.
        deck_uuid = self._pull_bound(name)
        self._stamp_pulled(deck_uuid)
        local = self._decks.get(deck_uuid)
        if local is None:  # pragma: no cover - pull just wrote it.
            return self._driver.get_deck(name)
        return local

    def resolve_bound(self, name: str) -> str:
        """Resolve ``name`` to the ONE local ``deck_uuid`` it BINDS to (M4 on the write path).

        A case-alias / rename write must land on the SAME row a read binds to. The
        naive ``resolve(name)`` is exact-match against the canonical row name, so a
        cased alias misses and mints a fresh uuid (F9: the M4-write regression). This
        instead runs the read/bind path (``read_deck`` → ``_pull_bound`` binds the
        alias to the canonical row via the source's external ref) and returns the
        BOUND uuid, resolved by that external ref — never a freshly-minted hex.

        Ephemeral drafts and already-resolvable names short-circuit through the plain
        resolver; a genuine name-miss surfaces the resolver's clean error (F13).
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
        source = self._read_source_rename_safe(name)
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
        """Refuse a case-alias WRITE that a slug read silently bound under dup names (r6-M1).

        The exact-name resolver REFUSES an ambiguous dup-name with the candidate list
        precisely because the system cannot know which row the user meant. A cased
        alias (``PRECIOUS`` vs two ``Precious`` rows) misses the exact match and would
        otherwise fall through the slug read to ONE row silently. Re-check the BOUND
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
        """Serve a deck addressed by an EXISTING uuid, applying the pull policy."""
        row = self._decks.get_row(deck_uuid)
        if row is not None and row.sync_status == 'ephemeral':
            local = self._decks.get(deck_uuid)
            if local is not None:
                return local
        if self._needs_pull(deck_uuid):
            source_ref = row.source_ref if row is not None and row.source_ref else name
            # ``_sync.pull`` reads the source through the ONE bound-read chokepoint
            # (r6-B1/M3): a dup-named / renamed row addressed by --id pulls ITS OWN
            # bound file/record, never the base slug.
            _sync.pull(self._decks, self._driver, deck_uuid=deck_uuid, source_ref=source_ref)
            self._stamp_pulled(deck_uuid)
        local = self._decks.get(deck_uuid)
        if local is None:  # pragma: no cover
            return self._driver.get_deck(name)
        return local

    def _external_ref(self, source_deck: Deck) -> tuple[str, str] | None:
        """Return ``(backend, native_ref)`` — the source deck's stable binding key.

        Airtable: the recordId (globally unique, rename-proof). Local YAML: the
        in-file ``uuid``. The pair keys the ONE-row-per-ref binding (design §4). A
        source that carries neither (a legacy YAML that has not yet been assigned a
        uuid on read is impossible — ``get_deck`` always mints one) returns None.
        """
        if self.backend == 'airtable':
            rid = source_deck.airtable_record_id
            return ('airtable', rid) if rid else None
        return ('local', source_deck.uuid) if source_deck.uuid else None

    def _pull_bound(self, name: str) -> str:
        """Pull the source deck for ``name`` into the ONE local row bound to its ref.

        The M4-killing mechanism (design §4): read the source, derive its external
        ref, and find-or-create the single local row for that ref — so two name
        aliases (case variants) or a file/name rename all resolve to the SAME row.
        Falls back to the name-resolved uuid when the source carries no external ref
        (should not happen for local YAML: ``get_deck`` always assigns a uuid).
        """
        source = self._read_source_rename_safe(name)
        ext = self._external_ref(source)
        if ext is not None:
            backend, native_ref = ext
            bound = self._decks.uuid_for_external_ref(backend, native_ref)
            if bound is None:
                # No row for this ref yet — reuse a name-resolved row if one exists
                # (e.g. a pre-binding local row), else mint from the source's own uuid
                # so the local identity matches the source's in-file uuid.
                bound = self._decks.uuid_for_name(name) or source.uuid
            # Persist the SOURCE we already read (bound by ref) rather than re-reading
            # by the ambiguous name in ``pull`` — a name/slug re-read could return an
            # UNRELATED same-named deck and clobber this bound row (F4). ``version``
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

    def _read_source_rename_safe(self, name: str) -> Deck:
        """Read the source deck for ``name`` through the ONE bound-read chokepoint (P9).

        When ``name`` resolves to a SINGLE existing local row, read that row's BOUND
        source (its Airtable recordId / local in-file uuid) — rename/slug-proof, so a
        promoted/dup-named/renamed row keeps reading ITS OWN file/record and never a
        same-named sibling (r6-B1/B2). Only a genuine first-pull (no row for ``name``
        yet) falls back to the controlled NAME read inside the chokepoint. A dead
        bound ref surfaces as the source's clean FileNotFoundError (source gone).
        """
        existing = self._decks.uuid_for_name(name)
        if existing is not None:
            source = _sync.read_source_bound(
                self._decks, self._driver, deck_uuid=existing, source_ref=name
            )
            if source is not None:
                return source
            # r7-m1: distinguish a DEAD binding (a row exists AND is bound, but its
            # source is gone/re-identified) from a genuine "no row" miss. Saying "no
            # deck named 'X'" would misdirect the user into re-creating it — the row
            # and a local copy exist; the SOURCE is what is gone. Point them at the ONE
            # real recovery (P11): a fresh-identity ``save-deck`` (NOT "pull" — a dead
            # ref cannot be re-bound by pull; the chokepoint returns None by design).
            if _sync.binding_is_dead(self._decks, self._driver, deck_uuid=existing, source_ref=name):
                raise DecksError(self._dead_binding_message(name, existing))
            # r7-m4: the row is bound on ANOTHER backend (a backend switch); its source
            # lives there, not as a same-named file here. Do NOT report "no deck" (which
            # would invite a clobbering recreate) — say where it is bound.
            if _sync._bound_on_other_backend(
                self._decks, existing, self.backend if self.backend == 'airtable' else 'local'
            ):
                raise DecksError(
                    f"{name!r} is bound to a different backend than the active one ({self.backend}) — "
                    'switch back to its source backend to read it.'
                )
            raise DecksError(f'no deck named {name!r}')
        # Genuine first pull (no local row): the ONE controlled name-read fallback.
        # A miss is a clean "no deck named 'X'" — never the driver's tmp-fs path
        # (r6-m1 / F13 residue: get-deck/deck-add/set-strategy/pull/deck-swap/undo).
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
        """Write ``deck`` to the local store; PUSH to the source at the commit boundary.

        ``save-deck`` is the user's AUTHORITATIVE write (arbitrary deck JSON), so it
        adopts the current source version as the baseline — the push is treated as
        our own authoritative write, not a drift (the shrink ceremony still guards
        it). Set ``commit=False`` to stage the local edit without touching the
        source.

        ``target_uuid`` (r9-M1 — ``save-deck --id`` recovery) PINS the write to ONE
        already-resolved row (a dup-named dead row addressed by ``--id``), bypassing the
        NAME-based dup refusal (the caller already disambiguated by uuid). Only the pinned
        row is touched — it can never fork or clobber a sibling.
        """
        if target_uuid is None:
            # r7-M1 + r8-M1: run the dup-name refusal at the TOP, BEFORE any put — and make
            # it ALIAS-AWARE. The old order ``put`` the payload into the oldest row (silent
            # ``uuid_for_name`` tie-break) and only refused later inside ``push``, staging
            # refused content a later ``sync`` could land. And the EXACT-name check (r8-M1)
            # missed a CASE alias: ``save-deck PRECIOUS`` under two ``Precious`` rows matched
            # zero rows, slid past the wall, and full-overwrote the base-slug file at exit 0.
            # Resolve the alias to its bound canonical row and refuse if THAT row is one of a
            # dup set — the same wall the edit verbs use (``resolve_bound`` shape).
            self._refuse_dupname_save(deck.name)

        # Key the local row by the source's external ref when the source already
        # exists (bind to the ONE row), else by the deck's OWN uuid so a later read
        # binds by the same in-file uuid the push writes into the source (no dual
        # row). This is the write-side half of the M4 bind-by-ref invariant.
        #
        # When a local row for this name already exists, read its CURRENT source
        # through the bound-read chokepoint (P9): a re-save of a dup-named / renamed
        # deck derives its baseline from ITS OWN bound file/record, never the base
        # slug. Only a genuine first save (no row yet) name-reads the source to learn
        # whether one already exists on the backend.
        existing_uuid = target_uuid if target_uuid is not None else self._decks.uuid_for_name(deck.name)
        # r9-m1: a CASE-ALIAS payload name on a dead row must not FORK. ``save-deck EMBER``
        # while row ``Ember`` is dead had no exact-name row, so it slid past recovery and
        # created a NEW row+file, orphaning the dead zombie. Resolve a case alias to its
        # canonical dead-bound row and route THROUGH recovery of that row (rename the
        # payload to the canonical name) — a typo recovers, never forks.
        if existing_uuid is None:
            aliased = self._alias_dead_row(deck.name)
            if aliased is not None:
                canonical_uuid, canonical_name = aliased
                existing_uuid = canonical_uuid
                if deck.name != canonical_name:
                    deck = deck.model_copy(update={'name': canonical_name})
        # P11 RECOVERY (replaces P10's --recreate): a save-deck onto a row whose bound
        # source is gone/re-identified RECOVERS by creating a BRAND-NEW source at a fresh
        # identity — it does NOT refuse (edit/sync verbs still refuse; save-deck is the
        # authoritative "write this deck" verb and the one recovery path). Strip the
        # stale external identity so Airtable takes ``create_record`` (not update→422)
        # and YAML mints a new file; drop the row's dead ref; then fall through as a
        # first-save create. It can NEVER adopt a same-named stranger because a dead
        # binding reads back None (source absent) — the name-read fallback is never
        # entered for a bound-but-dead row.
        recovering = existing_uuid is not None and _sync.binding_is_dead(
            self._decks, self._driver, deck_uuid=existing_uuid, source_ref=deck.name
        )
        if recovering:
            assert existing_uuid is not None
            deck = self._prepare_recovery(deck)
        # During recovery the source is (by definition) gone; do NOT name-read it — a
        # re-identified base slug would be a same-named STRANGER, and adopting it is the
        # exact r8-M3 harm. A fresh-identity create writes its own source.
        source = None if recovering else self._source_for_save(deck.name, existing_uuid)
        baseline = version(source) if source is not None else None
        deck_uuid = self._save_target_uuid(deck, source, existing_uuid=existing_uuid)
        pre_edit = self._decks.get(deck_uuid) if commit else None
        self._decks.put(deck, deck_uuid=deck_uuid, sync_status='synced', source_ref=deck.name,
                        synced_baseline=baseline, rationale='save-deck')
        # Bind to the source's ref when it EXISTS. For a FIRST save that is about to
        # COMMIT, do NOT pre-bind to the deck's own (not-yet-written) uuid: the commit
        # ``push`` would then read the not-yet-created source as a DEAD binding (P10)
        # and refuse its own create. A committing first save binds AFTER the push
        # (the re-bind block below). A stage-only (``commit=False``) first save keeps
        # the eager self-bind so a later read resolves the local identity.
        ext = self._external_ref(source) if source is not None else (
            None if commit else self._external_ref(deck)
        )
        if ext is not None:
            self._decks.set_external_id(deck_uuid, *ext)
        if commit:
            # Transactional (r7-M1 tail): a refused commit must NOT leave the payload
            # staged in the row — roll the local edit back (or delete a first-save row)
            # so a later ``sync`` cannot land refused content.
            try:
                if recovering:
                    # Fresh-identity CREATE — bypass ``push``'s current-source name-read
                    # (which would adopt a same-named stranger, r8-M3). The payload
                    # already carries a fresh identity (no ``airtable_record_id``, a
                    # fresh in-file uuid), so ``driver.save_deck`` creates a NEW record /
                    # mints a NEW disambiguated file rather than updating a dead ref.
                    self._commit_recovery(deck, deck_uuid, allow_shrink=allow_shrink)
                else:
                    self.push(deck.name, allow_shrink=allow_shrink)
            except Exception:
                self._rollback_save(deck_uuid, pre_edit)
                raise
            # Re-bind against the freshly-written source through the chokepoint (a
            # create mints/keeps the in-file uuid; Airtable assigns the recordId on
            # create) — never a base-slug name read. Recovery already bound the row to
            # the fresh identity in ``_commit_recovery`` (a name read here would adopt a
            # same-named stranger, r8-M3), so skip it.
            if not recovering:
                written = _sync.read_source_bound(
                    self._decks, self._driver, deck_uuid=deck_uuid, source_ref=deck.name
                )
                written_ext = self._external_ref(written) if written is not None else None
                if written_ext is not None:
                    self._decks.set_external_id(deck_uuid, *written_ext)

    def _refuse_dupname_save(self, name: str) -> None:
        """Refuse a dup-name ``save_deck`` up front — EXACT-name AND case-alias (r8-M1).

        The exact-name check (r7-M1) refuses ``save-deck Precious`` when two ``Precious``
        rows exist. But a CASE ALIAS (``save-deck PRECIOUS``) matched zero exact rows,
        slid past the wall, and full-overwrote the base-slug file at exit 0 (r8-M1). This
        resolves the alias to the ONE bound canonical row and refuses if THAT row's name
        has >1 live candidate — the same ``resolve_bound``/``_refuse_alias_under_dup_names``
        wall the edit verbs use. An unambiguous name / a first-seen name passes through.
        """
        exact = self._decks.rows_for_name(name)
        if len(exact) > 1:
            raise DecksError(self._ambiguous_name_message(name, exact))
        if exact:
            return  # a single exact-name row — unambiguous, not an alias.
        # No exact-name row: does this name ALIAS a bound canonical row that is one of a
        # dup set? Resolve the source's external ref and, if it binds a row whose name has
        # >1 live candidate, refuse with the candidate list (``_refuse_alias_under_dup_names``
        # raises). A genuine first-seen name (no source, or a source that binds a unique
        # row) must NOT be refused here — a first save is legitimate. So we swallow a
        # name-MISS but re-raise a true ambiguity.
        try:
            source = self._read_source_rename_safe(name)
        except DecksError:
            return  # no source for this name yet — a genuine first save, not an alias.
        ext = self._external_ref(source)
        if ext is None:
            return
        bound = self._decks.uuid_for_external_ref(*ext)
        if bound is not None:
            self._refuse_alias_under_dup_names(name, bound)

    def _prepare_recovery(self, deck: Deck) -> Deck:
        """Return a FRESH-identity payload for a recovery save (P11 + r9-B2).

        A ``save-deck`` onto a row whose bound source is gone/re-identified recovers by
        creating a BRAND-NEW source at a fresh identity:

        - drop ``airtable_record_id`` so the Airtable adapter takes ``create_record``
          (not ``update_record`` on the deleted record → 422, the r8-M2 wedge);
        - mint a FRESH in-file ``uuid`` so the local YAML adapter writes a NEW file.

        r9-B2 — ATOMIC ref handling: this NO LONGER wipes the row's dead external ref
        up front. The wipe/replace happens ONLY after the create succeeds (in
        ``_commit_recovery``). A create that FAILS therefore leaves the OLD dead ref
        intact, so ``binding_is_dead`` stays True and the honest refusal is restored —
        a retry cannot fall into the never-bound name-read fallback and adopt a
        same-named stranger. The create needs no wipe: this payload already carries a
        fresh identity (no ``airtable_record_id``, a fresh in-file uuid), so
        ``save_deck(force_fresh=True)`` creates rather than adopting the dead ref.
        """
        from uuid import uuid4

        return deck.model_copy(update={'uuid': uuid4().hex, 'airtable_record_id': None})

    def _commit_recovery(self, deck: Deck, deck_uuid: str, *, allow_shrink: bool) -> None:
        """Create a fresh source for a recovery save, then bind the row to it (P11 + r9).

        Bypasses ``push``'s current-source name-read (which would adopt a same-named
        STRANGER, r8-M3). The payload already carries a fresh identity, and ``force_fresh``
        forbids the adapter from adopting ANY existing same-named file (r9-B1: the local
        legacy-upgrade branch), so ``save_deck`` mints a NEW record / a NEW disambiguated
        file. The create runs FIRST; only on SUCCESS is the row's ref REPLACED with the
        fresh one — a failed create raises before any ref change, leaving the row
        bound-and-dead (r9-B2). Then re-read by the fresh identity (never a name read) to
        stamp the baseline.
        """
        from pipeline.decks.version import version as _version

        to_save = deck  # fresh identity already stamped by ``_prepare_recovery``.
        # r9-B1: FORCED-FRESH create — never adopt a same-named legacy backup in place.
        # r9-B2: this runs BEFORE any ref change; a failure raises here, ref untouched.
        self._driver.save_deck(to_save, allow_shrink=allow_shrink, force_fresh=True)
        expected = (
            to_save.airtable_record_id if self.backend == 'airtable' else to_save.uuid
        )
        written = _sync.read_source_bound(
            self._decks, self._driver, deck_uuid=deck_uuid,
            source_ref=deck.name, expected_ref=expected,
        )
        landed = written if written is not None else to_save
        # Create SUCCEEDED — now (and only now) REPLACE the dead ref with the fresh one,
        # atomically with adopting the new source. A ``set_external_id`` merge would leave
        # the OLD dead ref alongside the new one, so replace the whole map with just the
        # fresh identity's ref (r9-B2: the ref transitions dead → fresh in one step).
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
        poison ledger head, like the set-* verbs). A FIRST save (no prior content)
        deletes the just-created row entirely so nothing refused lingers to be synced.
        """
        if pre_edit is not None:
            self._decks.rollback_failed_edit(deck_uuid, pre_edit)
        else:
            self._decks.delete(deck_uuid)

    def _source_for_save(self, name: str, existing_uuid: str | None) -> Deck | None:
        """Read the CURRENT source for a ``save_deck`` — bound when a row exists (P9)."""
        if existing_uuid is not None:
            return _sync.read_source_bound(
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
        tie-break so a dup-named recovery lands on the ROW THE CALLER PINNED, never the
        oldest same-named row (r9-M1).
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
        """Push a just-applied set-* edit; ROLL BACK the local edit if the push refuses (r6-m6).

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
        # Bind the (possibly aliased/renamed) name to the ONE canonical row and edit
        # THAT row (F9 — the M4 write-path fix): ``resolve_bound`` reads/binds by the
        # source's external ref and returns the bound uuid, never a freshly-minted hex.
        deck_uuid = self.resolve_bound(name)
        return deck_uuid, name

    def _commit(self, deck_uuid: str, name: str, *, allow_shrink: bool = False) -> None:
        """Push a just-applied local edit to the source ONLY when the deck is synced.

        The target's ephemerality decides (design §4): a SYNCED deck commits through
        to its source of record at the edit boundary (else a later W4 re-pull would
        silently revert the edit); an EPHEMERAL draft has no source, so the edit
        stays purely local — that is the whole point of an exploration draft.
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
        # r7-m2 (= r6-m3): the name path had NO dup-name ambiguity refusal, so
        # ``pull Precious`` under two Precious rows silently re-pulled the oldest.
        # Refuse with the candidate list (the same wall push/sync/save-deck hit).
        dup_rows = self._decks.rows_for_name(name)
        if len(dup_rows) > 1:
            raise DecksError(self._ambiguous_name_message(name, dup_rows))
        deck_uuid = self._pull_bound(name)
        self._stamp_pulled(deck_uuid)

    def push(
        self,
        name: str,
        *,
        allow_shrink: bool = False,
        id_prefix: str | None = None,
    ) -> None:
        """Push the local deck ``name`` (or ``--id``) to the source through the ceremony.

        A DEAD binding REFUSES (P10). The one recovery is a fresh-identity
        ``save-deck`` (P11) — there is no ``--recreate`` override.
        """
        deck_uuid = self._resolve_existing(name, id_prefix)
        _sync.push(self._decks, self._driver, deck_uuid=deck_uuid, allow_shrink=allow_shrink)

    def _resolve_existing(self, name: str, id_prefix: str | None) -> str:
        """Resolve a push/sync target to an EXISTING row — a name-miss is a clean error.

        A bare ``resolve(name)`` MINTS a fresh uuid on a miss, so ``push``/``sync`` of
        an unknown name would surface ``no deck with id '<hex>'`` — leaking a uuid the
        user never saw (r6-m1). An unknown name here is instead ``no deck named 'X'``.
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
        """Reconcile ``name`` (or ``--id``) against its source — pull / push / drift (M2).

        RECONCILE-not-destroy (design §4): compares local / baseline / current source
        and pulls (only source moved), pushes (only local moved), no-ops (neither),
        or raises ``SyncDriftError`` (both moved — nothing changed, both preserved).
        Never pull-clobbers an unpushed local edit. A DEAD binding REFUSES (P10) — the
        one recovery is a fresh-identity ``save-deck`` (P11), not a ``--recreate`` flag.
        """
        deck_uuid = self._resolve_existing(name, id_prefix)
        _sync.sync_reconcile(self._decks, self._driver, deck_uuid=deck_uuid, allow_shrink=allow_shrink)


def deck_access(driver: CollectionStore, *, decks: DecksStore | None = None) -> DeckAccess:
    """Construct a :class:`DeckAccess` over the given source driver (decks only)."""
    return DeckAccess(driver, decks=decks)
