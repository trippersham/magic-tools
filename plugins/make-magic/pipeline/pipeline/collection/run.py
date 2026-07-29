"""Collection dispatcher: ``python -m pipeline.collection.run <verb> [args...]``.

Mirrors ``sources/run.py`` (delta D2): a plain ``sys.argv`` dispatcher routing to
a per-verb ``argparse`` handler — no Typer, no new dep. This is the SINGLE,
backend-agnostic data surface the three skills (building-decks, chasing-cards,
managing-inventory) bind to in BOTH modes; the active backend is resolved by
``get_store`` (local YAML or Airtable records), so a verb's behavior is identical
regardless of source of record.

Verbs print STABLE, parseable output — JSON where a skill consumes structured
data (``get-deck``, ``list-*``, ``factsheet``, ``status``), and a short
confirmation line for writes.

Hydration: the local adapter fills base-``Card`` enrichment via the package
default ``CardResolver`` (``pipeline.collection.resolver``), which ``get_store``
supplies — so this dispatcher injects nothing (no ``scripts/`` edge). The Airtable
adapter hydrates directly from the row and ignores the resolver (documented
asymmetry).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from pipeline.collection import (
    CollectionError,
    copy_collection,
    get_store,
    onboard,
    onboarding_status,
)
from pipeline.config import AirtableConfigError
from pipeline.contracts import Deck, Trade

if TYPE_CHECKING:
    from pipeline.collection import CollectionStore

#: The repo's ``scripts/`` dir (sibling of the pipeline package root). Only the
#: ``factsheet`` verb reaches it (bridging to ``scripts/deck_factsheet.py``); card
#: hydration no longer uses a script-edge resolver (it's package-native now).
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / 'scripts'


def _store(*, writes_enabled: bool = False) -> CollectionStore:
    """Resolve the active backend and construct its store.

    ``writes_enabled`` opts the Airtable adapter into mutations (ignored by the
    local adapter, which always writes to YAML). The local adapter's resolver is
    supplied by ``get_store`` (the package default) — nothing is injected here.
    """
    return get_store(writes_enabled=writes_enabled)


def _dump(models: BaseModel | Sequence[BaseModel]) -> str:
    """JSON for a list of pydantic models (or one model)."""
    if isinstance(models, BaseModel):
        return models.model_dump_json(indent=2)
    return json.dumps([m.model_dump(mode='json') for m in models], indent=2)


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #


def _status(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection status')
    parser.parse_args(argv)
    status = onboarding_status()
    backend = status.effective_backend
    label = 'local (collection/ YAML)' if backend == 'local' else 'airtable (records adapter)'
    out: dict[str, object] = {
        'backend': backend,
        'source_of_record': label,
        'onboarded': status.onboarded,
        'needs_onboarding': status.needs_onboarding,
    }
    if status.needs_onboarding:
        # Actionable nag — nothing hard-blocks (reads default to local), but tell
        # the user how to pin their backend so subsequent runs never re-prompt.
        out['message'] = (
            'No backend configured — defaulting reads to local. Run '
            '`collection onboard --backend local|airtable` to pin your source of record.'
        )
    print(json.dumps(out, indent=2))


def _onboard(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection onboard')
    parser.add_argument('--backend', required=True, choices=('local', 'airtable'))
    args = parser.parse_args(argv)
    onboard(args.backend)
    print(f'onboard: backend set to {args.backend} (persisted; will not re-prompt).')


# --------------------------------------------------------------------------- #
# Decks
# --------------------------------------------------------------------------- #


def _list_decks(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection list-decks')
    parser.parse_args(argv)
    for deck in _store().list_decks():
        print(deck.name)


def _get_deck(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection get-deck')
    parser.add_argument('name')
    parser.add_argument('--field', help='Print only this Deck field (e.g. strategy, assessment, focus_otags).')
    args = parser.parse_args(argv)
    deck = _store().get_deck(args.name)
    if args.field:
        allowed = sorted(set(Deck.model_fields) | {'commanders'})
        if args.field not in allowed:
            raise CollectionError(f'unknown deck field {args.field!r}; choose from {allowed}')
        value = getattr(deck, args.field)
        if isinstance(value, list):
            # A list field may hold scalars (``focus_otags`` -> list[str]) or
            # pydantic models (``commanders`` -> list[DeckCard]); serialize models
            # via pydantic so ``--field commanders`` doesn't blow up on json.dumps.
            print(json.dumps([v.model_dump(mode='json') if isinstance(v, BaseModel) else v for v in value], indent=2))
        elif isinstance(value, BaseModel):
            print(value.model_dump_json(indent=2))
        else:
            print('' if value is None else value)
    else:
        print(deck.model_dump_json(indent=2))


def _save_deck(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection save-deck')
    parser.add_argument('--from-json', required=True, help='Path to a JSON Deck (- for stdin).')
    args = parser.parse_args(argv)
    raw = sys.stdin.read() if args.from_json == '-' else Path(args.from_json).read_text()
    try:
        deck = Deck.model_validate_json(raw)
    except ValidationError as exc:
        # Bad USER-supplied JSON is clean input error, not a defect — surface it
        # as a one-line `error:` rather than a raw ValidationError traceback.
        raise CollectionError(f'invalid deck JSON: {exc}') from exc
    _store(writes_enabled=True).save_deck(deck)
    print(f'save-deck: {deck.name}')


def _set_strategy(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection set-strategy')
    parser.add_argument('name')
    parser.add_argument('text')
    args = parser.parse_args(argv)
    _store(writes_enabled=True).set_strategy(args.name, args.text)
    print(f'set-strategy: {args.name}')


def _set_assessment(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection set-assessment')
    parser.add_argument('name')
    parser.add_argument('text')
    args = parser.parse_args(argv)
    _store(writes_enabled=True).set_assessment(args.name, args.text)
    print(f'set-assessment: {args.name}')


def _set_focus_otags(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection set-focus-otags')
    parser.add_argument('name')
    parser.add_argument('otag', nargs='+', help='One or more focus otag/bucket slugs.')
    args = parser.parse_args(argv)
    _store(writes_enabled=True).set_focus_otags(args.name, list(args.otag))
    print(f'set-focus-otags: {args.name} -> {args.otag}')


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #


def _list_inventory(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection list-inventory')
    parser.parse_args(argv)
    print(_dump(_store().list_inventory()))


def _add_card(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection add-card')
    parser.add_argument('ref')
    parser.add_argument('--qty', type=int, default=1, help='copies to add (default 1)')
    parser.add_argument('--condition', action='append', default=None, help='condition, e.g. Near Mint (repeatable)')
    parser.add_argument('--foil', type=int, default=0, help='FOIL copies as an int COUNT, e.g. --foil 1 (default 0)')
    parser.add_argument('--set', dest='sets', action='append', default=None, help='set name (repeatable)')
    parser.add_argument('--source', dest='sources', action='append', default=None, help='source (repeatable)')
    args = parser.parse_args(argv)
    _store(writes_enabled=True).add_card(
        args.ref, args.qty, condition=args.condition, foil=args.foil, sets=args.sets, sources=args.sources
    )
    print(f'add-card: {args.ref} x{args.qty}')


def _set_quantity(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection set-quantity')
    parser.add_argument('ref')
    parser.add_argument('qty', type=int)
    args = parser.parse_args(argv)
    _store(writes_enabled=True).set_quantity(args.ref, args.qty)
    print(f'set-quantity: {args.ref} = {args.qty}')


def _remove_card(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection remove-card')
    parser.add_argument('ref')
    args = parser.parse_args(argv)
    _store(writes_enabled=True).remove_card(args.ref)
    print(f'remove-card: {args.ref}')


# --------------------------------------------------------------------------- #
# Chase
# --------------------------------------------------------------------------- #


def _list_chase(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection list-chase')
    parser.parse_args(argv)
    print(_dump(_store().list_chase()))


def _add_chase(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection add-chase')
    parser.add_argument('ref')
    parser.add_argument('--priority', type=int, default=None)
    parser.add_argument('--for-deck', dest='for_deck', default=None)
    parser.add_argument('--status', default=None)
    parser.add_argument('--target-price', dest='target_price', type=float, default=None)
    args = parser.parse_args(argv)
    note = _store(writes_enabled=True).add_chase(
        args.ref,
        priority=args.priority,
        for_deck=args.for_deck,
        status=args.status,
        target_price=args.target_price,
    )
    # Surface any adapter note (e.g. Airtable can't persist priority/status/
    # target-price — no such columns) so the drop isn't silent at the CLI.
    print(f'add-chase: {args.ref}' + (f' ({note})' if note else ''))


def _remove_chase(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection remove-chase')
    parser.add_argument('ref')
    args = parser.parse_args(argv)
    _store(writes_enabled=True).remove_chase(args.ref)
    print(f'remove-chase: {args.ref}')


# --------------------------------------------------------------------------- #
# Trades
# --------------------------------------------------------------------------- #


def _list_trades(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection list-trades')
    parser.parse_args(argv)
    print(_dump(_store().list_trades()))


def _log_trade(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection log-trade')
    parser.add_argument('--from-json', help='Path to a JSON Trade (- for stdin).')
    parser.add_argument('--from-source', dest='from_source')
    parser.add_argument('--to-destination', dest='to_destination')
    parser.add_argument('--from-deck', dest='from_deck', default=None, help="From (Deck) when Source == 'Deck'.")
    parser.add_argument('--to-deck', dest='to_deck', default=None, help="To (Deck) when Destination == 'Deck'.")
    parser.add_argument('--status', default=None)
    parser.add_argument('--notes', default=None)
    parser.add_argument('--card-in', dest='cards_in', action='append', default=None)
    parser.add_argument('--card-out', dest='cards_out', action='append', default=None)
    args = parser.parse_args(argv)
    if args.from_json:
        raw = sys.stdin.read() if args.from_json == '-' else Path(args.from_json).read_text()
        trade = Trade.model_validate_json(raw)
    else:
        if not args.from_source or not args.to_destination:
            parser.error('either --from-json or both --from-source and --to-destination are required')
        trade = Trade(
            from_source=args.from_source,
            to_destination=args.to_destination,
            from_deck=args.from_deck,
            to_deck=args.to_deck,
            status=args.status,
            notes=args.notes,
            cards_in=args.cards_in or [],
            cards_out=args.cards_out or [],
        )
    _store(writes_enabled=True).log_trade(trade)
    print('log-trade: recorded')


# --------------------------------------------------------------------------- #
# Factsheet (offline behavioral verb — Phase 1.4)
# --------------------------------------------------------------------------- #


def _build_store(
    backend: str, *, writes_enabled: bool = False, data_dir: str | None = None
) -> CollectionStore:
    """Construct a store for an EXPLICIT backend (bypassing `resolve_backend`).

    Used by ``copy`` to build the source + destination independently. A local
    store may be pointed at an explicit ``data_dir`` (its ``collection/`` child)
    so a local->local copy can target a distinct dir within one process.
    """
    if backend == 'airtable':
        from pipeline.collection.adapters.airtable_collection import AirtableCollectionStore

        token = os.environ.get('AIRTABLE_API_KEY')
        if not token:
            raise CollectionError(
                'Backend is `airtable` but AIRTABLE_API_KEY is not set. Export an Airtable '
                'Personal Access Token, or use a local backend for offline mode.'
            )
        return AirtableCollectionStore.from_settings(token, writes_enabled=writes_enabled)

    from pipeline.collection import resolver as resolver_mod
    from pipeline.collection.adapters.local_yaml import LocalYamlStore

    root = Path(data_dir).resolve() / 'collection' if data_dir else None
    return LocalYamlStore(resolver=resolver_mod.default_card_resolver(), collection_root=root)


def _copy(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection copy')
    parser.add_argument('--from', dest='src', required=True, choices=('local', 'airtable'))
    parser.add_argument('--to', dest='dst', required=True, choices=('local', 'airtable'))
    parser.add_argument(
        '--confirm',
        action='store_true',
        help='Required for any `--to airtable` — guards against an accidental live production write.',
    )
    parser.add_argument(
        '--dest-data-dir',
        dest='dest_data_dir',
        default=None,
        help='For `--to local`: write the destination collection under this data dir.',
    )
    args = parser.parse_args(argv)

    if args.dst == 'airtable' and not args.confirm:
        parser.error(
            '`--to airtable` writes to a LIVE base — pass --confirm to proceed. '
            '(This guards against an accidental production write.)'
        )

    source = _build_store(args.src, writes_enabled=False)
    dest = _build_store(args.dst, writes_enabled=True, data_dir=args.dest_data_dir)
    report = copy_collection(source, dest)
    print(report.model_dump_json(indent=2))


def _factsheet(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection factsheet')
    parser.add_argument('name')
    parser.add_argument('--focus', default=None, help='Comma-separated focus set.')
    args = parser.parse_args(argv)
    deck = _store().get_deck(args.name)

    root = str(_SCRIPTS_DIR)
    if root not in sys.path:
        sys.path.insert(0, root)
    from deck_factsheet import factsheet_from_deck  # pyright: ignore[reportMissingImports]

    focus = [f.strip() for f in args.focus.split(',')] if args.focus else None
    report = factsheet_from_deck(deck, focus=focus)
    print(json.dumps(report, indent=2))


VERBS = {
    'status': _status,
    'onboard': _onboard,
    'copy': _copy,
    # decks
    'list-decks': _list_decks,
    'get-deck': _get_deck,
    'save-deck': _save_deck,
    'set-strategy': _set_strategy,
    'set-assessment': _set_assessment,
    'set-focus-otags': _set_focus_otags,
    # inventory
    'list-inventory': _list_inventory,
    'add-card': _add_card,
    'set-quantity': _set_quantity,
    'remove-card': _remove_card,
    # chase
    'list-chase': _list_chase,
    'add-chase': _add_chase,
    'remove-chase': _remove_chase,
    # trades
    'list-trades': _list_trades,
    'log-trade': _log_trade,
    # behavioral
    'factsheet': _factsheet,
}

__all__ = ('main',)


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in VERBS:
        avail = ', '.join(sorted(VERBS))
        print(
            f'usage: python -m pipeline.collection.run <verb> [args...]\n  verbs: {avail}',
            file=sys.stderr,
        )
        raise SystemExit(2)
    verb = sys.argv[1]
    # `ReadOnlyStoreError` lives in the lazily-imported Airtable adapter; import it
    # here so the local-only path never triggers the adapter import.
    from pipeline.collection.adapters.airtable_collection import ReadOnlyStoreError

    try:
        VERBS[verb](sys.argv[2:])
    except (FileNotFoundError, CollectionError, ReadOnlyStoreError, AirtableConfigError) as exc:
        # EXPECTED, user-facing failures only (unknown deck, bad --field, malformed
        # YAML, bad user JSON, missing creds, Airtable schema/read-only errors):
        # surface a clean one-line message instead of a raw traceback. A genuine
        # bug (KeyError / RuntimeError / ValidationError / AttributeError) is NOT
        # caught here — it tracebacks so the defect stays visible. SystemExit
        # (argparse, guards) passes through untouched.
        print(f'error: {exc}', file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == '__main__':
    main()
