"""Local YAML `CollectionStore` adapter — hand-editable YAML as source of record.

Implements the `CollectionStore` port over YAML files under the `collection/`
dir (resolved off `StorePaths`). Thin persistence: only the non-derivable facts
(ownership / intent / membership + card ref) are written; the base-`Card`
enrichment is HYDRATED on read via an injected `CardResolver` (unresolved cards
read back name-only with null enrichment).

Layout (design "Local data layout"):

    collection/
      decks/<slug>.yaml   # self-contained: name, strategy, airtable_record_id, cards[]
      inventory.yaml      # owned-facts only:  [{card, owned, foil, condition, sets, sources}]
      chase.yaml          # intent-facts only: [{card, priority, for_decks, status, target_price}]
      trades.yaml         # movement events

This module MUST NOT import scripts/ — the concrete Scryfall-cache resolver is
injected at the edge; here we only depend on the `CardResolver` Protocol.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import yaml

from pipeline import store as _store
from pipeline.collection.errors import CollectionError
from pipeline.collection.guards import check_remove_allowed, shrink_check
from pipeline.contracts import ChaseCard, Deck, DeckCard, OwnedCard, Trade

if TYPE_CHECKING:
    from pipeline.collection.store import CardResolver

_SLUG_STRIP = re.compile(r'[^a-z0-9]+')


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

        Local parity for the Airtable cascade guard (Phase 4): if ``ref`` is
        LINKED to one or more decks and ``force`` is False, raise
        ``CollectionError`` (enumerating the affected decks) BEFORE any write, so
        the local and Airtable ports enforce the same defense-in-depth.
        ``force=True`` (or an unlinked card) removes the row as before.
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
        return Deck(
            name=data['name'],
            strategy=data.get('strategy'),
            assessment=data.get('assessment'),
            focus_otags=data.get('focus_otags', []) or [],
            format=data.get('format'),
            cards=cards,
            airtable_record_id=data.get('airtable_record_id'),
        )

    def get_deck(self, name: str) -> Deck:
        path = self._deck_path(name)
        if not path.exists():
            raise FileNotFoundError(f'No deck YAML at {path} for {name!r}.')
        data = yaml.safe_load(path.read_text()) or {}
        return self._load_deck_from_dict(data, path=path)

    def list_decks(self) -> list[Deck]:
        decks_dir = self._decks_dir()
        if not decks_dir.exists():
            return []
        out: list[Deck] = []
        for path in sorted(decks_dir.glob('*.yaml')):
            data = yaml.safe_load(path.read_text()) or {}
            out.append(self._load_deck_from_dict(data, path=path))
        return out

    def save_deck(self, deck: Deck, *, allow_shrink: bool = False) -> None:
        path = self._deck_path(deck.name)
        if not allow_shrink and path.exists():
            # Defensive shrink guard (Phase 4): refuse a save that drops an
            # at-target deck under target unless explicitly allowed. Read the
            # prior deck for its size/target only.
            prior = self._load_deck_from_dict(yaml.safe_load(path.read_text()) or {}, path=path)
            if shrink_check(prior, deck):
                raise CollectionError(
                    f"save-deck '{deck.name}': this save shrinks the deck from "
                    f'{sum(c.quantity for c in prior.cards)} to {sum(c.quantity for c in deck.cards)} '
                    f'cards, below its target of {prior.target_size}. Pass allow_shrink=True '
                    '(the CLI: `save-deck --confirm`) to proceed.'
                )
        data: dict[str, Any] = {
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
        self._write_yaml(self._deck_path(deck.name), data)

    def _set_deck_field(self, name: str, key: str, value: object) -> None:
        """Set one top-level field on an existing deck YAML in place (cards preserved)."""
        path = self._deck_path(name)
        if not path.exists():
            raise FileNotFoundError(f'No deck YAML at {path} for {name!r}.')
        data = yaml.safe_load(path.read_text()) or {}
        data[key] = value
        self._write_yaml(path, data)

    def set_strategy(self, name: str, text: str) -> None:
        self._set_deck_field(name, 'strategy', text)

    def set_assessment(self, name: str, text: str) -> None:
        self._set_deck_field(name, 'assessment', text)

    def set_focus_otags(self, name: str, otags: list[str]) -> None:
        self._set_deck_field(name, 'focus_otags', list(otags))
