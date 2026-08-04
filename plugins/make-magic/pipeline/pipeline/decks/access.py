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
        """Record the pull time in the local-only ``freshness`` column."""
        self._decks.set_freshness(deck_uuid, {'pulled_at': datetime.now(tz=UTC).isoformat()})

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

    def _read_known_uuid(self, deck_uuid: str, *, name: str) -> Deck:
        """Serve a deck addressed by an EXISTING uuid, applying the pull policy."""
        row = self._decks.get_row(deck_uuid)
        if row is not None and row.sync_status == 'ephemeral':
            local = self._decks.get(deck_uuid)
            if local is not None:
                return local
        if self._needs_pull(deck_uuid):
            source_ref = row.source_ref if row is not None and row.source_ref else name
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
            _sync.pull(self._decks, self._driver, deck_uuid=bound, source_ref=name)
            self._decks.set_external_id(bound, backend, native_ref)
            return bound
        deck_uuid = self.resolve(name)
        _sync.pull(self._decks, self._driver, deck_uuid=deck_uuid, source_ref=name)
        return deck_uuid

    def _read_source_rename_safe(self, name: str) -> Deck:
        """Read the source deck for ``name``, preferring a known bound recordId (Airtable).

        A source deck RENAMED on the backend would 404 on a name read; when a local
        row addressed by ``name`` already carries a bound Airtable recordId, re-read
        by that stable id (rename-proof, design §4) and fall back to the name read
        on any miss. The local YAML backend is name/slug-addressed and rename-safe
        via the in-file uuid scan, so this only augments the Airtable path.
        """
        if self.backend == 'airtable':
            get_by_id = getattr(self._driver, 'get_deck_by_record_id', None)
            existing = self._decks.uuid_for_name(name)
            if get_by_id is not None and existing is not None:
                ext_raw = self._decks.external_ids(existing)
                try:
                    ext = json.loads(ext_raw) if ext_raw else {}
                except (json.JSONDecodeError, TypeError):
                    ext = {}
                rid = ext.get('airtable') if isinstance(ext, dict) else None
                if rid:
                    try:
                        return get_by_id(rid)
                    except FileNotFoundError:
                        pass
        return self._driver.get_deck(name)

    # ----------------------------------------------------------------------- #
    # Write (local edits; push at the explicit commit boundary)
    # ----------------------------------------------------------------------- #

    def save_deck(self, deck: Deck, *, allow_shrink: bool = False, commit: bool = True) -> None:
        """Write ``deck`` to the local store; PUSH to the source at the commit boundary.

        ``save-deck`` is the user's AUTHORITATIVE write (arbitrary deck JSON), so it
        adopts the current source version as the baseline — the push is treated as
        our own authoritative write, not a drift (the shrink ceremony still guards
        it). Set ``commit=False`` to stage the local edit without touching the
        source.
        """
        # Key the local row by the source's external ref when the source already
        # exists (bind to the ONE row), else by the deck's OWN uuid so a later read
        # binds by the same in-file uuid the push writes into the source (no dual
        # row). This is the write-side half of the M4 bind-by-ref invariant.
        try:
            source = self._driver.get_deck(deck.name)
        except FileNotFoundError:
            source = None
        baseline = version(source) if source is not None else None
        deck_uuid = self._save_target_uuid(deck, source)
        self._decks.put(deck, deck_uuid=deck_uuid, sync_status='synced', source_ref=deck.name,
                        synced_baseline=baseline, rationale='save-deck')
        ext = self._external_ref(source) if source is not None else self._external_ref(deck)
        if ext is not None:
            self._decks.set_external_id(deck_uuid, *ext)
        if commit:
            self.push(deck.name, allow_shrink=allow_shrink)
            # Re-bind against the freshly-written source (a create mints/keeps the
            # in-file uuid; the recordId is assigned by Airtable on create).
            written_ext = self._external_ref(self._driver.get_deck(deck.name))
            if written_ext is not None:
                self._decks.set_external_id(deck_uuid, *written_ext)

    def _save_target_uuid(self, deck: Deck, source: Deck | None) -> str:
        """Pick the local row uuid for a ``save_deck`` (bind to an existing ref if any)."""
        if source is not None:
            ext = self._external_ref(source)
            if ext is not None:
                bound = self._decks.uuid_for_external_ref(*ext)
                if bound is not None:
                    return bound
        existing = self._decks.uuid_for_name(deck.name)
        if existing is not None:
            return existing
        return deck.uuid

    def set_strategy(self, name: str, text: str, *, commit: bool = True, id_prefix: str | None = None) -> None:
        deck_uuid, eff_name = self._target(name, id_prefix)
        self._decks.set_strategy(deck_uuid, text, rationale='set-strategy')
        if commit:
            self._commit(deck_uuid, eff_name)

    def set_assessment(self, name: str, text: str, *, commit: bool = True, id_prefix: str | None = None) -> None:
        deck_uuid, eff_name = self._target(name, id_prefix)
        self._decks.set_assessment(deck_uuid, text, rationale='set-assessment')
        if commit:
            self._commit(deck_uuid, eff_name)

    def set_focus_otags(
        self, name: str, otags: list[str], *, commit: bool = True, id_prefix: str | None = None
    ) -> None:
        deck_uuid, eff_name = self._target(name, id_prefix)
        self._decks.set_focus_otags(deck_uuid, list(otags), rationale='set-focus-otags')
        if commit:
            self._commit(deck_uuid, eff_name)

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
        self.read_deck(name)  # pull-current / mint a row
        return self.resolve(name), name

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
        deck_uuid = self._pull_bound(name)
        self._stamp_pulled(deck_uuid)

    def push(self, name: str, *, allow_shrink: bool = False, id_prefix: str | None = None) -> None:
        """Push the local deck ``name`` (or ``--id``) to the source through the ceremony."""
        deck_uuid = self.resolve(id_prefix=id_prefix) if id_prefix is not None else self.resolve(name)
        _sync.push(self._decks, self._driver, deck_uuid=deck_uuid, allow_shrink=allow_shrink)

    def sync(self, name: str, *, allow_shrink: bool = False, id_prefix: str | None = None) -> None:
        """Reconcile ``name`` (or ``--id``) against its source — pull / push / drift (M2).

        RECONCILE-not-destroy (design §4): compares local / baseline / current source
        and pulls (only source moved), pushes (only local moved), no-ops (neither),
        or raises ``SyncDriftError`` (both moved — nothing changed, both preserved).
        Never pull-clobbers an unpushed local edit.
        """
        deck_uuid = self.resolve(id_prefix=id_prefix) if id_prefix is not None else self.resolve(name)
        _sync.sync_reconcile(self._decks, self._driver, deck_uuid=deck_uuid, allow_shrink=allow_shrink)


def deck_access(driver: CollectionStore, *, decks: DecksStore | None = None) -> DeckAccess:
    """Construct a :class:`DeckAccess` over the given source driver (decks only)."""
    return DeckAccess(driver, decks=decks)
