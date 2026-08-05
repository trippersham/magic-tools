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
import logging
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
    last_known_good_deck,
    last_known_inventory_row,
    onboard,
    onboarding_status,
    record_snapshot,
    remove_impact,
    shrink_check,
)
from pipeline.config import AirtableConfigError
from pipeline.contracts import Deck, DeckCard, Trade

if TYPE_CHECKING:
    from pipeline.collection import CollectionStore
    from pipeline.decks import DecksStore
    from pipeline.decks.access import DeckAccess

#: The repo's ``scripts/`` dir (sibling of the pipeline package root). Only the
#: ``factsheet`` verb reaches it (bridging to ``scripts/deck_factsheet.py``); card
#: hydration no longer uses a script-edge resolver (it's package-native now).
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / 'scripts'

log = logging.getLogger('make_magic.collection.run')


def _store(*, writes_enabled: bool = False) -> CollectionStore:
    """Resolve the active backend and construct its store.

    ``writes_enabled`` opts the Airtable adapter into mutations (ignored by the
    local adapter, which always writes to YAML). The local adapter's resolver is
    supplied by ``get_store`` (the package default) — nothing is injected here.

    This is the DIRECT source-of-record store — inventory / chase / trades bind to
    it unchanged (W1: only the DECK verbs route through the local decks store via
    :func:`_deck_access`).
    """
    store = get_store(writes_enabled=writes_enabled)
    _backfill_local_deck_uuids(store)
    return store


def _backfill_local_deck_uuids(store: CollectionStore) -> None:
    """Run the §7 legacy-YAML uuid backfill once for the local backend (F1/F12).

    Additive + idempotent: injects an in-file ``uuid`` into every pre-P6 deck file
    lacking one so it stops being an unclaimed clobber target, then binds each
    touched deck's local row ``external_ids['local']`` to the file's real in-file
    uuid. LOCAL-only; a no-op for the Airtable backend. Guarded so a store built
    for a store with no decks dir (or a driver without the hook) is a cheap no-op.
    """
    backfill = getattr(store, 'backfill_deck_uuids', None)
    if backfill is None:
        return
    try:
        assigned = backfill()
    except Exception:  # a backfill hiccup must never break an unrelated verb.
        return
    if not assigned:
        return
    from pipeline.decks import DecksStore

    decks = DecksStore()
    for name, file_uuid in assigned.items():
        bound = decks.uuid_for_name(name)
        if bound is not None:
            decks.set_external_id(bound, 'local', file_uuid)


def _deck_access(*, writes_enabled: bool = False) -> DeckAccess:
    """The DECK access path — deck reads/writes route through the local decks store.

    W1/W4: only DECK verbs (``get-deck`` / ``save-deck`` / ``set-*`` / ``pull`` /
    ``push`` / ``sync``) go through here; a synced deck is pulled-current per the
    W4 TTL policy and served locally, edits stage locally, and a push through the
    source ceremony happens at the commit boundary. Inventory / chase / trades do
    NOT use this — they call :func:`_store` directly.
    """
    from pipeline.decks.access import deck_access

    store = get_store(writes_enabled=writes_enabled)
    _backfill_local_deck_uuids(store)
    return deck_access(store)


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


def _provenance_states(decks_store: DecksStore, deck_uuid: str | None) -> dict[str, str]:
    """The per-row provenance tri-state (``fresh``/``stale``/``absent``) for a deck (M7).

    Derived against the deck's CURRENT ``version()`` from the stored stamps. A deck
    with no local row yet (``deck_uuid`` is None — a synced source deck never pulled
    locally) has no stamps, so both states are ``absent``.
    """
    if deck_uuid is None:
        return {'assessment': 'absent', 'sim': 'absent'}
    return {
        'assessment': decks_store.assessment_state(deck_uuid),
        'sim': decks_store.sim_state(deck_uuid),
    }


def _provenance_block(decks_store: DecksStore, deck_uuid: str | None) -> dict[str, object]:
    """The full ``get-deck --provenance`` block for a deck (design §6 / M7).

    Emits ``{assessment: {version, at, state}, last_sim: {result, deck_version, at,
    state}}`` — the stored stamps augmented with the derived tri-state. Missing
    stamps surface as ``state: 'absent'`` with null fields (never validated). A deck
    with no local row yet is uniformly ``absent``.
    """
    assessment: dict[str, object] = {'version': None, 'at': None, 'state': 'absent'}
    last_sim: dict[str, object] = {'result': None, 'deck_version': None, 'at': None, 'state': 'absent'}
    if deck_uuid is None:
        return {'assessment': assessment, 'last_sim': last_sim}

    row = decks_store.get_row(deck_uuid)
    if row is not None and row.freshness:
        stamp = json.loads(row.freshness).get('assessment')
        if isinstance(stamp, dict):
            assessment = {'version': stamp.get('version'), 'at': stamp.get('at'),
                          'state': decks_store.assessment_state(deck_uuid)}
    if row is not None and row.last_sim:
        stamp = json.loads(row.last_sim)
        if isinstance(stamp, dict):
            last_sim = {'result': stamp.get('result'), 'deck_version': stamp.get('deck_version'),
                        'at': stamp.get('at'), 'state': decks_store.sim_state(deck_uuid)}
    return {'assessment': assessment, 'last_sim': last_sim}


def _list_decks(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='collection list-decks',
        description='List decks: source-backed decks [synced] + local ephemeral drafts [ephemeral].',
    )
    parser.add_argument('--json', action='store_true', help='Emit a JSON array of {name, status} rows.')
    parser.add_argument(
        '--archived', action='store_true', help='Also include archived ephemeral drafts (marked archived).'
    )
    args = parser.parse_args(argv)

    from pipeline.decks import DecksStore

    # The UNION (design §8): source-backed decks are `synced`; purely-local drafts
    # (no source) are `ephemeral`. Ephemeral drafts have no source, so the two sets
    # never overlap — a synced deck's local row is enumerated only via the source.
    decks_store = DecksStore()
    rows: list[dict[str, object]] = []
    for deck in sorted(_store().list_decks(), key=lambda d: d.name):
        rows.append({'name': deck.name, 'status': 'synced', 'archived': False,
                     **_provenance_states(decks_store, decks_store.uuid_for_name(deck.name))})
    drafts = decks_store.list_rows(sync_status='ephemeral', include_archived=args.archived)
    for row in sorted(drafts, key=lambda r: r.name):
        rows.append({'name': row.name, 'status': 'ephemeral', 'archived': row.archived,
                     **_provenance_states(decks_store, row.deck_uuid)})

    if args.json:
        print(json.dumps(rows))
        return
    for row in rows:
        markers = [str(row['status'])]
        if row['archived']:
            markers.append('archived')
        print(f'{row["name"]} [{",".join(markers)}]')


def _archive_deck(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='collection archive-deck',
        description='Archive a local deck by NAME (hide it from the default list; not deleted).',
    )
    parser.add_argument('deck', nargs='?', help='Deck name (resolved to its local deck_uuid).')
    parser.add_argument('--id', dest='id_prefix', default=None, help='Address by deck_uuid prefix (overrides name).')
    args = parser.parse_args(argv)
    from pipeline.decks import DecksStore

    deck_uuid = _resolve_existing_local(_deck_access(), args.deck, args.id_prefix)
    DecksStore().archive(deck_uuid)
    print(f'archive-deck: {args.deck if args.id_prefix is None else args.id_prefix}')


