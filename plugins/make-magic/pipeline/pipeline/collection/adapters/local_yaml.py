"""Local YAML `CollectionStore` adapter — hand-editable YAML as source of record.

Implements the `CollectionStore` port over YAML files under the `collection/`
dir (resolved off `StorePaths`). Thin persistence: only the non-derivable facts
(ownership / intent / membership + card ref) are written; the base-`Card`
enrichment is HYDRATED on read via an injected `CardResolver` (unresolved cards
read back name-only with null enrichment).

Layout:

    collection/
      decks/<slug>.yaml   # self-contained: name, strategy, airtable_record_id, cards[]
      inventory.yaml      # owned-facts only:  [{card, owned, foil, condition, sets, sources}]
      chase.yaml          # intent-facts only: [{card, priority, for_decks, status, target_price}]
      trades.yaml         # movement events

This module MUST NOT import scripts/ — the concrete Scryfall-cache resolver is
injected at the edge; here we only depend on the `CardResolver` Protocol.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import uuid4

import yaml

from pipeline import store as _store
from pipeline.collection.errors import CollectionError
from pipeline.collection.guards import check_remove_allowed, shrink_check, shrink_refusal_message
from pipeline.contracts import ChaseCard, Deck, DeckCard, OwnedCard, Trade

if TYPE_CHECKING:
    from pipeline.collection.store import CardResolver

_SLUG_STRIP = re.compile(r'[^a-z0-9]+')

#: The protective comment written above the in-file ``uuid``. ``safe_dump`` cannot
#: emit comments, so the header is injected manually and the ``uuid`` is dumped as
#: the first key so the comment sits directly above it.
_UUID_COMMENT = (
    "# This is the deck's permanent ID (used to keep copies in sync).\n"
    '# Please leave it as-is — renaming the FILE is fine, editing this is not.\n'
)


def _slugify(name: str) -> str:
    """Deck name -> filename slug, e.g. 'Gruul Aggro' -> 'gruul-aggro'."""
    return _SLUG_STRIP.sub('-', name.lower()).strip('-')


def _validate_card_entry(entry: Any, allowed: set[str], *, path: Path, index: int) -> dict[str, Any]:
    """Validate one hand-editable card row, turning silent corruption into a loud error.

    PyYAML flow style ``{ card: Grumgully, the Generous, role: commander }`` parses
    to ``{'card': 'Grumgully', 'the Generous': None, 'role': 'commander'}`` — a
    silent WRONG-identity read. Reject any entry that is not a mapping, carries an
    unexpected key, or carries a null-valued key (the flow-comma tell), with an
    actionable message telling the user to quote comma-containing card names.

    ``allowed`` is the set of known keys for this row type (always includes
    ``card``). Returns the entry unchanged when valid.
    """
    if not isinstance(entry, dict):
        raise CollectionError(
            f'Malformed card entry #{index} in {path}: expected a mapping, got {type(entry).__name__}.'
        )
    unexpected = sorted(k for k in entry if k not in allowed)
    null_valued = sorted(k for k, v in entry.items() if v is None and k != 'card')
    if unexpected or null_valued:
        offending = sorted(set(unexpected) | set(null_valued))
        raise CollectionError(
            f'Malformed card entry #{index} in {path}: unexpected/empty key(s) {offending!r}. '
            f'This usually means a card name containing a comma was left unquoted in YAML flow '
            f'style (e.g. `{{ card: Grumgully, the Generous }}`). Quote such names: '
            f'`- card: "Grumgully, the Generous"`. Known keys: {sorted(allowed)!r}.'
        )
    if 'card' not in entry:
        raise CollectionError(f'Malformed card entry #{index} in {path}: missing required `card` key.')
    return entry


class LocalYamlStore:
    """The local YAML implementation of `CollectionStore`."""

    def __init__(self, resolver: CardResolver, *, collection_root: Path | None = None) -> None:
        self._resolver = resolver
        #: Optional explicit ``collection/`` dir. When None, resolved off the
        #: env-driven ``StorePaths`` (the normal case); an explicit root lets the
        #: one-shot ``copy`` point a destination at a distinct dir without env
        #: juggling (source + dest can differ within one process).
        self._collection_root = collection_root

    # --- paths / io helpers -------------------------------------------------- #

    @property
    def _root(self) -> Path:
        if self._collection_root is not None:
            return self._collection_root
        return _store.StorePaths.resolve().collection

    def _inventory_path(self) -> Path:
        return self._root / 'inventory.yaml'

    def _chase_path(self) -> Path:
        return self._root / 'chase.yaml'

    def _trades_path(self) -> Path:
        return self._root / 'trades.yaml'

    def _decks_dir(self) -> Path:
        return self._root / 'decks'

    @staticmethod
    def _read_list(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        data = yaml.safe_load(path.read_text()) or []
        if not isinstance(data, list):
            raise CollectionError(f'Expected a YAML list at {path}, got {type(data).__name__}.')
        return data

    @staticmethod
    def _row_card(row: dict[str, Any], *, path: Path) -> str:
        """Return a write-path row's ``card`` ref, or raise a clean error.

        The write verbs read raw rows (not via ``_validate_card_entry``); a
        hand-edited row missing ``card`` would otherwise raise a bare ``KeyError``
        that the CLI wrapper no longer swallows. Surface a clear `CollectionError`.
        """
        if 'card' not in row:
            raise CollectionError(f'Malformed row in {path}: missing required `card` key: {row!r}.')
        return row['card']

    @staticmethod
    def _write_yaml(path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False))

    def _hydrate(self, name: str) -> dict[str, Any]:
        """Return the resolver's enrichment for `name` as a base-`Card` field dict.

        Unresolved -> just the name (subclasses fill null enrichment defaults).
        """
        card = self._resolver.get_card(name)
        if card is None:
            return {'name': name}
        return card.model_dump()

    # --- Meta ---------------------------------------------------------------- #

    @property
    def backend_name(self) -> str:
        return 'local'

    # --- Inventory ----------------------------------------------------------- #

    _INVENTORY_KEYS: ClassVar[set[str]] = {
        'card',
        'owned',
        'foil',
        'condition',
        'sets',
        'sources',
        'airtable_record_id',
    }

    def list_inventory(self) -> list[OwnedCard]:
        out: list[OwnedCard] = []
        path = self._inventory_path()
        for i, row in enumerate(self._read_list(path)):
            row = _validate_card_entry(row, self._INVENTORY_KEYS, path=path, index=i)
            name = row['card']
            fields = self._hydrate(name)
            fields.update(
                owned=row.get('owned', 0),
                foil=row.get('foil', 0),
                condition=row.get('condition', []),
                sets=row.get('sets', []),
                sources=row.get('sources', []),
                airtable_record_id=row.get('airtable_record_id'),
            )
            out.append(OwnedCard.model_validate(fields))
        return out

    def add_card(
        self,
        ref: str,
        qty: int = 1,
        *,
        condition: list[str] | None = None,
        foil: int = 0,
        sets: list[str] | None = None,
        sources: list[str] | None = None,
    ) -> None:
        path = self._inventory_path()
        rows = self._read_list(path)
        existing = next((r for r in rows if self._row_card(r, path=path) == ref), None)
        if existing is None:
            new_row: dict[str, Any] = {'card': ref, 'owned': 0}
            rows.append(new_row)
            existing = new_row
        existing['owned'] = int(existing.get('owned', 0)) + qty
        if foil:
            existing['foil'] = existing.get('foil', 0) + foil
        if condition:
            existing['condition'] = condition
        if sets:
            existing['sets'] = sets
        if sources:
            existing['sources'] = sources
        self._write_yaml(self._inventory_path(), rows)

    def set_quantity(self, ref: str, qty: int) -> None:
        path = self._inventory_path()
        rows = self._read_list(path)
        existing = next((r for r in rows if self._row_card(r, path=path) == ref), None)
        if existing is None:
            rows.append({'card': ref, 'owned': qty})
        else:
            existing['owned'] = qty
        self._write_yaml(path, rows)

    def remove_card(self, ref: str, *, force: bool = False) -> None:
        """Delete the Inventory row for ``ref`` from the local YAML store.

        Local parity for the Airtable cascade guard: if ``ref`` is linked to one or
        more decks and ``force`` is False, raise ``CollectionError`` (enumerating the
        affected decks) before any write, so the local and Airtable ports enforce the
        same defense-in-depth. ``force=True`` (or an unlinked card) removes the row.
        """
        check_remove_allowed(self, ref, force=force)
        path = self._inventory_path()
        rows = [r for r in self._read_list(path) if self._row_card(r, path=path) != ref]
        self._write_yaml(path, rows)

    # --- Chase --------------------------------------------------------------- #

    _CHASE_KEYS: ClassVar[set[str]] = {
        'card',
        'priority',
        'for_decks',
        'status',
        'target_price',
        'airtable_record_id',
    }

    def list_chase(self) -> list[ChaseCard]:
        out: list[ChaseCard] = []
        path = self._chase_path()
        for i, row in enumerate(self._read_list(path)):
            row = _validate_card_entry(row, self._CHASE_KEYS, path=path, index=i)
            name = row['card']
            fields = self._hydrate(name)
            fields.update(
                priority=row.get('priority'),
                for_decks=row.get('for_decks', []),
                status=row.get('status'),
                target_price=row.get('target_price'),
                airtable_record_id=row.get('airtable_record_id'),
            )
            out.append(ChaseCard.model_validate(fields))
        return out

    def add_chase(
        self,
        ref: str,
        *,
        priority: int | None = None,
        for_deck: str | None = None,
        status: str | None = None,
        target_price: float | None = None,
    ) -> str | None:
        # Local YAML persists every field, so no note is ever returned; the
        # `str | None` return matches the port (an Airtable backend DOES drop
        # unpersistable fields and returns a note), keeping the contract uniform.
        rows = self._read_list(self._chase_path())
        existing = next((r for r in rows if r['card'] == ref), None)
        if existing is None:
            new_row: dict[str, Any] = {'card': ref}
            rows.append(new_row)
            existing = new_row
        if priority is not None:
            existing['priority'] = priority
        if for_deck is not None:
            for_decks: list[str] = list(existing.get('for_decks', []))
            if for_deck not in for_decks:
                for_decks.append(for_deck)
            existing['for_decks'] = for_decks
        if status is not None:
            existing['status'] = status
        if target_price is not None:
            existing['target_price'] = target_price
        self._write_yaml(self._chase_path(), rows)

    def remove_chase(self, ref: str) -> None:
        rows = [r for r in self._read_list(self._chase_path()) if r['card'] != ref]
        self._write_yaml(self._chase_path(), rows)

    # --- Trades -------------------------------------------------------------- #

    def list_trades(self) -> list[Trade]:
        # `Trade` keeps `extra='forbid'` as a contract edge-guard, but the
        # hand-editable read path is TOLERANT (matching decks/inventory/chase): a
        # harmless hand-added annotation key must not break `list_trades`, so strip
        # unknown keys before validating.
        known = set(Trade.model_fields)
        out: list[Trade] = []
        for row in self._read_list(self._trades_path()):
            fields = {k: v for k, v in row.items() if k in known} if isinstance(row, dict) else row
            out.append(Trade.model_validate(fields))
        return out

    def log_trade(self, trade: Trade) -> None:
        rows = self._read_list(self._trades_path())
        rows.append(trade.model_dump(exclude_none=True))
        self._write_yaml(self._trades_path(), rows)

    # --- Decks --------------------------------------------------------------- #

    def _deck_path(self, name: str) -> Path:
        return self._decks_dir() / f'{_slugify(name)}.yaml'

    def _deck_save_path(self, deck: Deck, *, force_fresh: bool = False) -> Path:
        """Pick the file to save ``deck`` into without CLOBBERING an unrelated deck.

        Two decks may share a name (dup names are legal under uuid identity), so
        they must not share a file. The base slug path is used only when it is free
        or already belongs to this deck's uuid; otherwise the deck is written to a
        disambiguated ``<slug>-<short-uuid>.yaml`` so it never overwrites a file
        whose in-file uuid differs. ``find_deck_path_by_uuid`` already locates the
        row regardless of filename, so the disambiguated name is transparent to
        every read.

        ``force_fresh`` (the recovery path): a fresh-identity recovery save must
        never adopt an existing file. When set, the two "adopt an existing file"
        branches are bypassed: the base slug is taken only when genuinely free —
        never because a same-named legacy (no-uuid) file sits there (a restored
        legacy backup) — and (defensively) never because a same-uuid file already
        exists (a recovery payload always carries a fresh uuid, so this cannot
        legitimately match). Any occupied base slug disambiguates to
        ``<slug>-<fresh-uuid>.yaml``. The first-save legacy-upgrade behaviour is
        unchanged for the normal (non-recovery) path.
        """
        base = self._deck_path(deck.name)
        if force_fresh:
            # RECOVERY: never adopt an existing file. Take the base slug only when it is
            # genuinely free; any occupant (legacy no-uuid backup, same-name stranger, or
            # even a same-uuid file — impossible for a fresh recovery id) disambiguates.
            if not base.exists():
                return base
            return self._fresh_disambiguated_path(deck)
        # A file already bound to THIS uuid (possibly a disambiguated one) wins — a
        # re-save must land on the same file, not spawn a new one.
        bound = self.find_deck_path_by_uuid(deck.uuid)
        if bound is not None:
            return bound
        if not base.exists():
            return base  # the base slug is FREE.
        base_uuid = self._file_uuid(base)
        if base_uuid == deck.uuid:
            return base  # the base file is already OURS.
        # A base file with no in-file uuid (a legacy file) may be upgraded in place
        # only when it is the same deck by name — a legacy file is never a free
        # upgrade target for a different deck that merely shares the slug. The
        # shrink ceremony in ``save_deck`` still guards a same-name upgrade.
        if base_uuid is None:
            try:
                stored = self._load_deck_from_dict(yaml.safe_load(base.read_text()) or {}, path=base)
            except CollectionError:
                stored = None
            if stored is not None and stored.name == deck.name:
                return base
        # Base file bound to a DIFFERENT uuid (or a legacy file for a DIFFERENT deck)
        # -> disambiguate so we never overwrite an unrelated file (the collision case).
        return self._decks_dir() / f'{_slugify(deck.name)}-{deck.uuid[:8]}.yaml'

    def _fresh_disambiguated_path(self, deck: Deck) -> Path:
        """A never-occupied ``<slug>-<uuid8>.yaml`` recovery target.

        ``force_fresh`` recovery must never adopt an existing file. The base slug is
        taken, so we disambiguate by the fresh uuid's first 8 hex — but if that file is
        also occupied (a pre-existing ``<slug>-<uuid8>.yaml`` sibling whose name matches
        the fresh mint's first 8 hex — a 2^-32 collision, but free to close), re-check the
        occupant: widen the suffix until the filename is genuinely free. The filename is
        transparent to every read (``find_deck_path_by_uuid`` locates the row by its
        in-file uuid regardless of filename), so a widened suffix is harmless.
        """
        slug = _slugify(deck.name)
        decks_dir = self._decks_dir()
        # Prefer the canonical 8-hex suffix; widen (more hex, then a counter) if occupied.
        for width in range(8, len(deck.uuid) + 1):
            candidate = decks_dir / f'{slug}-{deck.uuid[:width]}.yaml'
            if not candidate.exists():
                return candidate
        counter = 1
        while True:
            candidate = decks_dir / f'{slug}-{deck.uuid}-{counter}.yaml'
            if not candidate.exists():
                return candidate
            counter += 1

    @staticmethod
    def _file_uuid(path: Path) -> str | None:
        """Return the in-file ``uuid`` of a deck YAML (or None if absent/unparsable)."""
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            return None
        return data.get('uuid') if isinstance(data, dict) else None

    @staticmethod
    def _deck_card_to_row(card: DeckCard) -> dict[str, Any]:
        """Persist only the membership facts (card ref + role/qty), not enrichment.

        Both ``role`` and ``qty`` are emitted independently: ``role`` whenever set,
        and ``qty`` whenever ``quantity != 1`` (the non-default). A commander with
        two copies therefore round-trips BOTH facts — they are orthogonal.
        """
        row: dict[str, Any] = {'card': card.name}
        if card.role is not None:
            row['role'] = card.role
        if card.quantity != 1:
            row['qty'] = card.quantity
        return row

    _DECK_CARD_KEYS: ClassVar[set[str]] = {'card', 'qty', 'role'}

    def _load_deck_from_dict(self, data: dict[str, Any], *, path: Path) -> Deck:
        if not isinstance(data, dict) or 'name' not in data:
            raise CollectionError(
                f'Malformed deck YAML at {path}: expected a mapping with a `name` key, got {type(data).__name__}.'
            )
        cards: list[DeckCard] = []
        for i, entry in enumerate(data.get('cards', []) or []):
            entry = _validate_card_entry(entry, self._DECK_CARD_KEYS, path=path, index=i)
            name = entry['card']
            fields = self._hydrate(name)
            fields.update(quantity=entry.get('qty', 1), role=entry.get('role'))
            cards.append(DeckCard.model_validate(fields))
        # The in-file ``uuid`` is the authoritative identity: read it back into
        # ``Deck.uuid`` so the store binds by it. A legacy file missing a uuid is not
        # an error — the Deck model mints a fresh one (default_factory), which is
        # written into the file on the next save (see ``save_deck``).
        deck_kwargs: dict[str, Any] = {
            'name': data['name'],
            'strategy': data.get('strategy'),
            'assessment': data.get('assessment'),
            'focus_otags': data.get('focus_otags', []) or [],
            'format': data.get('format'),
            'cards': cards,
            'airtable_record_id': data.get('airtable_record_id'),
        }
        stored_uuid = data.get('uuid')
        if isinstance(stored_uuid, str) and stored_uuid:
            deck_kwargs['uuid'] = stored_uuid
        return Deck(**deck_kwargs)

    def get_deck(self, name: str) -> Deck:
        path = self._deck_path(name)
        if not path.exists():
            raise FileNotFoundError(f'No deck YAML at {path} for {name!r}.')
        data = yaml.safe_load(path.read_text()) or {}
        return self._load_deck_from_dict(data, path=path)

    def find_deck_path_by_uuid(self, uuid: str) -> Path | None:
        """Return the deck-file whose in-file ``uuid`` equals ``uuid`` (or None).

        Rename-tolerant binding: the filename is a human-friendly slug and is not
        authoritative, so a user renaming a deck file must not break the binding.
        Glob the (small-N) decks dir and read each file's ``uuid`` key — the
        authoritative identity — returning the matching path. A file lacking a uuid
        (legacy) simply never matches.
        """
        decks_dir = self._decks_dir()
        if not decks_dir.exists():
            return None
        matches: list[Path] = []
        for path in sorted(decks_dir.glob('*.yaml')):
            try:
                data = yaml.safe_load(path.read_text()) or {}
            except yaml.YAMLError:
                continue
            if isinstance(data, dict) and data.get('uuid') == uuid:
                matches.append(path)
        if not matches:
            return None
        if len(matches) > 1:
            # A user-duplicated deck file (same in-file uuid twice, e.g. a hand-made
            # backup) would otherwise split reads and writes across files and lose
            # edits silently. Refuse loudly, naming both offending paths.
            names = ', '.join(p.name for p in matches)
            raise CollectionError(
                f'duplicate deck file: {len(matches)} files share the in-file uuid {uuid!r} '
                f'({names}). Deck uuids must be unique — delete or re-uuid the extra copy '
                '(renaming the FILE is fine; two files carrying the same uuid is not).'
            )
        return matches[0]

    def backfill_deck_uuids(self) -> dict[str, str]:
        """Inject an in-file ``uuid`` (+ the protective header) into every legacy deck file.

        A legacy YAML deck carries no in-file uuid, so ``_deck_save_path`` treats it
        as an unclaimed upgrade target and the slug-collision guard cannot protect
        it. Walk ``collection/decks/*.yaml`` and, for each file missing a uuid, mint
        one and rewrite the file with the ``uuid`` first key + header — additive
        (card content preserved) and idempotent (a file that already carries a uuid
        is left byte-identical, never re-minted). Local-only; no Airtable, no network.

        Returns ``{name: uuid}`` for the files it touched (the newly-assigned uuids),
        so a caller can bind ``external_ids['local']`` to the file's real in-file uuid.
        """
        decks_dir = self._decks_dir()
        if not decks_dir.exists():
            return {}
        # This full-dir YAML parse runs on every store construction (twice per CLI
        # verb), so once a pass finds no legacy files, drop a marker keyed to the
        # files' state and skip the parse while that state is unchanged.
        #
        # The key is max(st_mtime_ns over *.yaml) + file count — NOT the directory
        # mtime, which does not change when a file's content is overwritten in
        # place (e.g. `cp backup.yaml decks/vault.yaml`). A restored legacy backup
        # must invalidate the marker so the backfill heals it on the next read.
        marker = decks_dir / '.uuid-backfill-clean'
        try:
            files_key = self._decks_dir_files_key(decks_dir)
            if marker.exists() and marker.read_text().strip() == files_key:
                return {}
        except OSError:
            files_key = None
        assigned: dict[str, str] = {}
        for path in sorted(decks_dir.glob('*.yaml')):
            try:
                data = yaml.safe_load(path.read_text()) or {}
            except yaml.YAMLError:
                continue  # a malformed file is left untouched (surfaces on real read).
            if not isinstance(data, dict) or data.get('uuid'):
                continue  # already carries a uuid — idempotent skip.
            new_uuid = uuid4().hex
            # ``uuid`` must be the first key so the header sits directly above it.
            # Strip a falsy in-file ``uuid`` (``uuid:`` -> None / ``uuid: ''``) from
            # ``data`` first: otherwise ``{**data}`` re-inserts the falsy value and
            # overrides the mint, so the injection silently fails, the file is
            # rewritten on every run, and a row would bind to a ghost uuid in no file.
            data = {k: v for k, v in data.items() if k != 'uuid'}
            reordered = {'uuid': new_uuid, **data}
            self._write_deck_yaml(path, reordered)
            name = data.get('name')
            if isinstance(name, str):
                assigned[name] = new_uuid
        if not assigned:
            # A clean pass: drop the marker keyed to the files' state after writing it
            # (any write bumps a file mtime, so recompute) so the next construction
            # skips. Keyed on max file mtime + count, never the dir mtime.
            import contextlib

            with contextlib.suppress(OSError):
                marker.write_text(self._decks_dir_files_key(decks_dir))
        return assigned

    @staticmethod
    def _decks_dir_files_key(decks_dir: Path) -> str:
        """Marker key that changes on an in-place overwrite: max mtime + count.

        Keyed on ``max(st_mtime_ns over *.yaml)`` and the file count — a per-file cheap
        stat pass. Unlike the dir mtime, this bumps when a file's content is overwritten
        in place, so a dropped-in legacy backup invalidates the marker and the backfill
        heals the row on the next read. The marker file itself is not a ``*.yaml`` so it
        never self-references.
        """
        mtimes = [p.stat().st_mtime_ns for p in sorted(decks_dir.glob('*.yaml'))]
        return f'{max(mtimes) if mtimes else 0}:{len(mtimes)}'

    def list_decks(self) -> list[Deck]:
        decks_dir = self._decks_dir()
        if not decks_dir.exists():
            return []
        out: list[Deck] = []
        for path in sorted(decks_dir.glob('*.yaml')):
            data = yaml.safe_load(path.read_text()) or {}
            out.append(self._load_deck_from_dict(data, path=path))
        return out

    def save_deck(self, deck: Deck, *, allow_shrink: bool = False, force_fresh: bool = False) -> None:
        # Bind to this deck's file (by uuid, then a free/own slug) so a dup-named
        # deck can never overwrite an unrelated file (the slug-collision guard).
        # ``force_fresh`` (recovery): never adopt an existing same-named file; an
        # occupied base slug always disambiguates to a fresh ``<slug>-<uuid>.yaml``.
        path = self._deck_save_path(deck, force_fresh=force_fresh)
        if not allow_shrink and path.exists():
            # Defensive shrink guard: refuse a save that drops an at-target deck
            # under target unless explicitly allowed. Compare against the file's
            # current contents regardless of uuid match — a legacy (no in-file uuid)
            # file must not be a free upgrade target that bypasses the ceremony
            # (``_deck_save_path`` already routes a different deck to a disambiguated
            # path, so a compared shrink here is a genuine same-file gutting write).
            prior = self._load_deck_from_dict(yaml.safe_load(path.read_text()) or {}, path=path)
            if shrink_check(prior, deck):
                raise CollectionError(shrink_refusal_message(prior, deck))
        # ``uuid`` is the first key so the protective comment header injected by
        # ``_write_deck_yaml`` sits directly above it.
        data: dict[str, Any] = {
            'uuid': deck.uuid,
            'name': deck.name,
            'strategy': deck.strategy,
        }
        # Optional skill-authored fields: only persist them when set, so a deck
        # that declares neither keeps the YAML clean (and hand-editing stays easy).
        if deck.assessment is not None:
            data['assessment'] = deck.assessment
        if deck.focus_otags:
            data['focus_otags'] = list(deck.focus_otags)
        if deck.format is not None:
            data['format'] = deck.format
        data['airtable_record_id'] = deck.airtable_record_id
        data['cards'] = [self._deck_card_to_row(c) for c in deck.cards]
        self._write_deck_yaml(path, data)

    def get_deck_by_uuid(self, uuid: str) -> Deck:
        """Read the deck whose in-file ``uuid`` equals ``uuid`` (rename/slug-proof).

        Locate the deck file by its authoritative in-file uuid regardless of
        filename, then hydrate it. Used by ``promote`` / the sync layer to re-read a
        just-written source deck by the one identity that can't collide with a
        same-named sibling. Raises ``FileNotFoundError`` on miss.
        """
        path = self.find_deck_path_by_uuid(uuid)
        if path is None:
            raise FileNotFoundError(f'No deck YAML with uuid {uuid!r}.')
        data = yaml.safe_load(path.read_text()) or {}
        return self._load_deck_from_dict(data, path=path)

    def _write_deck_yaml(self, path: Path, data: dict[str, Any]) -> None:
        """Write a deck YAML with the protective ``uuid`` comment header on top.

        ``safe_dump`` cannot emit comments, so the header is prepended manually.
        The ``uuid`` MUST be the first key of ``data`` (``sort_keys=False`` keeps
        insertion order) so the header lands directly above it.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
        # Atomic write: write a sibling tmp file then ``os.replace`` it into place,
        # so a crash mid-write (e.g. the backfill mass-rewriting legacy files) can
        # never truncate/destroy an intact deck file — the old file survives until
        # the new one is complete. ``os.replace`` is atomic within a filesystem.
        tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        try:
            tmp.write_text(_UUID_COMMENT + body)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def _set_deck_field(self, name: str, key: str, value: object) -> None:
        """Set one top-level field on an existing deck YAML in place (cards preserved)."""
        path = self._deck_path(name)
        if not path.exists():
            raise FileNotFoundError(f'No deck YAML at {path} for {name!r}.')
        data = yaml.safe_load(path.read_text()) or {}
        data[key] = value
        # Preserve (or add) the in-file uuid + its protective header. A legacy file
        # without a uuid gets one now (via a fresh mint) so the identity is stable.
        if not data.get('uuid'):
            # Strip a falsy ``uuid`` first so it can't override the mint.
            data = {'uuid': uuid4().hex, **{k: v for k, v in data.items() if k != 'uuid'}}
        self._write_deck_yaml(path, data)

    def set_strategy(self, name: str, text: str) -> None:
        self._set_deck_field(name, 'strategy', text)

    def set_assessment(self, name: str, text: str) -> None:
        self._set_deck_field(name, 'assessment', text)

    def set_focus_otags(self, name: str, otags: list[str]) -> None:
        self._set_deck_field(name, 'focus_otags', list(otags))