def _unarchive_deck(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='collection unarchive-deck',
        description='Unarchive a local deck by NAME (restore it to the default list).',
    )
    parser.add_argument('deck', nargs='?', help='Deck name (resolved to its local deck_uuid).')
    parser.add_argument('--id', dest='id_prefix', default=None, help='Address by deck_uuid prefix (overrides name).')
    args = parser.parse_args(argv)
    from pipeline.decks import DecksStore

    deck_uuid = _resolve_existing_local(_deck_access(), args.deck, args.id_prefix)
    DecksStore().unarchive(deck_uuid)
    print(f'unarchive-deck: {args.deck if args.id_prefix is None else args.id_prefix}')


def _get_deck(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection get-deck')
    parser.add_argument('name', nargs='?')
    parser.add_argument('--id', dest='id_prefix', default=None, help='Address by deck_uuid prefix (overrides name).')
    parser.add_argument('--field', help='Print only this Deck field (e.g. strategy, assessment, focus_otags).')
    parser.add_argument(
        '--provenance',
        action='store_true',
        help='Emit a {deck, provenance} envelope with the assessment/sim freshness stamps + tri-state.',
    )
    args = parser.parse_args(argv)
    if args.id_prefix is None and not args.name:
        raise CollectionError('get-deck: a deck name or --id prefix is required')
    # W1/W4: route the deck READ through the local decks store (pull-current per
    # the TTL policy, serve local thereafter). --id takes precedence over the name.
    access = _deck_access()
    deck = access.read_deck(args.name or '', id_prefix=args.id_prefix)
    if args.provenance:
        # The M7 provenance view: a {deck, provenance} envelope. DEFAULT output
        # (no flag) stays byte-for-byte the deck JSON — existing parsers unaffected.
        from pipeline.decks import DecksStore

        decks_store = DecksStore()
        deck_uuid = access.resolve(id_prefix=args.id_prefix) if args.id_prefix else access.resolve(args.name or '')
        envelope = {
            'deck': deck.model_dump(mode='json'),
            'provenance': _provenance_block(decks_store, deck_uuid),
        }
        print(json.dumps(envelope, indent=2))
        return
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
    parser.add_argument(
        '--confirm',
        action='store_true',
        help='Required when the save SHRINKS an at-target deck below its target (guards against '
        'silently dropping a legal deck under size).',
    )
    args = parser.parse_args(argv)
    raw = sys.stdin.read() if args.from_json == '-' else Path(args.from_json).read_text()
    try:
        deck = Deck.model_validate_json(raw)
    except ValidationError as exc:
        # Bad USER-supplied JSON is clean input error, not a defect — surface it
        # as a one-line `error:` rather than a raw ValidationError traceback.
        raise CollectionError(f'invalid deck JSON: {exc}') from exc
    access = _deck_access(writes_enabled=True)
    store = _store(writes_enabled=True)
    # Shrink guard: read the PRIOR deck (if any) and require --confirm when this
    # save would drop an at-target deck below target. Building/creating a deck
    # (no prior, or prior never met target) never trips this. Read the prior from
    # the SOURCE directly (the pre-save baseline), independent of the local copy.
    try:
        prior = store.get_deck(deck.name)
    except FileNotFoundError:
        prior = None
    if prior is not None and shrink_check(prior, deck) and not args.confirm:
        parser.error(
            f"save-deck '{deck.name}' would shrink the deck from {sum(c.quantity for c in prior.cards)} "
            f'to {sum(c.quantity for c in deck.cards)} cards, below its target of {prior.target_size}. '
            'Pass --confirm to proceed. (This guards against silently dropping a legal deck under size.)'
        )
    # W1/W4: stage locally, then commit (push) through the source ceremony.
    access.save_deck(deck, allow_shrink=args.confirm, commit=True)
    print(f'save-deck: {deck.name}')


def _set_strategy(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection set-strategy')
    parser.add_argument('name', nargs='?')
    parser.add_argument('text')
    parser.add_argument('--id', dest='id_prefix', default=None, help='Address by deck_uuid prefix (overrides name).')
    args = parser.parse_args(argv)
    _deck_access(writes_enabled=True).set_strategy(args.name or '', args.text, id_prefix=args.id_prefix)
    print(f'set-strategy: {args.name if args.id_prefix is None else args.id_prefix}')


def _set_assessment(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection set-assessment')
    parser.add_argument('name', nargs='?')
    parser.add_argument('text')
    parser.add_argument('--id', dest='id_prefix', default=None, help='Address by deck_uuid prefix (overrides name).')
    args = parser.parse_args(argv)
    _deck_access(writes_enabled=True).set_assessment(args.name or '', args.text, id_prefix=args.id_prefix)
    print(f'set-assessment: {args.name if args.id_prefix is None else args.id_prefix}')


def _set_focus_otags(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection set-focus-otags')
    parser.add_argument('name', nargs='?')
    parser.add_argument('otag', nargs='+', help='One or more focus otag/bucket slugs.')
    parser.add_argument('--id', dest='id_prefix', default=None, help='Address by deck_uuid prefix (overrides name).')
    args = parser.parse_args(argv)
    _deck_access(writes_enabled=True).set_focus_otags(args.name or '', list(args.otag), id_prefix=args.id_prefix)
    print(f'set-focus-otags: {args.name if args.id_prefix is None else args.id_prefix} -> {args.otag}')


def _stamp_sim(argv: list[str]) -> None:
    """The thin VALIDATE hook (M7 / preflight W-E): stamp ``last_sim`` on a deck row.

    ``collection stamp-sim "<deck>" --result '<json>'`` (or ``--id <prefix>``) writes
    ``last_sim = {result, deck_version, at}`` keyed on the deck's current ``version()``
    so derived-phase staleness becomes REAL across sessions. The ``pipeline/sim/``
    subsystem is UNTOUCHED — the simulating-games step calls this verb with its run
    summary. ``--result`` accepts an arbitrary JSON blob (win-rate, games, ...) and
    is stored VERBATIM; a non-JSON value is stored as the raw string.
    """
    parser = argparse.ArgumentParser(prog='collection stamp-sim')
    parser.add_argument('name', nargs='?')
    parser.add_argument('--id', dest='id_prefix', default=None, help='Address by deck_uuid prefix (overrides name).')
    parser.add_argument('--result', required=True, help='The sim summary — arbitrary JSON (or a raw string).')
    args = parser.parse_args(argv)
    from pipeline.decks import DecksStore

    try:
        result: object = json.loads(args.result)
    except json.JSONDecodeError:
        result = args.result  # a non-JSON summary line — stored verbatim.
    access = _deck_access()
    # Ensure a local row exists (pull-current a synced source deck) so there is a
    # row to stamp, then resolve to its uuid.
    deck_uuid, _ = _resolve_edit_target(access, args.name, args.id_prefix)
    DecksStore().set_last_sim(deck_uuid, result=result)
    print(f'stamp-sim: {args.name if args.id_prefix is None else args.id_prefix}')


# --------------------------------------------------------------------------- #
# Deck edits (typed edits over the local DecksStore — the guided-build surface)
# --------------------------------------------------------------------------- #


def _resolve_target(access: DeckAccess, name: str | None, id_prefix: str | None) -> str:
    """Resolve a verb's (NAME | ``--id`` prefix) to a ``deck_uuid``.

    ``--id`` takes PRECEDENCE over the positional name (design §2/§3): when given,
    it prefix-matches ``deck_uuid`` and the name is ignored; otherwise the name is
    resolved (dup names refuse with the candidate list). Exactly one is required.
    """
    if id_prefix is not None:
        return access.resolve(id_prefix=id_prefix)
    if not name:
        raise CollectionError('a deck name or --id prefix is required')
    return access.resolve(name)


def _resolve_existing_local(access: DeckAccess, name: str | None, id_prefix: str | None) -> str:
    """Resolve a verb that requires an EXISTING LOCAL row (archive/unarchive) — clean miss.

    Unlike ``_resolve_target``, a name-miss here is NOT a create: the resolver would
    mint a fresh uuid and the downstream op would raise ``no deck with id '<hex>'``,
    leaking a uuid the user never saw (F13). Instead, a missing local row is a clean
    ``error: no deck named 'X'`` — never a minted uuid, never a filesystem path.
    """
    if id_prefix is not None:
        return access.resolve(id_prefix=id_prefix)
    if not name:
        raise CollectionError('a deck name or --id prefix is required')
    from pipeline.decks import DecksStore

    deck_uuid = DecksStore().uuid_for_name(name)
    if deck_uuid is None:
        raise CollectionError(f'no deck named {name!r}')
    return deck_uuid


def _resolve_deck_uuid(access: DeckAccess, name: str) -> str:
    """Resolve a deck NAME to the local ``deck_uuid`` the edit verbs operate on.

    The MINIMAL name->uuid shim (design §3): names are labels, ``deck_uuid`` is the
    identity. A known local row (a draft, or an already-pulled synced deck) resolves
    to its stable uuid; when no local row exists yet the name is a SYNCED source
    deck — ``read_deck`` pulls it current into the local store (per the W4 policy)
    so the typed edit has a row to mutate, and the row is then keyed by the uuid the
    pull minted. A name that is neither a known draft nor a readable source deck
    surfaces the source's clean error.
    """
    # Bind by external ref so a case-alias / rename WRITE lands on the ONE canonical
    # row (F9 — the M4 write-path fix), never a freshly-minted uuid. ``resolve_bound``
    # short-circuits a directly-resolvable name and otherwise reads/binds the source.
    return access.resolve_bound(name)


def _resolve_edit_target(access: DeckAccess, name: str | None, id_prefix: str | None) -> tuple[str, str]:
    """Resolve a deck-EDIT verb's (NAME | ``--id``) to ``(deck_uuid, effective_name)``.

    ``--id`` takes precedence (design §2/§3). For an ``--id`` target we pull-current
    an absent/stale synced row so the typed edit has a row to mutate, mirroring the
    name path's ``_resolve_deck_uuid``. The effective name is the row's own label,
    used to address the source push at the commit boundary.
    """
    if id_prefix is not None:
        deck_uuid = access.resolve(id_prefix=id_prefix)
        from pipeline.decks import DecksStore

        row = DecksStore().get_row(deck_uuid)
        eff_name = row.name if row is not None else (name or '')
        if row is not None and row.sync_status == 'synced' and not access.has_local_row(deck_uuid):
            access.read_deck(eff_name, id_prefix=id_prefix)
        return deck_uuid, eff_name
    if not name:
        raise CollectionError('a deck name or --id prefix is required')
    deck_uuid = _resolve_deck_uuid(access, name)
    # The effective name must be the ROW's CANONICAL name (not the raw alias) so the
    # commit-boundary push resolves back to THIS row (F9): pushing under a cased alias
    # would re-mint a uuid and fail. Fall back to the given name if the row is absent.
    from pipeline.decks import DecksStore

    row = DecksStore().get_row(deck_uuid)
    eff_name = row.name if row is not None else name
    return deck_uuid, eff_name


def _commit_deck_edit(access: DeckAccess, name: str, deck_uuid: str) -> None:
    """PUSH a just-applied local deck edit to the source, unless it is ephemeral.

    A SYNCED deck edit must be committed through the source ceremony at the edit
    boundary — otherwise a later ``read_deck`` (whose W4 pull policy re-pulls a
    stale source) would silently revert the local edit. An EPHEMERAL draft has no
    source to push to, so it stays purely local (that is the point of a draft).
    """
    from pipeline.decks import DecksStore

    row = DecksStore().get_row(deck_uuid)
    if row is not None and row.sync_status == 'synced' and row.source_ref is not None:
        access.push(name)


def _commit_deck_edit_transactional(access: DeckAccess, name: str, deck_uuid: str, pre_edit: Deck | None) -> None:
    """Commit a just-applied edit; ROLL BACK the local edit if the commit is refused (M5 tail).

    Transactional edit+commit: the typed edit already landed locally; this pushes it
    to the source. If the push is REFUSED (shrink guard / :class:`SyncDriftError`),
    the edit must not half-land — restore the local deck to ``pre_edit`` (popping the
    poison ledger head) so a later verb is not poisoned, then re-raise the original
    error so the user sees why the edit was rejected. An ephemeral draft (no source)
    never pushes, so there is nothing to roll back.
    """
    from pipeline.decks import DecksStore

    try:
        _commit_deck_edit(access, name, deck_uuid)
    except Exception:
        if pre_edit is not None:
            DecksStore().rollback_failed_edit(deck_uuid, pre_edit)
        raise


def _positive_qty(value: str) -> int:
    """An argparse ``type=`` that accepts only a qty >= 1 (M5).

    A ``--qty`` below 1 is a clean CLI error (argparse exit 2), NOT a traceback:
    ``deck-remove --qty -1`` can no longer GROW the deck and ``deck-add --qty 0``
    can no longer land a 0-qty entry (both mirror the ``DeckCard.quantity`` ge=1
    model guard at the CLI boundary).
    """
    try:
        qty = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f'{value!r} is not an integer') from None
    if qty < 1:
        raise argparse.ArgumentTypeError(f'must be at least 1 (got {qty})')
    return qty


def _resolve_card_name(name: str) -> str:
    """Canonicalize a card ``name`` via the REAL resolver — canonical on a hit, raw on a miss (M3).

    ``deck-add`` / ``deck-swap`` resolve the card name through the package resolver
    BEFORE building the ``DeckCard`` so a local entry's name matches the source's
    canonicalization: ``lightning bolt`` becomes canonical ``Lightning Bolt`` and
    merges into the existing entry (never a duplicate singleton that a later pull
    hydrates into two — the M3 fix). An UNRESOLVED name passes through VERBATIM
    (honest — unreleased / spoiler cards are real but not yet on Scryfall, per the
    resolving-card-names lesson), so a miss never drops or mangles the name.
    """
    from pipeline.collection import resolver as resolver_mod

    card = resolver_mod.default_card_resolver().get_card(name)
    return card.name if card is not None and card.name else name


def _deck_swap(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='collection deck-swap',
        description='Swap one card for another in a deck (size-preserving; commander-safe).',
    )
    parser.add_argument('deck', nargs='?')
    parser.add_argument('--add', required=True, help='Card name to add.')
    parser.add_argument('--cut', required=True, help='Card name to cut (one copy).')
    parser.add_argument('--role', default=None, help='Role for the added card (e.g. commander, sideboard).')
    parser.add_argument('--why', default=None, help='Rationale recorded in the deck ledger.')
    parser.add_argument('--id', dest='id_prefix', default=None, help='Address by deck_uuid prefix (overrides name).')
    args = parser.parse_args(argv)

    from pipeline.decks import DecksStore

    access = _deck_access(writes_enabled=True)
    deck_uuid, eff_name = _resolve_edit_target(access, args.deck, args.id_prefix)
    add_name = _resolve_card_name(args.add)
    cut_name = _resolve_card_name(args.cut)
    store_ = DecksStore()
    pre_edit = store_.get(deck_uuid)
    store_.swap(deck_uuid, add=DeckCard(name=add_name, role=args.role), cut=cut_name, rationale=args.why)
    _commit_deck_edit_transactional(access, eff_name, deck_uuid, pre_edit)
    print(f'deck-swap: {eff_name}  -{cut_name}  +{add_name}')


def _deck_add(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='collection deck-add', description='Add a card to a deck (increments an existing entry by name).'
    )
    parser.add_argument('deck', nargs='?')
    parser.add_argument('card')
    parser.add_argument('--qty', type=_positive_qty, default=1, help='Copies to add (default 1; must be >= 1).')
    parser.add_argument('--role', default=None, help='Role for the added card (e.g. commander, sideboard).')
    parser.add_argument('--why', default=None, help='Rationale recorded in the deck ledger.')
    parser.add_argument('--id', dest='id_prefix', default=None, help='Address by deck_uuid prefix (overrides name).')
    args = parser.parse_args(argv)

    from pipeline.decks import DecksStore

    access = _deck_access(writes_enabled=True)
    deck_uuid, eff_name = _resolve_edit_target(access, args.deck, args.id_prefix)
    card_name = _resolve_card_name(args.card)
    store_ = DecksStore()
    pre_edit = store_.get(deck_uuid)
    store_.add_card(deck_uuid, DeckCard(name=card_name, quantity=args.qty, role=args.role), rationale=args.why)
    _commit_deck_edit_transactional(access, eff_name, deck_uuid, pre_edit)
    print(f'deck-add: {eff_name}  +{args.qty}x {card_name}')


def _deck_remove(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='collection deck-remove',
        description='Remove copies of a card from a deck (quantity-aware; refuses to shrink under target).',
    )
    parser.add_argument('deck', nargs='?')
    parser.add_argument('card')
    parser.add_argument('--qty', type=_positive_qty, default=1, help='Copies to remove (default 1; must be >= 1).')
    parser.add_argument('--why', default=None, help='Rationale recorded in the deck ledger.')
    parser.add_argument('--id', dest='id_prefix', default=None, help='Address by deck_uuid prefix (overrides name).')
    args = parser.parse_args(argv)

    from pipeline.decks import DecksStore

    access = _deck_access(writes_enabled=True)
    deck_uuid, eff_name = _resolve_edit_target(access, args.deck, args.id_prefix)
    card_name = _resolve_card_name(args.card)
    store_ = DecksStore()
    pre_edit = store_.get(deck_uuid)
    store_.remove_card(deck_uuid, card_name, qty=args.qty, rationale=args.why)
    _commit_deck_edit_transactional(access, eff_name, deck_uuid, pre_edit)
    print(f'deck-remove: {eff_name}  -{args.qty}x {card_name}')


def _source_deck_exists(name: str) -> bool:
    """True iff a deck named ``name`` reads on the source of record (one cheap probe).

    Backs the F7 new-draft collision guard: ``resolve`` consults only LOCAL rows, so
    a never-pulled source deck is invisible to it. A direct ``driver.get_deck`` probe
    (name/slug for local YAML; a filterByFormula for Airtable) surfaces it. A miss
    (``FileNotFoundError`` / any read error) reads as "not on the source" — the guard
    then permits the draft.
    """
    try:
        _store().get_deck(name)
    except FileNotFoundError:
        return False
    except Exception:
        return False
    return True


def _new_draft(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='collection new-draft',
        description='Create an EPHEMERAL (local-only) deck draft — clean-slate, or a copy of an existing deck.',
    )
    parser.add_argument('name')
    parser.add_argument('--from', dest='source', default=None, help='Copy an existing deck as the starting point.')
    parser.add_argument('--commander', default=None, help='Commander card name (clean-slate only).')
    parser.add_argument('--format', dest='format_', default=None, help="Deck format (e.g. 'Commander').")
    parser.add_argument(
        '--force',
        action='store_true',
        help='Make a same-named draft even when a deck of that name already exists on the source.',
    )
    args = parser.parse_args(argv)

    from uuid import uuid4

    from pipeline.decks import DecksStore

    decks = DecksStore()
    # F7 (USER DECISION): a clean-slate draft named after an EXISTING source deck is
    # refused without --force — a same-named draft silently SHADOWS the source deck
    # (get-deck / set-strategy land on the draft; the real deck is unreachable by
    # name). One cheap driver probe; --from is exempt (it deliberately branches).
    if args.source is None and not args.force and _source_deck_exists(args.name):
        raise CollectionError(
            f'a deck named {args.name!r} already exists on the source; use --from to branch '
            'it into an exploration draft, or --force to make a same-named draft anyway.'
        )
    derived_from: str | None = None
    if args.source is not None:
        # An exploration COPY of an existing deck — read it via the access path
        # (pull-current per the policy), then re-name it as a local-only draft. A
        # FRESH uuid is minted so the draft can never hijack the source's row
        # (design §3); the airtable binding is dropped (a draft has no source of
        # record yet). The parent's ``deck_uuid`` is recorded as ``derived_from``
        # (LINEAGE) so a later ``promote`` commits back onto the PARENT's external
        # ref (never clobbering an unrelated deck by name — kills B1).
        access = _deck_access()
        source_deck = access.read_deck(args.source)
        derived_from = access.resolve(args.source)
        draft = source_deck.model_copy(
            update={'name': args.name, 'airtable_record_id': None, 'uuid': uuid4().hex}
        )
    else:
        # A clean-slate draft: a minimal Deck (name/format + optional commander); its
        # uuid is minted by the Deck model's default_factory. No lineage. The
        # commander is CANONICALIZED (F10 — the M3 hole one verb over): a raw
        # ``krenko, mob boss`` becomes canonical so a later ``deck-add`` of the same
        # card merges into the one entry instead of spawning a duplicate singleton.
        cards = (
            [DeckCard(name=_resolve_card_name(args.commander), role='commander')] if args.commander else []
        )
        draft = Deck(name=args.name, format=args.format_, cards=cards)
    deck_uuid = decks.create_ephemeral(draft, derived_from=derived_from)
    print(f'new-draft: {args.name} [ephemeral] ({deck_uuid})')


def _promote_deck(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='collection promote-deck',
        description=(
            'Promote an ephemeral draft to a SYNCED deck. An exploration draft '
            '(new-draft --from) commits onto its lineage PARENT and is consumed; a '
            'clean-slate draft CREATES a new source deck named --to.'
        ),
    )
    parser.add_argument('deck', nargs='?', help='The ephemeral draft name to promote.')
    parser.add_argument(
        '--id',
        dest='id_prefix',
        default=None,
        help='Address the draft by deck_uuid prefix (disambiguates dup-named drafts; overrides name).',
    )
    parser.add_argument(
        '--to',
        dest='source_name',
        default=None,
        help='New source deck name (clean-slate drafts only; ignored for --from exploration drafts).',
    )
    args = parser.parse_args(argv)

    from pipeline.decks import DecksStore, sync

    access = _deck_access(writes_enabled=True)
    decks = DecksStore()
    # Route the lookup through the P2 resolver (F6): dup-named drafts REFUSE with a
    # copy-paste candidate list rather than silently promoting the oldest; ``--id``
    # is the escape hatch. A clean name-miss is a clear error (F13), never a uuid.
    if args.id_prefix is not None:
        deck_uuid = access.resolve(id_prefix=args.id_prefix)
    elif args.deck:
        if decks.uuid_for_name(args.deck) is None:
            raise CollectionError(f"no deck named {args.deck!r} to promote")
        deck_uuid = access.resolve(args.deck)
    else:
        raise CollectionError('promote-deck: a draft name or --id prefix is required')

    row = decks.get_row(deck_uuid)
    if row is None or not decks.exists(deck_uuid):
        raise CollectionError(f"no ephemeral draft named {args.deck!r} to promote")
    # F5: promote is for DRAFTS only. A synced (or consumed) row is refused BEFORE
    # any write, so a synced source deck can never be renamed in place by promoting it.
    if row.sync_status != 'ephemeral':
        raise CollectionError(
            f'{row.name!r} is already a {row.sync_status} deck — promote is for drafts '
            '(new-draft). Edit it directly, or branch it with new-draft --from.'
        )
    is_exploration = bool(row.derived_from)
    if not is_exploration and not args.source_name:
        raise CollectionError(f'promote-deck {row.name!r}: a clean-slate draft requires --to <name>')

    driver = _store(writes_enabled=True)
    sync.promote(decks, driver, deck_uuid=deck_uuid, to_name=args.source_name)
    # No force-pull (F4): ``sync.promote`` already made the affected canonical row
    # current via its own ``decks.put`` — an exploration refreshes the PARENT row and
    # a clean-slate makes the draft's OWN row synced. A name-addressed re-pull here
    # would rebind a just-promoted --to name onto an UNRELATED same-named source deck.
    if is_exploration:
        # F16: an exploration lands on the lineage PARENT — name IT, not the draft.
        parent_row = decks.get_row(row.derived_from) if row.derived_from else None
        landed = parent_row.name if parent_row is not None else row.name
    else:
        landed = args.source_name or row.name
    print(f'promote-deck: {row.name} -> {landed} [synced]')


def _undo_deck(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='collection undo-deck',
        description=(
            'Restore a deck to its prior ledger version (step back one edit). '
            'NOTE: undo can undo a PULL — an intervening auto-pull is an undoable step, '
            'so an undo after one steps back over it (the foreign source change is '
            'recoverable via the ledger).'
        ),
    )
    parser.add_argument('deck', nargs='?')
    parser.add_argument('--id', dest='id_prefix', default=None, help='Address by deck_uuid prefix (overrides name).')
    args = parser.parse_args(argv)

    from pipeline.decks import DecksStore

    access = _deck_access(writes_enabled=True)
    deck_uuid, eff_name = _resolve_edit_target(access, args.deck, args.id_prefix)
    restored = DecksStore().undo(deck_uuid)
    if restored is None:
        print(f'undo-deck: {eff_name} — nothing to undo')
    else:
        # The restore is a local edit; commit it so a synced deck's source reflects
        # the step-back (else the next read re-pulls the un-done state).
        _commit_deck_edit(access, eff_name, deck_uuid)
        print(f'undo-deck: {eff_name} — restored to {sum(c.quantity for c in restored.cards)} cards')


# --------------------------------------------------------------------------- #
# Archetype-fidelity signal (VALIDATE) — the deterministic combo check
# --------------------------------------------------------------------------- #


def _deck_combos(argv: list[str]) -> None:
    """Report the named-card combos present in a deck — the VALIDATE fidelity fold.

    Reads the deck's card names and runs ``combos_in_deck`` against the normalized
    combo table. Prints the matched combos plus a ``combo_data_available`` flag.

    HONEST DEGRADATION (design §5): if the combo lake is sparse — ``load_combos``
    raises, or returns empty — this prints ``combo_data_available: false`` and an
    EMPTY match set. That is INCONCLUSIVE, NOT a clean bill: an empty match with
    ``available: false`` must never be read as "no combos / safe to trust the sim
    verdict". Only ``available: true`` with an empty match set means "checked, none
    found". VALIDATE prose treats an unavailable check as unknown, not clean.
    """
    parser = argparse.ArgumentParser(
        prog='collection deck-combos',
        description='Report named-card combos present in a deck (archetype-fidelity signal).',
    )
    parser.add_argument('deck')
    args = parser.parse_args(argv)

    from pipeline.transforms.combo_detect import combos_in_deck, load_combos

    deck = _deck_access().read_deck(args.deck)
    names = {c.name for c in deck.cards}

    # A sparse/absent combo lake is INCONCLUSIVE, not clean — swallow the load
    # failure into `combo_data_available: false` rather than crash or imply safety.
    try:
        combos = load_combos()
        available = bool(combos)
    except Exception:  # any lake/read failure = inconclusive, not a defect.
        log.warning('deck-combos: combo lake unavailable; reporting inconclusive', exc_info=True)
        combos = []
        available = False

    matched = combos_in_deck(names, combos) if available else []
    out = {
        'deck': deck.name,
        'combo_data_available': available,
        'combos': [
            {'variant_id': c.variant_id, 'cards': list(c.card_names), 'result': c.result} for c in matched
        ],
    }
    print(json.dumps(out, indent=2))


# --------------------------------------------------------------------------- #
# Sync (manual pull / push / sync — the override; normally opaque) — W4
# --------------------------------------------------------------------------- #


def _pull(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='collection pull', description='Pull a deck from the source of record into the local decks store.'
    )
    parser.add_argument('name', nargs='?')
    parser.add_argument('--id', dest='id_prefix', default=None, help='Address by deck_uuid prefix (overrides name).')
    args = parser.parse_args(argv)
    _deck_access(writes_enabled=True).pull(args.name or '', id_prefix=args.id_prefix)
    print(f'pull: {args.name if args.id_prefix is None else args.id_prefix}')


def _push(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='collection push',
        description='Push the local deck to the source of record through the ceremony (drift-guarded).',
    )
    parser.add_argument('name', nargs='?')
    parser.add_argument(
        '--confirm',
        action='store_true',
        help='Allow a push that SHRINKS an at-target deck below target (the source shrink ceremony).',
    )
    parser.add_argument('--id', dest='id_prefix', default=None, help='Address by deck_uuid prefix (overrides name).')
    args = parser.parse_args(argv)
    _deck_access(writes_enabled=True).push(args.name or '', allow_shrink=args.confirm, id_prefix=args.id_prefix)
    print(f'push: {args.name if args.id_prefix is None else args.id_prefix}')


def _sync(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='collection sync', description='Pull-then-push a deck (reconcile local against the source).'
    )
    parser.add_argument('name', nargs='?')
    parser.add_argument('--confirm', action='store_true', help='Allow a shrinking push (see `push --confirm`).')
    parser.add_argument('--id', dest='id_prefix', default=None, help='Address by deck_uuid prefix (overrides name).')
    args = parser.parse_args(argv)
    _deck_access(writes_enabled=True).sync(args.name or '', allow_shrink=args.confirm, id_prefix=args.id_prefix)
    print(f'sync: {args.name if args.id_prefix is None else args.id_prefix}')


# --------------------------------------------------------------------------- #
# Audit (read-only drift detector — Phase 3)
# --------------------------------------------------------------------------- #


class _DeckDrift:
    """The mirror-diff of a below-target deck against its last known-good baseline.

    Shared by ``audit-decks`` (report) and ``recover-decks`` (propose/apply) so
    the mirror query + subset/overfill decision + unlinked/deleted-row tagging
    live in ONE place. Every field is derived read-only; nothing here mutates the
    source of record.

    Attributes:
        baseline: The newest at-target ``deck_history`` row, or None (fresh mirror).
        baseline_cards: The baseline's ``{name, oracle_id, quantity, role}`` list.
        missing: ``(name, tag)`` pairs for cards in the baseline but not current,
            tag ``'unlinked'`` (a live inventory row exists), ``'deleted-row'``
            (the row is gone but a history capture exists — faithful recreate), or
            ``'deleted-row:no-history'`` (the row is gone AND no history capture
            exists — apply will fabricate a PLACEHOLDER row, owned=1). Empty unless
            ``recoverable`` is True. The deleted-row vs no-history split is decided
            HERE (single source) so the dry-run plan and apply agree.
        diverged: True when the current deck holds a card NOT in the baseline (an
            active swap) — recovery must BLOCK, never overfill / re-add a cut card.
        recoverable: True when a baseline exists, the deck is a STRICT subset of it
            (not diverged), AND re-adding the missing set lands EXACTLY on target.
        predicted_size: current size + Σ missing quantities (the size after recovery).
    """

    def __init__(
        self,
        *,
        baseline: dict[str, object] | None,
        current_size: int,
        target: int | None,
        missing: list[tuple[str, str]],
        diverged: bool,
        recoverable: bool,
        predicted_size: int,
    ) -> None:
        self.baseline = baseline
        self.baseline_cards: list[dict] = list(baseline['cards']) if baseline else []  # type: ignore[index]
        self.current_size = current_size
        self.target = target
        self.missing = missing
        self.diverged = diverged
        self.recoverable = recoverable
        self.predicted_size = predicted_size


def _deck_drift(deck: Deck, store: CollectionStore) -> _DeckDrift:
    """Compute the shared mirror-diff for a below-target ``deck`` (read-only).

    Queries the append-only DuckDB history for the deck's newest at-target row,
    then classifies: no baseline -> not recoverable; a card gained vs the baseline
    -> ``diverged`` (blocked); otherwise the missing set is tagged unlinked vs
    deleted-row and ``recoverable`` iff re-adding it lands EXACTLY on target.
    """
    from pipeline import store as _lake

    # A writable connection: `last_known_good_deck` ensures the history tables
    # (a CREATE IF NOT EXISTS), which a read-only handle rejects. The query itself
    # never mutates the source of record — this is the local mirror, not Airtable.
    with _lake.connect() as conn:
        baseline = last_known_good_deck(conn, deck.name, store.backend_name)

    current_size = sum(c.quantity for c in deck.cards)
    target = deck.target_size
    if baseline is None:
        return _DeckDrift(
            baseline=None,
            current_size=current_size,
            target=target,
            missing=[],
            diverged=False,
            recoverable=False,
            predicted_size=current_size,
        )

    baseline_cards = baseline['cards']
    baseline_names = {c['name'] for c in baseline_cards}
    current_names = {c.name for c in deck.cards}
    missing_names = baseline_names - current_names
    # Divergence: the deck holds a card NOT in the baseline. This is an active
    # swap, not a pure shrink — recovery would overfill or re-add a cut card, so
    # it must BLOCK. `current ⊆ baseline` is the hard subset guard.
    diverged = not current_names <= baseline_names

    # Cross-reference live inventory: a row still present => unlinked; gone =>
    # deleted. For a deleted row, decide HERE (during planning, read-only) whether
    # a faithful recreate is possible: `deleted-row` when a history capture exists,
    # else `deleted-row:no-history` (apply will fabricate a PLACEHOLDER row). This
    # is the SINGLE source of truth the printed plan and apply both consume — the
    # history lookup is not repeated at apply time with a divergent result.
    inventory = store.list_inventory()
    live_names = {card.name for card in inventory}
    live_oracle_ids = {card.oracle_id for card in inventory if card.oracle_id}
    baseline_by_name = {c['name']: c for c in baseline_cards}
    tagged: list[tuple[str, str]] = []
    missing_qty = 0
    for name in sorted(missing_names):
        oracle_id = baseline_by_name[name].get('oracle_id')
        present = name in live_names or (oracle_id is not None and oracle_id in live_oracle_ids)
        if present:
            tag = 'unlinked'
        else:
            with _lake.connect() as conn:
                history = last_known_inventory_row(
                    conn,
                    store.backend_name,
                    name=name,
                    oracle_id=oracle_id,  # type: ignore[arg-type]
                )
            tag = 'deleted-row' if history is not None else 'deleted-row:no-history'
        tagged.append((name, tag))
        missing_qty += int(baseline_by_name[name].get('quantity') or 1)

    predicted_size = current_size + missing_qty
    # Recoverable ONLY when: not diverged, something IS missing, and re-adding the
    # missing set lands EXACTLY on target (never overfill; never a partial land).
    recoverable = not diverged and bool(tagged) and target is not None and predicted_size == target
    return _DeckDrift(
        baseline=baseline,
        current_size=current_size,
        target=target,
        missing=tagged,
        diverged=diverged,
        recoverable=recoverable,
        predicted_size=predicted_size,
    )


def _missing_card_diff(deck: Deck, store: CollectionStore) -> list[tuple[str, str]] | None:
    """Diff a below-target deck against its last known-good baseline in the mirror.

    Thin wrapper over :func:`_deck_drift` preserving the ``audit-decks`` contract:
    returns the ``(name, tag)`` missing set when the deck is a strict subset of a
    baseline, else ``None`` (no baseline, or the deck diverged / gained cards — a
    different change, not a shrink to list).
    """
    drift = _deck_drift(deck, store)
    if drift.baseline is None or drift.diverged or not drift.missing:
        return None
    return drift.missing


def _audit_decks(argv: list[str]) -> None:
    """Read-only drift report: per-deck size vs target, plus a known-good diff.

    Source-agnostic and non-mutating w.r.t. the source of record. First takes a
    best-effort full capture into the local DuckDB mirror (a local write, not an
    Airtable write), then reports each deck's ``name · format · size · target ·
    status``. Under-target is a WARNING (never an error, never a nonzero exit);
    untargeted decks are reported as such, never flagged. For a below-target deck
    with a known-good baseline, lists the missing cards tagged unlinked vs
    deleted-row; with no baseline yet, says so plainly. Always exits 0 (report).
    """
    parser = argparse.ArgumentParser(prog='collection audit-decks')
    parser.add_argument('--json', action='store_true', help='Emit machine-readable JSON instead of a table.')
    args = parser.parse_args(argv)

    store = _store()

    # Best-effort capture-then-report: the Phase-2 full-capture opportunity. A
    # mirror write must never break the audit, so swallow any failure.
    try:
        record_snapshot(store)
    except Exception:  # pragma: no cover - record_snapshot is already best-effort.
        log.warning('audit-decks: history capture failed; continuing with the audit', exc_info=True)

    decks = sorted(store.list_decks(), key=lambda d: d.name)
    reports: list[dict[str, object]] = []
    under_target = 0
    for deck in decks:
        size = sum(c.quantity for c in deck.cards)
        target = deck.target_size
        entry: dict[str, object] = {
            'name': deck.name,
            'format': deck.format,
            'size': size,
            'target': target,
        }
        if target is None:
            entry['status'] = 'untargeted'
        elif size >= target:
            entry['status'] = 'OK'
        else:
            entry['status'] = 'UNDER-TARGET'
            under_target += 1
            diff = _missing_card_diff(deck, store)
            entry['baseline'] = diff is not None
            entry['missing'] = [{'name': name, 'tag': tag} for name, tag in diff] if diff is not None else []
        reports.append(entry)

    if args.json:
        print(json.dumps({'decks': reports, 'under_target': under_target}, indent=2))
        return

    _print_audit_table(reports, under_target)


def _print_audit_table(reports: list[dict[str, object]], under_target: int) -> None:
    """Print the aligned per-deck table + a one-line summary to stdout."""
    name_w = max((len(str(r['name'])) for r in reports), default=4)
    fmt_w = max((len(str(r['format'] or '-')) for r in reports), default=6)
    header = f'{"DECK":<{name_w}}  {"FORMAT":<{fmt_w}}  {"SIZE":>4}  {"TARGET":>6}  STATUS'
    print(header)
    print('-' * len(header))
    for r in reports:
        target = r['target']
        target_str = '-' if target is None else str(target)
        name = str(r['name'])
        fmt = str(r['format'] or '-')
        size = str(r['size'])
        line = f'{name:<{name_w}}  {fmt:<{fmt_w}}  {size:>4}  {target_str:>6}  {r["status"]}'
        print(line)
        if r['status'] == 'UNDER-TARGET':
            missing = r.get('missing') or []
            if not r.get('baseline'):
                print('    no historical baseline; target check only')
            elif missing:
                for entry in missing:  # type: ignore[union-attr]
                    print(f'    missing: {entry["name"]}  [{entry["tag"]}]')
    print(f'\n{len(reports)} decks, {under_target} under target')


# --------------------------------------------------------------------------- #
# Recover (mirror-based recovery — Phase 5; PROPOSES, never blind-applies)
# --------------------------------------------------------------------------- #


def _rebuild_deck_to_baseline(deck: Deck, drift: _DeckDrift) -> Deck:
    """Return a copy of ``deck`` grown back to its baseline composition.

    The baseline mirror row is authoritative for the recovered deck: we rebuild
    the FULL card list (name + role + quantity) from ``drift.baseline_cards`` so a
    single ``save_deck`` re-links every dropped card at once. This only GROWS to
    target (recovery is a subset restore), so ``allow_shrink`` stays False and the
    shrink guard is never tripped.
    """
    cards = [
        DeckCard(name=c['name'], role=c.get('role'), quantity=int(c.get('quantity') or 1)) for c in drift.baseline_cards
    ]
    return deck.model_copy(update={'cards': cards})


def _recreate_deleted_rows(store: CollectionStore, drift: _DeckDrift) -> list[str]:
    """Recreate any ``deleted-row`` inventory rows from history BEFORE the re-link.

    ``save_deck`` resolves deck-card NAMES to Inventory record ids and raises on an
    unresolved name, so a card whose inventory row was deleted MUST be re-created
    first. The recreation payload (condition/foil/sets/sources/counts) comes from
    the newest ``inventory_history`` capture of that card. Returns the recreated
    card names (for the report). Ordering is load-bearing: recreate, THEN save.

    The ``deleted-row`` vs ``deleted-row:no-history`` split was already decided in
    :func:`_deck_drift` (the single source), and the dry-run plan surfaced the
    placeholder case to the user before ``--confirm`` — so here we simply honor the
    tag: a faithful recreate from history, or a PLACEHOLDER row (owned=1) when no
    history exists.
    """
    from pipeline import store as _lake

    baseline_by_name = {c['name']: c for c in drift.baseline_cards}
    recreated: list[str] = []
    for name, tag in drift.missing:
        if tag not in ('deleted-row', 'deleted-row:no-history'):
            continue
        oracle_id = baseline_by_name.get(name, {}).get('oracle_id')
        if tag == 'deleted-row:no-history':
            fields = None
        else:
            with _lake.connect() as conn:
                fields = last_known_inventory_row(conn, store.backend_name, name=name, oracle_id=oracle_id)
        # No inventory history for this card: recreate a MINIMAL/PLACEHOLDER row
        # (name + 1 owned) so the re-link can resolve it, rather than fail the whole
        # deck. Enrichment backfills on the next capture. (Surfaced in the plan.)
        qty = int((fields or {}).get('number_owned') or 1)
        foil = int((fields or {}).get('foil_count') or 0)
        condition = (fields or {}).get('condition') or None
        sets = (fields or {}).get('sets') or None
        sources = (fields or {}).get('sources') or None
        store.add_card(name, max(qty, 1), condition=condition, foil=foil, sets=sets, sources=sources)
        recreated.append(name)
    return recreated


def _recover_decks(argv: list[str]) -> None:
    """Mirror-based recovery for a below-target deck — PROPOSES, never blind-applies.

    Dry-run by DEFAULT: without ``--confirm`` it prints the per-deck plan and exits
    0, writing NOTHING. ``--confirm`` is required to actually write to the source of
    record (mirrors the ``copy --confirm`` precedent).

    The plan (read-only) diffs each below-target deck against its last known-good
    mirror baseline. A deck is recovered ONLY when it is a STRICT SUBSET of the
    baseline AND re-adding the missing set lands EXACTLY on target — the
    subset/overfill guard. A deck that diverged (gained a card not in the baseline)
    is BLOCKED ("manual review"): recovery must never overfill or re-add a cut
    card. A deck with no baseline is reported and skipped (never fabricated).

    Apply (``--confirm`` only): recreate any ``deleted-row`` inventory rows from
    history FIRST (else the re-link can't resolve them), then rebuild the deck to
    its baseline composition via ``save_deck(allow_shrink=False)`` (recovery only
    grows, so the shrink guard never trips), then re-read and verify size==target.
    """
    parser = argparse.ArgumentParser(prog='collection recover-decks')
    parser.add_argument('names', nargs='*', help='Deck name(s) to recover (default: all under-target decks).')
    parser.add_argument(
        '--confirm',
        action='store_true',
        help='Required to actually WRITE the recovery to the source of record. Without it, '
        'recover-decks is a dry run: it prints the proposed plan and writes nothing.',
    )
    args = parser.parse_args(argv)

    store = _store(writes_enabled=True)
    all_decks = {d.name: d for d in store.list_decks()}
    if args.names:
        missing_names = [n for n in args.names if n not in all_decks]
        if missing_names:
            raise CollectionError(f'unknown deck(s): {missing_names}; known: {sorted(all_decks)}')
        selected = [all_decks[n] for n in args.names]
    else:
        # Default: every under-target (targeted-and-below) deck.
        selected = [
            d
            for d in all_decks.values()
            if d.target_size is not None and sum(c.quantity for c in d.cards) < d.target_size
        ]

    selected.sort(key=lambda d: d.name)

    recovered = 0
    blocked = 0
    skipped = 0
    for deck in sorted(selected, key=lambda d: d.name):
        # `get_deck` hydrates cards (oracle_id) so the mirror diff can match on the
        # stabler key; `list_decks` is name-only.
        hydrated = store.get_deck(deck.name)
        drift = _deck_drift(hydrated, store)
        size = sum(c.quantity for c in hydrated.cards)
        header = f'{deck.name}  (size {size}, target {drift.target})'

        if drift.baseline is None:
            print(f'{header}: no baseline in the mirror — cannot recover; skipping.')
            skipped += 1
            continue
        if drift.diverged:
            print(
                f'{header}: BLOCKED — diverged from baseline (holds cards not in the last-good '
                'snapshot; likely an active swap). Manual review required; recovering nothing.'
            )
            blocked += 1
            continue
        if not drift.recoverable:
            # Subset, but re-adding wouldn't land exactly on target (e.g. an
            # overfill or a partial baseline). Block rather than guess.
            print(
                f'{header}: BLOCKED — cannot land exactly on target '
                f'(predicted {drift.predicted_size} vs target {drift.target}); recovering nothing.'
            )
            blocked += 1
            continue

        print(f'{header}: recover {len(drift.missing)} card(s) -> predicted size {drift.predicted_size}')
        placeholder_count = 0
        for name, tag in drift.missing:
            if tag == 'deleted-row:no-history':
                placeholder_count += 1
                print(f'    + {name}  [deleted-row: NO history — placeholder recreate (owned=1), VERIFY after]')
            else:
                print(f'    + {name}  [{tag}]')
        if placeholder_count:
            print(
                f'    ⚠ {placeholder_count} card(s) will be recreated as placeholders — '
                'verify their owned counts after recovery.'
            )

        if not args.confirm:
            recovered += 1  # would-recover count in dry-run.
            continue

        # APPLY. Ordering is load-bearing: recreate deleted rows BEFORE save_deck,
        # else the re-link can't resolve them and raises.
        recreated = _recreate_deleted_rows(store, drift)
        if recreated:
            print(f'    recreated {len(recreated)} inventory row(s): {recreated}')
        rebuilt = _rebuild_deck_to_baseline(hydrated, drift)
        store.save_deck(rebuilt, allow_shrink=False)
        # Verify against a fresh read.
        after = store.get_deck(deck.name)
        after_size = sum(c.quantity for c in after.cards)
        status = 'OK' if after_size == drift.target else 'CHECK'
        print(f'    {status}: deck is now {after_size} (target {drift.target})')
        recovered += 1

    verb = 'would recover' if not args.confirm else 'recovered'
    summary = f'\n{len(selected)} deck(s): {recovered} {verb}, {blocked} blocked, {skipped} skipped (no baseline)'
    if not args.confirm:
        summary += '  (dry-run — no writes; pass --confirm to apply)'
    print(summary)


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
    parser.add_argument(
        '--force',
        action='store_true',
        help='Required to remove a card that is LINKED to one or more decks — deleting the shared '
        'Inventory row cascades the card out of EVERY deck at once. Without it, remove-card enumerates '
        'the affected decks and aborts (no delete).',
    )
    args = parser.parse_args(argv)
    store = _store(writes_enabled=True)
    # Cascade guard (the critical one): a HARD delete of a shared Inventory row
    # silently strips the card from every deck that links it (Airtable link
    # cascade). If ANY deck links `ref`, require --force and enumerate the blast
    # radius FIRST — the delete must never reach the store without --force.
    impacts = remove_impact(store, args.ref)
    if impacts and not args.force:
        lines = [
            f"remove-card '{args.ref}' is LINKED to {len(impacts)} deck(s) — deleting the shared "
            'Inventory row would cascade it out of ALL of them at once:'
        ]
        for imp in impacts:
            flag = '  ** DROPS UNDER TARGET **' if imp.goes_under_target else ''
            target = imp.target if imp.target is not None else '—'
            lines.append(f'  - {imp.deck_name}: {imp.current_size} -> {imp.size_after} (target {target}){flag}')
        lines.append('Pass --force to remove it anyway (this will unlink it from every deck above).')
        parser.error('\n'.join(lines))
    # The CLI already enumerated + aborted above when --force is absent, so this
    # never double-prompts; pass `force` through so the CLI and the port-level
    # guard agree (the store re-checks as defense-in-depth for direct callers).
    store.remove_card(args.ref, force=args.force)
    if impacts:
        print(f'remove-card: {args.ref} (forced; unlinked from {len(impacts)} deck(s))')
    else:
        print(f'remove-card: {args.ref}')


# --------------------------------------------------------------------------- #
# Chase
# --------------------------------------------------------------------------- #


def _list_chase(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='collection list-chase')
    parser.parse_args(argv)
    print(_dump(_store().list_chase()))


def _add_chase(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='collection add-chase',
        description='Track a card you want to acquire (a "chase" card). '
        'Note: --priority/--status/--target-price are NOT persisted by the Airtable backend (no such columns).',
    )
    parser.add_argument('ref', help='Card name (or Scryfall id) to chase; hydrated live from Scryfall.')
    parser.add_argument('--priority', type=int, default=None, help='Optional priority rank (local backend only).')
    parser.add_argument('--for-deck', dest='for_deck', default=None, help='Deck name this chase is intended for.')
    parser.add_argument('--status', default=None, help='Optional status label (local backend only).')
    parser.add_argument(
        '--target-price',
        dest='target_price',
        type=float,
        default=None,
        help='Optional target acquisition price (local backend only).',
    )
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
    parser = argparse.ArgumentParser(
        prog='collection log-trade',
        description='Record a trade. Provide a full JSON Trade via --from-json, or build one from the flags below.',
    )
    parser.add_argument('--from-json', help='Path to a JSON Trade (- for stdin). If set, the other flags are ignored.')
    parser.add_argument('--from-source', dest='from_source', help="Trade source, e.g. a person or 'Deck'.")
    parser.add_argument('--to-destination', dest='to_destination', help="Trade destination, e.g. a person or 'Deck'.")
    parser.add_argument('--from-deck', dest='from_deck', default=None, help="From (Deck) when Source == 'Deck'.")
    parser.add_argument('--to-deck', dest='to_deck', default=None, help="To (Deck) when Destination == 'Deck'.")
    parser.add_argument('--status', default=None, help='Optional trade status label.')
    parser.add_argument('--notes', default=None, help='Free-text notes for the trade.')
    parser.add_argument(
        '--card-in', dest='cards_in', action='append', default=None, help='A card received (repeatable).'
    )
    parser.add_argument(
        '--card-out', dest='cards_out', action='append', default=None, help='A card given away (repeatable).'
    )
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


def _build_store(backend: str, *, writes_enabled: bool = False, data_dir: str | None = None) -> CollectionStore:
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
    parser.add_argument('name', nargs='?')
    parser.add_argument('--id', dest='id_prefix', default=None, help='Address by deck_uuid prefix (overrides name).')
    parser.add_argument('--focus', default=None, help='Comma-separated focus set.')
    args = parser.parse_args(argv)
    if args.id_prefix is None and not args.name:
        raise CollectionError('factsheet: a deck name or --id prefix is required')
    # Route through the local decks store (design §4, B4) so factsheet works on
    # ephemeral drafts too; --id takes precedence over the name.
    deck = _deck_access().read_deck(args.name or '', id_prefix=args.id_prefix)

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
    'stamp-sim': _stamp_sim,
    # deck edits (typed edits + ephemeral lifecycle — the guided-build surface)
    'deck-swap': _deck_swap,
    'deck-add': _deck_add,
    'deck-remove': _deck_remove,
    'new-draft': _new_draft,
    'promote-deck': _promote_deck,
    'undo-deck': _undo_deck,
    'deck-combos': _deck_combos,
    'pull': _pull,
    'push': _push,
    'sync': _sync,
    'archive-deck': _archive_deck,
    'unarchive-deck': _unarchive_deck,
    'audit-decks': _audit_decks,
    'recover-decks': _recover_decks,
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
    usage = (
        "usage: collection <verb> [args...]   (run `collection <verb> -h` for a verb's flags)\n"
        f'  verbs: {", ".join(sorted(VERBS))}'
    )
    # Explicit -h/--help -> usage on stdout, exit 0. No/unknown verb -> stderr, exit 2.
    if len(sys.argv) >= 2 and sys.argv[1] in ('-h', '--help'):
        print(usage)
        return
    if len(sys.argv) < 2 or sys.argv[1] not in VERBS:
        print(usage, file=sys.stderr)
        raise SystemExit(2)
    verb = sys.argv[1]
    # `ReadOnlyStoreError` lives in the lazily-imported Airtable adapter; import it
    # here so the local-only path never triggers the adapter import.
    from pipeline.collection.adapters.airtable_collection import ReadOnlyStoreError
    from pipeline.decks.store import DecksError
    from pipeline.decks.sync import SyncDriftError

    try:
        VERBS[verb](sys.argv[2:])
    except (
        FileNotFoundError,
        CollectionError,
        ReadOnlyStoreError,
        AirtableConfigError,
        DecksError,
        SyncDriftError,
    ) as exc:
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
