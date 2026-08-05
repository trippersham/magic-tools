"""Airtable destination: sync engine-derived per-card otag facts -> Airtable Cards.

This reverse-ETL destination owns exactly two derived fields on the *Cards* table.
The base id, the Cards table id, and the Card Name / field ids are not hard-coded:
the base id + Cards table name come from env-driven :mod:`pipeline.config`, and
the ``tbl…``/``fld…`` ids are resolved at runtime via the Airtable meta API — so
the write-back is not locked to one Airtable instance. A card's otags are a pure
function of the card (keyed to its Scryfall ``oracle_id``), so storing them on
Cards is authoritative and never stale; Airtable rolls them up to decks natively
(the "derived/bulk -> Local authoritative -> sync to Airtable" authority rule).

    - ``⚙ Buckets`` — Airtable **multipleSelects** — the crosswalked functional
      buckets for the card (``crosswalk.buckets_for``: removal / ramp / draw /
      counters / burn / tutor / typal / ...), the option set == the crosswalk
      vocabulary (incl. the flagged gap buckets).
    - ``⚙ Otags``  — Airtable **multilineText** — the card's raw rolled-up
      Scryfall otag slugs (newline-joined), kept for fidelity / debugging.

The governing safety property: this module may write only the fields in its
:data:`ALLOWLIST_NAMES` — the card-dim / engine-derived fields it owns. Those are
(a) the two engine-created namespace-prefixed ⚙ fields (``⚙ Buckets`` / ``⚙
Otags``) and (b) the Scryfall-derived Cards columns that are a pure function of
the card's identity (:data:`DERIVED_CARD_FIELDS`: Card Type, Mana Cost, CMC,
Power / Toughness, Oracle Text, Card Art, Scryfall URL, Price (TCGPlayer), Color
Identity). It must never create, update, or delete any human/collection-edited
Card field (Card Name, Sets, Number Owned, Sources, Condition, Number in
Library, Decks, ...) — those stay pull-only / owned by the collection layer and
the human. That property is enforced structurally, not by convention, at
three layers:

    1. Payload construction. :func:`build_payload` emits only keys drawn from
       :data:`DERIVED_FIELDS`. There is no code path that copies an arbitrary
       field name into a payload.
    2. A hard guard. :func:`assert_no_human_fields` computes the intersection of
       (payload field names) and (the human denylist loaded from
       ``regression/golden_contract.json`` — the Cards table's human fields) and
       raises :class:`HumanFieldWriteError` if it is non-empty — before any
       request is built. Every write path calls it.
    3. A guarded client. :class:`AllowlistWriteClient` wraps the private httpx
       client; every mutating request is routed through :meth:`_guarded_body`,
       which re-runs the guard on the exact body about to leave the process. The
       httpx client is name-mangled (private), which deters accidental bypass of
       the choke point — it is not a hard security boundary.

Join model: each Airtable Card record -> its ``oracle_id`` (resolved from the
card name against the Scryfall oracle-card table) -> its ``card_otag`` slug
closure -> ``buckets_for``. A card that does not robustly resolve to an
``oracle_id`` (or carries no otag data) is skipped and logged — never guessed.
Upsert is keyed on the Airtable Card **record id** with ``use_field_ids=True``,
so re-running is idempotent (no dupes, no thrash).

Dry-run is the default. :func:`sync` and :func:`ensure_fields` print the diff /
schema mutation they would apply and issue zero write requests unless the caller
passes ``dry_run=False`` and ``apply=True`` (belt-and-suspenders: a real write
requires both).

Transport mirrors ``sources/airtable.py`` (httpx + Bearer PAT); ``pyairtable`` is
declared as the ``airtable`` extra but is not required here.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import httpx

from pipeline.config import AirtableConfigError, get_settings
from pipeline.transforms.crosswalk import BUCKETS, buckets_for

log = logging.getLogger('make_magic.destinations.airtable')

API_ROOT = 'https://api.airtable.com/v0'
META_ROOT = 'https://api.airtable.com/v0/meta'
HEADERS_UA = {'User-Agent': 'make-magic-plugin/2.0'}

#: Card Name (primary) field name. The pull lands columns by field id, so the
#: lake loader resolves this name to its per-base id at runtime (via the meta
#: schema) to read the card-name column — no hard-coded ``fld…`` id.
CARD_NAME_FIELD = 'Card Name'

#: Airtable caps a single PATCH ``records[]`` array at 10 records. The apply path
#: must chunk the resolved writes to this size; a full-inventory run of hundreds
#: of cards would otherwise send one oversized request and be rejected.
MAX_RECORDS_PER_PATCH = 10

#: Airtable's REST API rate limit is ~5 requests/sec per base. We sleep this long
#: between chunk PATCHes to stay comfortably under it (5 req/s => 0.2s spacing).
INTER_CHUNK_DELAY_S = 0.25

#: Namespace prefix marking a field as engine-owned. The gear glyph makes it
#: visually unmistakable in the Airtable UI and guarantees the derived field
#: names cannot collide with any human-edited field name (all of which are plain
#: words: Card Name, Sets, Oracle Text, ...). See :data:`DERIVED_FIELDS`.
NS = '⚙ '  # "⚙ "


@dataclass(frozen=True)
class DerivedField:
    """One engine-owned Airtable field this destination is authorized to write.

    ``name`` is the Airtable field name (namespace-prefixed). ``airtable_type`` is
    the Airtable schema type used by :func:`ensure_fields` when creating it.
    """

    key: str
    name: str
    airtable_type: str
    options: dict[str, Any] | None = None


#: The multi-select option set for ``⚙ Buckets``. Airtable requires every
#: multipleSelects option to exist before it can be written; we seed the field
#: with the full crosswalk vocabulary (incl. the flagged gap buckets) at
#: :func:`ensure_fields` time, so any bucket ``buckets_for`` can emit is writable.
BUCKET_OPTIONS: tuple[str, ...] = BUCKETS

#: The engine-creatable derived fields. The two namespace-prefixed ⚙ fields this
#: adapter both creates (schema mutation via :func:`ensure_fields`) and writes.
#: Each maps a per-card derived value to a namespace-prefixed Cards field. These
#: do not exist on a fresh base, so the adapter is authorized to create them.
DERIVED_FIELDS: tuple[DerivedField, ...] = (
    DerivedField(
        key='buckets',
        name=f'{NS}Buckets',
        airtable_type='multipleSelects',
        options={'choices': [{'name': b} for b in BUCKET_OPTIONS]},
    ),
    DerivedField(
        key='otags',
        name=f'{NS}Otags',
        airtable_type='multilineText',
    ),
)

#: The card-dim / engine-derived Scryfall fields. These are plain (non-namespaced)
#: Inventory Cards columns that already exist on the live table and are a pure
#: function of the card's Scryfall identity (keyed to oracle_id) — so the card-dim
#: write-back owns them and may write them, even though the skills also read them.
#: Unlike the ⚙ fields above, the adapter never creates these (they already
#: exist); it only writes them. This list must match the golden contract's
#: ``tables."Inventory Cards".derived_fields`` — the lazy denylist loader asserts
#: that equality at first use (drift guard). This is the closed authorization set:
#: the only way to authorize a new derived write is to add it here and to the
#: contract's derived_fields.
DERIVED_CARD_FIELDS: frozenset[str] = frozenset(
    {
        'Card Type',
        'Mana Cost',
        'CMC',
        'Power / Toughness',
        'Oracle Text',
        'Card Art',
        'Scryfall URL',
        'Price (TCGPlayer)',
        'Color Identity',
    }
)

#: The Chase Cards card-dim derived fields. The Chase Cards table is a separate
#: Airtable table that carries the same eleven engine-derived columns as Inventory
#: Cards: the nine Scryfall-pure columns (:data:`DERIVED_CARD_FIELDS`) plus the two
#: engine ⚙ otag fields (⚙ Buckets / ⚙ Otags). So the chase derived set is those
#: eleven.
#:
#: Asymmetry (deliberate) vs Inventory's :data:`DERIVED_CARD_FIELDS` (nine): the two
#: constants are not identical. Inventory's inline derived-write emits only the
#: nine Scryfall columns — its two ⚙ fields are populated by the separate otag sync
#: path (:func:`sync` / :func:`build_payload`), so the ⚙ fields sit on the Inventory
#: allowlist but not in its inline-write set. Chase has no otag sync path, so the
#: chase inline write is the only way ⚙ Buckets / ⚙ Otags ever land on Chase — which
#: is why the two ⚙ fields are in :data:`CHASE_DERIVED_CARD_FIELDS` (and thus emitted
#: by :func:`build_chase_derived_card_payload`), not just on the allowlist.
#:
#: This is the closed authorization set for the Chase table; it is a different table
#: from Inventory (its own allowlist/denylist/guard binding) and must equal the
#: golden contract's ``tables."Chase Cards".derived_fields`` (drift guard at first
#: use).
CHASE_DERIVED_CARD_FIELDS: frozenset[str] = DERIVED_CARD_FIELDS | frozenset(f.name for f in DERIVED_FIELDS)

#: The allowlist. The only Airtable fields this adapter may ever touch: the two
#: engine-created ⚙ fields plus the card-dim derived Scryfall columns. Adding a
#: field here is the only way to authorize a new write; nothing else in this
#: module enumerates writable field names. Frozen so it can be used as a
#: membership check and diffed against the denylist at first use.
ALLOWLIST_NAMES: frozenset[str] = frozenset(f.name for f in DERIVED_FIELDS) | DERIVED_CARD_FIELDS

#: The chase allowlist. The only Chase Cards fields the inline chase derived-write
#: may touch: the eleven engine-derived columns — the nine Scryfall-pure columns
#: (which include Price (TCGPlayer) — Chase has that column, sourced live) plus the
#: two engine ⚙ otag fields (⚙ Buckets / ⚙ Otags), which exist on the live Chase
#: table. Unlike Inventory (whose ⚙ come from the separate otag sync), Chase has no
#: otag sync, so the inline write is chase's only path to ⚙ — hence they are both on
#: the allowlist and in :data:`CHASE_DERIVED_CARD_FIELDS` (emitted by the payload
#: builder). Closed set, diffed against the chase denylist at first use.
CHASE_ALLOWLIST_NAMES: frozenset[str] = CHASE_DERIVED_CARD_FIELDS

#: The chase wrong-table probe. The chase human fields the live Chase Cards table
#: is guaranteed to have (its primary ``Card Name`` + the ``Target Decks`` link the
#: read/write paths use). Used as :attr:`AllowlistWriteClient._probe_fields` for the
#: chase binding, kept as a tight, live-accurate probe separate from the write
#: denylist: it answers "is the resolved table really Chase Cards?" (both these
#: link/primary fields present), whereas the denylist answers "which fields may
#: never be written?". A tight probe still rejects a genuinely-wrong table (Decks
#: lacks 'Card Name'); the full chase write denylist is unchanged, so writing any
#: chase human field is still refused. (The chase contract denylist — Card Name /
#: Sets / Target Decks — is a subset of the live table, so it would also pass; the
#: tight probe is kept for the clearer wrong-table intent and the probe/denylist
#: split.)
CHASE_PROBE_FIELDS: frozenset[str] = frozenset({'Card Name', 'Target Decks'})

#: Convenience handles onto the two ⚙ field names (single source of truth = tuple).
BUCKETS_FIELD: str = f'{NS}Buckets'
OTAGS_FIELD: str = f'{NS}Otags'


class HumanFieldWriteError(RuntimeError):
    """Raised when a write payload would touch a human-edited (denylisted) field.

    This is the enforcement of the per-field authority invariant: the derived
    write-back must never create/update/delete a human field. Raised before any
    request is constructed.
    """


class ChunkWriteError(RuntimeError):
    """Raised when a chunked apply fails on one of its PATCH batches.

    Carries the partial progress so an operator (or a re-run) knows exactly how
    far the apply got before it stopped: ``chunks_completed`` chunks were PATCHed
    successfully out of ``chunks_planned`` total. Because the upsert is keyed on
    the Airtable record id (idempotent), a re-run re-issues the same plan and the
    already-written records are simply overwritten with identical values — so it
    is safe to resume from a partial failure. The original transport error is
    chained via ``raise ... from`` (inspect ``__cause__``).
    """

    def __init__(self, chunks_completed: int, chunks_planned: int) -> None:
        self.chunks_completed = chunks_completed
        self.chunks_planned = chunks_planned
        super().__init__(
            f'Chunked apply stopped after {chunks_completed}/{chunks_planned} '
            'chunk(s) PATCHed successfully. Re-run to resume (idempotent upsert by '
            'record id; completed chunks are simply re-written with identical values).'
        )


class NonAllowlistFieldError(RuntimeError):
    """Raised when a payload field is not in :data:`ALLOWLIST_NAMES`.

    Stronger than the denylist check: even a field that is neither human nor
    engine-owned (e.g. a typo, or a new human field added to Airtable after this
    contract was written) is refused. The allowlist is a closed set.
    """


# --------------------------------------------------------------------------- #
# Human denylist — the Cards table's human-edited fields, from the golden
# contract so it stays in sync.
# --------------------------------------------------------------------------- #

#: The Cards table key in the golden contract (target of this write-back). This
#: indexes the static contract (its table keys use the live name "Inventory
#: Cards"). It is deliberately distinct from ``config.Settings.cards_table`` (which
#: resolves the live schema by name); the two happen to share the same string but
#: are separate concerns — one keys the committed contract, the other hits the API.
CARDS_TABLE_NAME = 'Inventory Cards'

#: The Chase Cards table key in the golden contract (target of the inline chase
#: derived-write). Same distinction as :data:`CARDS_TABLE_NAME`: it indexes
#: the static contract, not the live schema (which config resolves by name).
CHASE_TABLE_NAME = 'Chase Cards'


def _golden_contract_path() -> Path:
    """Locate ``regression/golden_contract.json`` relative to this package.

    This file lives at ``pipeline/pipeline/destinations/airtable.py``;
    ``parents[3]`` is the ``make-magic`` plugin root that holds ``regression/``.
    """
    return Path(__file__).resolve().parents[3] / 'regression' / 'golden_contract.json'


def load_contract_derived_fields(
    path: Path | None = None,
    *,
    table_name: str = CARDS_TABLE_NAME,
) -> frozenset[str]:
    """Load the card-dim / engine-derived field names for ``table_name`` from the contract.

    For Inventory Cards these are the columns classified as a pure function of the
    card's Scryfall identity (``tables."Inventory Cards".derived_fields``). They
    are engine-writable and therefore excluded from the human denylist even though
    they appear in ``required_fields`` (the regression harness still checks they
    exist on the live table). Sourcing this from the contract keeps the derived-
    vs-human split single-homed; the module's :data:`DERIVED_CARD_FIELDS` mirror
    is asserted equal at first use (drift guard in :func:`_human_denylist`).

    Chase Cards also has a ``derived_fields`` key in the contract (the same nine
    columns as Inventory — verified against the live base). The chase guard
    (:func:`_chase_human_denylist`) passes an explicit ``derived_override`` for
    the denylist derivation and asserts the contract's chase ``derived_fields``
    equals :data:`CHASE_DERIVED_CARD_FIELDS` (drift guard), so the chase split is
    single-homed in the contract too.
    """
    contract = json.loads((path or _golden_contract_path()).read_text(encoding='utf-8'))
    tbl_meta = contract.get('tables', {}).get(table_name, {})
    return frozenset(tbl_meta.get('derived_fields', []))


def load_human_denylist(
    path: Path | None = None,
    *,
    table_name: str = CARDS_TABLE_NAME,
    derived_override: frozenset[str] | None = None,
) -> frozenset[str]:
    """Load the DENYLIST of human/collection-owned field names for ``table_name``.

    The write-back targets ``table_name``, so the fields it must never touch are
    that table's human/collection-authoritative fields: the table
    ``primary_field`` plus every ``required_fields`` entry any skill declares for
    it (for Inventory Cards: Card Name, Sets, Number Owned, Sources, Condition,
    Number in Library, Decks, ...; for Chase Cards: Card Name, Target Decks, ...)
    — minus the card-dim / engine-derived Scryfall columns the engine owns and so
    are on the allowlist, not the denylist.

    The derived source is: ``derived_override`` when given (the chase caller
    passes :data:`CHASE_DERIVED_CARD_FIELDS` for Chase, whose contract entry has no
    ``derived_fields`` key), else the table's contract ``derived_fields``
    (:func:`load_contract_derived_fields`). Deriving the denylist here (rather than
    hard-coding) keeps it in lock-step with the contract: a new human field is
    automatically denied, and a field reclassified as derived is automatically
    allowed.
    """
    contract = json.loads((path or _golden_contract_path()).read_text(encoding='utf-8'))
    denied: set[str] = set()
    tbl_meta = contract.get('tables', {}).get(table_name, {})
    pf = tbl_meta.get('primary_field')
    if pf:
        denied.add(pf)
    for skill in contract.get('skills', {}).values():
        skill_tbl = skill.get('tables', {}).get(table_name, {})
        denied.update(skill_tbl.get('required_fields', []))
    # The engine-derived Scryfall columns are owned by the write-back
    # (they land on the allowlist), so they are not human-denied.
    derived = (
        derived_override if derived_override is not None else load_contract_derived_fields(path, table_name=table_name)
    )
    denied -= derived
    return frozenset(denied)


@lru_cache(maxsize=1)
def _human_denylist() -> frozenset[str]:
    """Return the human-field denylist, loaded lazily and cached on first use.

    Deliberately not loaded at import: importing this module must be
    side-effect-free (no filesystem read, no ``assert``) so the pipeline package
    is not coupled to the sibling ``regression/`` dir merely by import. The load
    (and the two fail-closed safety asserts below) run on first real use — i.e.
    when a write is planned/guarded — so a missing/empty/mis-keyed contract still
    fail-closes loudly, just at first use instead of at import time.

    Three fail-closed asserts, checked here on first use:
        1. Non-empty: an empty denylist would silently degrade the human-field
           guard to allowlist-only. That happens if the golden contract is missing
           or its Cards-table key does not match :data:`CARDS_TABLE_NAME`. Fail
           loud instead.
        2. Disjointness: the fields this module may write and the human fields it
           must never write must be disjoint; if an edit ever names an allowlist
           field after a human field, this fails loud.
        3. No drift: the contract's card-dim ``derived_fields`` must exactly match
           the module's :data:`DERIVED_CARD_FIELDS` mirror, and every one must be
           on the allowlist. If the contract and code drift (a field reclassified
           in one but not the other), the derived-vs-human partition is
           inconsistent — fail loud rather than silently mis-authorize a write.
    """
    denylist = load_human_denylist()
    assert denylist, (
        'FATAL: the human-field denylist is empty; regression/golden_contract.json is '
        f"missing or its Cards-table key doesn't match CARDS_TABLE_NAME={CARDS_TABLE_NAME!r}. "
        'The human-field write guard must never degrade to allowlist-only.'
    )
    overlap = ALLOWLIST_NAMES & denylist
    assert not overlap, (
        f'FATAL: derived allowlist overlaps the human denylist: {sorted(overlap)}. '
        'The write-back must never own a human-edited field name.'
    )
    contract_derived = load_contract_derived_fields()
    assert contract_derived == DERIVED_CARD_FIELDS, (
        'FATAL: the golden contract card-dim derived_fields '
        f'{sorted(contract_derived)} do not match the module DERIVED_CARD_FIELDS '
        f'{sorted(DERIVED_CARD_FIELDS)}. The derived-vs-human split must be single-homed; '
        'reconcile regression/golden_contract.json with destinations/airtable.py.'
    )
    unauthorized = DERIVED_CARD_FIELDS - ALLOWLIST_NAMES
    assert not unauthorized, (
        f'FATAL: contract-derived fields {sorted(unauthorized)} are not on the allowlist; '
        'every card-dim derived field must be writable.'
    )
    return denylist


@lru_cache(maxsize=1)
def _chase_human_denylist() -> frozenset[str]:
    """Return the Chase Cards human-field denylist, loaded lazily and cached.

    The analogue of :func:`_human_denylist` for the Chase Cards table: it
    derives the chase human/collection-owned fields (Card Name + Sets + Target
    Decks + any other chase ``required_fields``) from the golden contract, minus the
    nine chase derived columns (:data:`CHASE_DERIVED_CARD_FIELDS`), which the engine
    owns. The three fail-closed asserts (non-empty, disjointness, no-drift) are the
    same shape as the Inventory guard, bound to the Chase table — so this
    generalizes the guard to a second table without touching (or weakening) the
    Inventory guard above.
    """
    denylist = load_human_denylist(table_name=CHASE_TABLE_NAME, derived_override=CHASE_DERIVED_CARD_FIELDS)
    assert denylist, (
        'FATAL: the chase human-field denylist is empty; regression/golden_contract.json is '
        f"missing or its Chase-table key doesn't match CHASE_TABLE_NAME={CHASE_TABLE_NAME!r}. "
        'The chase human-field write guard must never degrade to allowlist-only.'
    )
    overlap = CHASE_ALLOWLIST_NAMES & denylist
    assert not overlap, (
        f'FATAL: chase derived allowlist overlaps the chase human denylist: {sorted(overlap)}. '
        'The chase write-back must never own a human-edited field name.'
    )
    # No drift: the contract's chase card-dim derived_fields must exactly match the
    # module's CHASE_DERIVED_CARD_FIELDS mirror, so the chase derived-vs-human split
    # is single-homed in the contract (same guarantee the Inventory guard enforces).
    contract_chase_derived = load_contract_derived_fields(table_name=CHASE_TABLE_NAME)
    assert contract_chase_derived == CHASE_DERIVED_CARD_FIELDS, (
        'FATAL: the golden contract chase derived_fields '
        f'{sorted(contract_chase_derived)} do not match the module CHASE_DERIVED_CARD_FIELDS '
        f'{sorted(CHASE_DERIVED_CARD_FIELDS)}. The chase derived-vs-human split must be '
        'single-homed; reconcile regression/golden_contract.json with destinations/airtable.py.'
    )
    unauthorized = CHASE_DERIVED_CARD_FIELDS - CHASE_ALLOWLIST_NAMES
    assert not unauthorized, (
        f'FATAL: chase derived fields {sorted(unauthorized)} are not on the chase allowlist; '
        'every chase derived field must be writable.'
    )
    return denylist


def _assert_within_binding(payload_fields: Any, *, allowlist: frozenset[str], denylist: frozenset[str]) -> None:
    """Parameterized guard core: refuse fields outside ``allowlist`` / inside ``denylist``.

    The shared enforcement both the Inventory guard (:func:`assert_no_human_fields`)
    and the chase guard (:func:`assert_no_chase_human_fields`) delegate to, so the
    two tables share one implementation and the chase generalization cannot drift
    from — or weaken — the Inventory one. Two independent checks so the failure mode
    is explicit:
        1. intersection with ``denylist`` must be empty (the core proof);
        2. every field must be in ``allowlist`` (closed-set defense).
    """
    fields = set(payload_fields)
    human = fields & denylist
    if human:
        raise HumanFieldWriteError(
            f'REFUSED: write payload contains human-edited field(s) {sorted(human)}. '
            f'This adapter may only write the derived allowlist {sorted(allowlist)}; '
            'human fields are pull-only.'
        )
    non_allowed = fields - allowlist
    if non_allowed:
        raise NonAllowlistFieldError(
            f'REFUSED: write payload contains non-allowlisted field(s) '
            f'{sorted(non_allowed)}. Only {sorted(allowlist)} may be written.'
        )


def assert_no_human_fields(payload_fields: Any) -> None:
    """Guard (Inventory Cards): refuse any payload that touches a human (denylisted)
    field, or any field outside the allowlist. Called before every request is built.

    A thin Inventory-bound wrapper over :func:`_assert_within_binding` (allowlist =
    :data:`ALLOWLIST_NAMES`, denylist = :func:`_human_denylist`).
    """
    _assert_within_binding(payload_fields, allowlist=ALLOWLIST_NAMES, denylist=_human_denylist())


def assert_no_chase_human_fields(payload_fields: Any) -> None:
    """Guard (Chase Cards): the chase analogue of :func:`assert_no_human_fields`.

    Bound to the chase allowlist (:data:`CHASE_ALLOWLIST_NAMES`) + chase denylist
    (:func:`_chase_human_denylist`), so a chase human field (Card Name / Target
    Decks / …) fails closed and only the eleven chase derived columns (nine Scryfall
    + ⚙ Buckets + ⚙ Otags) may be written.
    """
    _assert_within_binding(payload_fields, allowlist=CHASE_ALLOWLIST_NAMES, denylist=_chase_human_denylist())


# --------------------------------------------------------------------------- #
# Card resolution + payload construction.
# --------------------------------------------------------------------------- #

#: A trailing printing annotation on a card name, e.g. the "(Borderless)" in
#: "Parallel Lives (Borderless)" or "(Retro)" in "Sol Ring (Retro)". Real
#: Airtable/cardlist names carry these cosmetic suffixes, but Scryfall oracle
#: names do not — so an exact match against ``name_to_oracle_id`` misses. We
#: strip a single trailing parenthetical (and surrounding whitespace) as a
#: fallback before giving up.
_PRINTING_ANNOTATION = re.compile(r'\s*\([^()]*\)\s*$')


def _strip_printing_annotation(name: str) -> str:
    """Strip a trailing parenthetical printing annotation from a card name.

    ``"Parallel Lives (Borderless)"`` -> ``"Parallel Lives"``. Leaves a name with
    no trailing parenthetical untouched. Only the last parenthetical is removed,
    so a name like ``"Fire // Ice"`` or an interior paren is preserved.
    """
    return _PRINTING_ANNOTATION.sub('', name).strip()


def _resolve_oracle_id(name: str, name_to_oracle_id: dict[str, str]) -> str | None:
    """Resolve a card ``name`` to its ``oracle_id``, tolerating printing suffixes.

    Tries an exact match first (the common case), then falls back to matching the
    name with a trailing printing annotation stripped (e.g. "(Borderless)",
    "(Retro)"). Returns ``None`` if still unmatched — the caller keeps its existing
    loud-skip behavior for genuinely unresolvable names.
    """
    oid = name_to_oracle_id.get(name)
    if oid:
        return oid
    stripped = _strip_printing_annotation(name)
    if stripped != name:
        return name_to_oracle_id.get(stripped)
    return None


@dataclass(frozen=True)
class CardResolution:
    """A single Airtable Card record joined to its derived otag facts.

    ``record_id`` is the durable Airtable record id (the upsert key). ``oracle_id``
    is the resolved Scryfall key (``None`` if the card name did not robustly
    resolve — such rows are skipped, never guessed). ``buckets`` and ``otags`` are
    the derived per-card values (the crosswalked functional buckets and the raw
    slug closure).
    """

    record_id: str
    oracle_id: str | None
    buckets: tuple[str, ...]
    otags: tuple[str, ...]


def resolve_cards(
    cards: dict[str, str],
    *,
    name_to_oracle_id: dict[str, str],
    card_otag: dict[str, set[str]],
) -> tuple[list[CardResolution], list[str]]:
    """Join pulled Airtable Cards to their per-card otag facts (offline, pure).

    Args:
        cards: ``{airtable_record_id: card_name}`` — the pulled Cards records.
        name_to_oracle_id: ``{card_name: oracle_id}`` — the durable-key resolver
            (built from the Scryfall oracle-card table; card name -> oracle_id).
        card_otag: ``{oracle_id: {slug}}`` — the rolled-up slug closure per card
            (from ``otag_rollup``).

    Returns:
        ``(resolved, skipped)`` where ``resolved`` is the list of
        :class:`CardResolution` for cards with a robust ``oracle_id`` join AND a
        non-empty derived payload, and ``skipped`` is the list of record ids that
        were dropped (no record id / no name / unresolvable oracle_id / no otag
        data) — logged, never guessed.
    """
    resolved: list[CardResolution] = []
    skipped: list[str] = []
    for record_id in sorted(cards):
        name = cards[record_id]
        if not record_id:
            log.warning('Skipping a Card with no Airtable record id.')
            skipped.append(record_id)
            continue
        oid = _resolve_oracle_id(name, name_to_oracle_id)
        if not oid:
            log.warning(
                'SKIP Card %s (%r): no robust oracle_id match — not guessing.',
                record_id,
                name,
            )
            skipped.append(record_id)
            continue
        slugs = card_otag.get(str(oid))
        if not slugs:
            log.warning(
                'SKIP Card %s (%r, oracle_id=%s): no otag data — nothing to write.',
                record_id,
                name,
                oid,
            )
            skipped.append(record_id)
            continue
        buckets = tuple(sorted(buckets_for(set(slugs))))
        otags = tuple(sorted(str(s) for s in slugs))
        resolved.append(CardResolution(record_id=record_id, oracle_id=str(oid), buckets=buckets, otags=otags))
    return resolved, skipped


def build_payload(resolution: CardResolution) -> dict[str, Any]:
    """Build the derived-field write payload for ONE resolved card.

    Emits exactly the two allowlisted fields: ``⚙ Buckets`` (a list of bucket
    names for the multipleSelects field) and ``⚙ Otags`` (the raw slugs joined
    into multiline text). There is no branch that copies an arbitrary key from
    the card, so a human field cannot appear structurally. The guard is then
    re-run as defense-in-depth. Idempotent: same input -> identical payload.
    """
    payload: dict[str, Any] = {
        BUCKETS_FIELD: list(resolution.buckets),
        OTAGS_FIELD: '\n'.join(resolution.otags),
    }
    assert_no_human_fields(payload.keys())  # defense-in-depth
    return payload


@dataclass
class CardWrite:
    """One intended upsert: a Cards record id + its derived-field payload."""

    record_id: str
    fields: dict[str, Any]


# --------------------------------------------------------------------------- #
# Card-dim derived-column payload: map a resolved Card + live price onto
# the nine already-existing Scryfall-derived Inventory Cards columns.
# --------------------------------------------------------------------------- #

#: The mapping from an Airtable derived column name to how it is sourced from a
#: resolved ``contracts.Card`` (+ the live price, injected separately — price is
#: not on the Card contract). This is the single place the projection lives, so a
#: schema drift surfaces here. Every key is in :data:`DERIVED_CARD_FIELDS` (the
#: builder asserts that), so the payload can never carry a non-derived field.


def _power_toughness(card: Any) -> str:
    """Render ``Power / Toughness`` as ``"P/T"`` for creatures, else empty.

    A non-creature (no power and no toughness) yields ``''`` rather than ``'/'``
    so the derived column is blanked, matching the human-facing convention.
    """
    power = card.power
    toughness = card.toughness
    if power is None and toughness is None:
        return ''
    return f'{power or ""}/{toughness or ""}'


def _coerce_price(price_usd: str | None) -> float | None:
    """Coerce the live Scryfall price to a float for the Airtable currency column.

    Scryfall serves ``prices.usd`` as a string (e.g. ``'3.94'``), but the Airtable
    ``Price (TCGPlayer)`` field is a ``currency`` (number) type that rejects a
    string with a 422. Return a float, or ``None`` (clears the cell) for a missing
    or non-numeric price — fail-open, never raise.
    """
    if price_usd is None:
        return None
    try:
        return float(price_usd)
    except (TypeError, ValueError):
        return None


#: Scryfall serves color identity as single letters; the Airtable ``Color Identity``
#: ``multipleSelects`` column uses full color names (verified against the live base:
#: existing rows are ``["Black"]``, ``["Green","Red"]``, …). Writing a single letter
#: that is not an existing option 422s (the write does not ``typecast``), so map to
#: the canonical names both the Inventory and Chase tables carry. Colorless -> ``[]``.
_COLOR_IDENTITY_NAMES = {'W': 'White', 'U': 'Blue', 'B': 'Black', 'R': 'Red', 'G': 'Green', 'C': 'Colorless'}


def _color_identity(card: Any) -> list[str]:
    """Map the Card's single-letter color identity to the Airtable option names."""
    return [str(_COLOR_IDENTITY_NAMES.get(c, c)) for c in (card.color_identity or [])]


def build_derived_card_payload(card: Any, price_usd: str | None) -> dict[str, Any]:
    """Build the nine-field derived write payload for one resolved card.

    Sources the enrichment from the lake ``contracts.Card`` and the volatile price
    from the live ``price_usd`` argument (never the Card — price is not a Card
    field). Emits exactly the keys in :data:`DERIVED_CARD_FIELDS`; there is no
    branch that copies an arbitrary field name, so a human field cannot appear
    structurally. The guard (:func:`assert_no_human_fields`) is re-run as
    defense-in-depth. Idempotent: same Card + price -> identical payload.

    Values are coerced to the Airtable column types (verified against the live
    base): ``Price (TCGPlayer)`` -> float (currency), ``Color Identity`` -> full
    color names (``multipleSelects``). A string price or single-letter color
    would 422 the real PATCH (the mock transport does not type-check).
    """
    payload: dict[str, Any] = {
        'Card Type': card.type_line or '',
        'Mana Cost': card.mana_cost or '',
        'CMC': card.mana_value if card.mana_value is not None else 0,
        'Power / Toughness': _power_toughness(card),
        'Oracle Text': card.oracle_text or '',
        'Card Art': card.art_crop or '',
        'Scryfall URL': card.scryfall_uri or '',
        'Price (TCGPlayer)': _coerce_price(price_usd),
        'Color Identity': _color_identity(card),
    }
    # Structural invariant: the emitted keys are exactly the closed derived set.
    assert set(payload) == DERIVED_CARD_FIELDS, (
        f'derived payload keys {sorted(payload)} != DERIVED_CARD_FIELDS {sorted(DERIVED_CARD_FIELDS)}'
    )
    assert_no_human_fields(payload.keys())  # defense-in-depth
    return payload


def build_chase_derived_card_payload(card: Any, price_usd: str | None) -> dict[str, Any]:
    """Build the eleven-field chase derived write payload for one resolved card.

    The Chase Cards table carries the same eleven engine-derived columns as
    Inventory: the nine Scryfall-pure columns (including Price (TCGPlayer), sourced
    live) plus the two engine ⚙ otag fields (⚙ Buckets / ⚙ Otags). So this payload
    is:

        (1) the nine Scryfall columns from :func:`build_derived_card_payload`, plus
        (2) the two ⚙ fields, formatted with the same logic :func:`build_payload`
            uses for the Inventory otag sync — ``⚙ Buckets`` as the multipleSelects
            list of bucket names (from ``card.otag_buckets``), ``⚙ Otags`` as the raw
            slugs newline-joined into multilineText (from ``card.otags``).

    Asymmetry (deliberate): the owned inline write (:func:`build_derived_card_payload`)
    stays nine — Inventory's ⚙ fields are populated by the separate otag sync path.
    Chase has no otag sync, so this inline write is chase's only path to ⚙, which is
    why it emits eleven. A card with empty ``otag_buckets`` / ``otags`` writes empty ⚙
    (an empty list / empty string) — fail-open and valid, never a crash.

    Emits exactly the keys in :data:`CHASE_DERIVED_CARD_FIELDS`; there is no branch
    that copies an arbitrary field name, so a human field cannot appear structurally.
    The chase guard (:func:`assert_no_chase_human_fields`) is re-run as
    defense-in-depth. Idempotent: same Card + price -> identical payload.
    """
    payload = build_derived_card_payload(card, price_usd)
    # The two ⚙ otag fields, formatted exactly as the Inventory otag sync
    # (:func:`build_payload`) does: buckets -> multipleSelects list, otags ->
    # newline-joined multilineText. Sourced from the resolver Card.
    payload[BUCKETS_FIELD] = list(card.otag_buckets or [])
    payload[OTAGS_FIELD] = '\n'.join(card.otags or [])
    # Structural invariant: the emitted keys are exactly the closed chase derived set.
    assert set(payload) == CHASE_DERIVED_CARD_FIELDS, (
        f'chase derived payload keys {sorted(payload)} != CHASE_DERIVED_CARD_FIELDS {sorted(CHASE_DERIVED_CARD_FIELDS)}'
    )
    assert_no_chase_human_fields(payload.keys())  # defense-in-depth (chase guard)
    return payload


@dataclass
class DerivedWriteReport:
    """Result of a (dry-run, applied, or local-no-op) derived-column write."""

    backend: str
    dry_run: bool
    applied: bool
    planned: list[CardWrite] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    write_requests_issued: int = 0
    chunks_planned: int = 0
    #: True when the backend is not airtable (local mode): the primitive is a
    #: no-op and issued zero Airtable calls. Never set on an airtable run.
    skipped_no_airtable: bool = False

    def render(self) -> str:
        lines: list[str] = []
        mode = 'DRY-RUN (no writes issued)' if self.dry_run else 'APPLY'
        lines.append(f'=== Airtable card-dim DERIVED-column write-back -> Cards [{mode}] ===')
        lines.append(f'backend: {self.backend}')
        if self.skipped_no_airtable:
            lines.append(
                'Backend is not `airtable` (local mode): NO Airtable calls issued. '
                'The lake is canonical; derived columns are only synced when '
                'MAKE_MAGIC_BACKEND=airtable.'
            )
            return '\n'.join(lines)
        lines.append(f'Derived columns: {sorted(DERIVED_CARD_FIELDS)}')
        for w in self.planned:
            lines.append(f'  upsert Cards/{w.record_id}:')
            for name in sorted(w.fields):
                val = w.fields[name]
                shown = val if len(str(val)) <= 80 else f'{str(val)[:77]}...'
                lines.append(f'    {name} = {shown!r}')
        lines.append(f'cards resolved: {len(self.planned)}  |  skipped (no resolve): {len(self.skipped)}')
        verb = 'would send' if self.dry_run else 'sent'
        lines.append(f'PATCH chunks {verb} (<= {MAX_RECORDS_PER_PATCH} records each): {self.chunks_planned}')
        lines.append(f'write requests issued: {self.write_requests_issued}')
        return '\n'.join(lines)


def plan_writes(
    cards: dict[str, str],
    *,
    name_to_oracle_id: dict[str, str],
    card_otag: dict[str, set[str]],
) -> list[CardWrite]:
    """Turn pulled Cards into a deterministic write plan (idempotent).

    Resolves each card to its ``oracle_id`` and derived facts, skipping any card
    without a robust match (:func:`resolve_cards`), then builds one upsert per
    resolved card keyed on the Airtable Card record id — so re-running with the
    same input yields the same plan (no new records, no dupes).
    """
    resolved, _skipped = resolve_cards(cards, name_to_oracle_id=name_to_oracle_id, card_otag=card_otag)
    return [CardWrite(record_id=r.record_id, fields=build_payload(r)) for r in resolved]


def _chunk[T](records: list[T], size: int = MAX_RECORDS_PER_PATCH) -> list[list[T]]:
    """Split ``records`` into batches of at most ``size`` (Airtable's PATCH cap).

    Deterministic and order-preserving so a re-run produces identical chunks (the
    upsert stays idempotent). E.g. 25 records -> [10, 10, 5].
    """
    if size < 1:
        raise ValueError('chunk size must be >= 1')
    return [records[i : i + size] for i in range(0, len(records), size)]


def _chunk_count(n: int, size: int = MAX_RECORDS_PER_PATCH) -> int:
    """Number of PATCH chunks ``n`` records would require (0 for an empty plan)."""
    return math.ceil(n / size) if n else 0


# --------------------------------------------------------------------------- #
# Guarded write client (structural choke point).
# --------------------------------------------------------------------------- #


class SupportsWrite(Protocol):
    """Narrow port a writer needs: field-schema read/create + record patch."""

    def list_fields(self) -> dict[str, str]: ...
    def create_field(self, name: str, airtable_type: str, options: dict[str, Any] | None) -> None: ...
    def patch_records(self, records: list[dict[str, Any]]) -> httpx.Response: ...


class AllowlistWriteClient:
    """httpx wrapper that permits only allowlisted-field writes to Cards.

    Every mutating body is routed through :meth:`_guarded_body`, which re-runs
    :func:`assert_no_human_fields` on the exact fields about to be sent. The
    httpx client is name-mangled (private), which deters accidental use of a raw
    request that skips the guard (not a hard security boundary). Uses
    ``use_field_ids=True`` by resolving field
    names to ids via the meta schema before patching.
    """

    def __init__(
        self,
        token: str,
        *,
        base_id: str | None = None,
        cards_table_name: str | None = None,
        allowlist: frozenset[str] | None = None,
        denylist_fn: Any | None = None,
        guard: Any | None = None,
        probe_fields: frozenset[str] | None = None,
        _client: httpx.Client | None = None,
    ) -> None:
        self.__client = _client or httpx.Client(timeout=30)
        self.__auth = {'Authorization': f'Bearer {token}', **HEADERS_UA}
        #: Env-driven identity. Base id + Cards table name come from Settings
        #: (overridable per instance); the Cards table ``tbl…`` id is discovered
        #: from the meta schema by name in :meth:`list_fields` (never hard-coded).
        settings = get_settings()
        self._base_id = base_id or settings.airtable_base_id
        self._cards_table_name = cards_table_name or settings.cards_table
        #: The table binding. Defaults to the Inventory Cards binding
        #: (allowlist = :data:`ALLOWLIST_NAMES`, denylist = the Inventory human
        #: denylist, guard = :func:`assert_no_human_fields`). The chase inline write
        #: passes the chase allowlist / denylist / guard so the same machinery binds
        #: to a second table without weakening the Inventory guard.
        self._allowlist = allowlist if allowlist is not None else ALLOWLIST_NAMES
        self._denylist_fn = denylist_fn if denylist_fn is not None else _human_denylist
        self._guard = guard if guard is not None else assert_no_human_fields
        #: The wrong-table probe set — the human fields whose presence proves the
        #: resolved table is the intended write-target (see :meth:`list_fields`).
        #: Separate from the write denylist because the two answer different
        #: questions: the denylist is the closed set of fields that must never be
        #: written (kept full — no weakening), while the probe is the set the guard
        #: requires to be present to accept the table. For Inventory they coincide
        #: (``probe_fields=None`` -> the full Inventory denylist). For Chase the
        #: contract's denylist is a superset of the live table's fields (the
        #: chasing-cards skill declares an aspirational fuller schema than the live
        #: eleven-derived + Card Name/Target Decks table), so probing with the full
        #: denylist would wrongly reject the real Chase table; the chase caller
        #: passes a tight, live-accurate probe (Card Name + Target Decks) that still
        #: rejects a genuinely-wrong table (Decks lacks 'Card Name'). The write
        #: denylist stays full, so writing any of the wider chase human fields is
        #: still refused — the guard is generalized, not loosened.
        self._probe_fields = probe_fields
        #: Resolved lazily from the meta schema on the first :meth:`list_fields`.
        self._cards_table_id: str | None = None
        #: id-keyed wire guard state, derived from the resolved allowlist
        #: name->id map. Empty until :meth:`resolve_field_map` (or
        #: :meth:`list_fields`) runs — so before resolution no id can pass the
        #: wire guard (fail-closed).
        self._allowed_ids: frozenset[str] = frozenset()
        self._id_to_name: dict[str, str] = {}

    # --- schema (meta API) -------------------------------------------------- #

    def resolve_field_map(self, name_to_id: dict[str, str]) -> None:
        """Derive the id-keyed wire-guard state from a name->id schema map.

        By the wire the payload is keyed by Airtable field ids, but the allowlist
        is a set of names. So we translate: ``allowed_ids`` = the ids of the two
        allowlisted derived fields (``⚙ Buckets`` / ``⚙ Otags``), and
        ``id_to_name`` = the reverse map restricted to those allowed ids. Human
        field ids (and unknown ids) are deliberately excluded, so the wire guard
        can reject anything that is not one of the two derived ids.
        """
        self._allowed_ids = frozenset(fid for name, fid in name_to_id.items() if name in self._allowlist)
        self._id_to_name = {fid: name for name, fid in name_to_id.items() if name in self._allowlist}

    def list_fields(self) -> dict[str, str]:
        """Return ``{field_name: field_id}`` for the Cards table (meta API GET).

        The Cards table is located by its env-driven name (:attr:`_cards_table_name`),
        not a hard-coded ``tbl…`` id; its id is cached in :attr:`_cards_table_id`
        for the subsequent record PATCH/field-create URLs. Also caches the
        id-keyed wire-guard state (:meth:`resolve_field_map`) so the wire guard can
        validate the field-ID-keyed body it is about to send. Raises
        :class:`AirtableConfigError` if the named table is absent from the base.
        """
        url = f'{META_ROOT}/bases/{self._base_id}/tables'
        resp = self.__client.get(url, headers=self.__auth)
        resp.raise_for_status()
        for table in resp.json().get('tables', []):
            if table.get('name') == self._cards_table_name:
                name_to_id = {f['name']: f['id'] for f in table.get('fields', [])}
                # Wrong-table write guard: bind the derived write to the real target
                # table structurally. A misconfigured/hostile table name (e.g.
                # '=Decks') could resolve a table by name that is not the intended
                # one; the write would then PATCH derived values onto its records.
                # Require that the resolved table contains this binding's human
                # fields (its denylist is a subset of the table's field names) —
                # e.g. Decks lacks 'Card Name'/'Number Owned'/… (Inventory binding)
                # or 'Target Decks' (chase binding) so it is refused before any
                # create/PATCH. The probe defaults to the full write denylist for the
                # Inventory guard; a caller whose live schema is a subset of its
                # contract denylist (Chase) passes a tight, live-accurate probe
                # (Card Name + Target Decks) via ``probe_fields``. The write denylist
                # stays full regardless, so this is an additional binding; it does
                # not loosen any allowlist/denylist/wire guard.
                probe = self._probe_fields if self._probe_fields is not None else self._denylist_fn()
                missing_card_fields = probe - set(name_to_id)
                if missing_card_fields:
                    raise AirtableConfigError(
                        f'resolved table {self._cards_table_name!r} (id {table.get("id")!r}) is not the '
                        f'inventory Cards table — it is missing the expected fields '
                        f'{sorted(missing_card_fields)}; check the configured table name.'
                    )
                self._cards_table_id = table.get('id')
                self.resolve_field_map(name_to_id)
                return name_to_id
        raise AirtableConfigError(
            f'table {self._cards_table_name!r} not found in base {self._base_id!r}; '
            'set AIRTABLE_CARDS_TABLE (and AIRTABLE_BASE_ID) for this instance.'
        )

    def create_field(self, name: str, airtable_type: str, options: dict[str, Any] | None) -> None:
        """Create a derived field on Cards. Refuses any non-allowlisted name."""
        if name not in self._allowlist:
            raise NonAllowlistFieldError(f'REFUSED: cannot create field {name!r} — not in the derived allowlist.')
        self._guard([name])
        table_id = self._require_cards_table_id()
        url = f'{META_ROOT}/bases/{self._base_id}/tables/{table_id}/fields'
        body: dict[str, Any] = {'name': name, 'type': airtable_type}
        if options:
            body['options'] = options
        resp = self.__client.post(url, headers=self.__auth, json=body)
        resp.raise_for_status()

    def _require_cards_table_id(self) -> str:
        """Return the resolved Cards table id, resolving it first if needed.

        The id is discovered from the meta schema by name (:meth:`list_fields`);
        this triggers that resolution on demand so create/patch URLs are never
        built from a hard-coded id.
        """
        if self._cards_table_id is None:
            self.list_fields()
        assert self._cards_table_id is not None
        return self._cards_table_id

    # --- records ------------------------------------------------------------ #

    def _guarded_body(self, records: list[dict[str, Any]]) -> None:
        """Re-run the guard on the exact field ids in every record about to be sent.

        By the wire the payload is keyed by Airtable **field ids**, not names, so
        the name-level allowlist cannot be applied directly. Instead we enforce
        the same guarantee in id-space: every field id must be one of the two
        derived ids in :attr:`_allowed_ids` (built from the resolved allowlist
        name->id map). Anything else — a human field id, or an id with no mapping
        — is rejected. As a belt-and-suspenders proof we also translate the
        allowed ids back to their names via :attr:`_id_to_name` and re-run the
        name-level :func:`assert_no_human_fields` on them, so the two guards agree.

        Fail-closed: if the field map has not been resolved yet, ``_allowed_ids``
        is empty and no id can pass.
        """
        for rec in records:
            field_ids = set((rec.get('fields') or {}).keys())
            unknown = field_ids - self._allowed_ids
            if unknown:
                raise NonAllowlistFieldError(
                    f'REFUSED: write body contains non-allowlisted field id(s) '
                    f'{sorted(unknown)}. Only the derived field ids '
                    f'{sorted(self._allowed_ids)} (== {sorted(self._allowlist)}) '
                    'may be written; a human field id or unknown id is refused.'
                )
            # Belt-and-suspenders: the mapped names must ALSO pass the name guard.
            self._guard(self._id_to_name[fid] for fid in field_ids)

    def patch_records(self, records: list[dict[str, Any]]) -> httpx.Response:
        """PATCH derived fields onto existing Cards records (upsert by id).

        ``records`` are ``{"id": rec, "fields": {field_id: value}}`` already keyed
        by field ID (``use_field_ids=True``). The guard runs on the raw field
        keys one final time before the request leaves the process.
        """
        # The guard runs before id-resolution too (see sync), but re-check here on
        # whatever is actually about to be transmitted — the true choke point.
        self._guarded_body(records)
        table_id = self._require_cards_table_id()
        url = f'{API_ROOT}/{self._base_id}/{table_id}'
        body = {'records': records, 'returnFieldsByFieldId': True}
        resp = self.__client.patch(url, headers=self.__auth, json=body)
        resp.raise_for_status()
        return resp

    def close(self) -> None:
        self.__client.close()


# --------------------------------------------------------------------------- #
# Diff / dry-run reporting
# --------------------------------------------------------------------------- #


@dataclass
class PushReport:
    """Result of a (dry-run or applied) sync."""

    dry_run: bool
    applied: bool
    planned: list[CardWrite] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    fields_to_create: list[str] = field(default_factory=list)
    write_requests_issued: int = 0
    #: Number of PATCH chunks the plan maps to (Airtable's 10-records-per-request
    #: cap). In a dry-run this is the count it would send; in an applied run it
    #: equals ``write_requests_issued`` on success.
    chunks_planned: int = 0
    #: True when the plan could not be computed because there was no Airtable API
    #: access (offline dry-run: no credential -> no meta schema, so the
    #: field-id-keyed lake can't be read and field existence can't be checked). In
    #: that state the resolved-count / would-create claims are meaningless, so we
    #: report "unknown" rather than a misleading 0 / bogus creation. Never set on an
    #: applied run (a write always has a credential).
    plan_unknown_no_api: bool = False

    def render(self) -> str:
        lines: list[str] = []
        mode = 'DRY-RUN (no writes issued)' if self.dry_run else 'APPLY'
        lines.append(f'=== Airtable derived write-back -> Cards [{mode}] ===')
        lines.append(f'Allowlist (engine-owned): {sorted(ALLOWLIST_NAMES)}')
        if self.plan_unknown_no_api:
            lines.append(
                'Plan: unknown (no API access; set AIRTABLE_API_KEY for an accurate dry-run). '
                'The Card Name field id and live schema cannot be resolved offline, so the '
                'field-ID-keyed lake cannot be read and field creation cannot be assessed — '
                'no cards resolved and no would-create claim are reported.'
            )
            return '\n'.join(lines)
        if self.fields_to_create:
            lines.append(f'Fields that WOULD be created: {self.fields_to_create}')
        for w in self.planned:
            lines.append(f'  upsert Cards/{w.record_id}:')
            for name in sorted(w.fields):
                val = w.fields[name]
                shown = val if len(str(val)) <= 80 else f'{str(val)[:77]}...'
                lines.append(f'    {name} = {shown!r}')
        lines.append(f'cards resolved: {len(self.planned)}  |  skipped (no match): {len(self.skipped)}')
        verb = 'would send' if self.dry_run else 'sent'
        lines.append(f'PATCH chunks {verb} (<= {MAX_RECORDS_PER_PATCH} records each): {self.chunks_planned}')
        lines.append(f'write requests issued: {self.write_requests_issued}')
        return '\n'.join(lines)


# --------------------------------------------------------------------------- #
# ensure_fields + sync (public API)
# --------------------------------------------------------------------------- #


def ensure_fields(
    client: SupportsWrite,
    *,
    dry_run: bool = True,
    apply: bool = False,
) -> list[str]:
    """Ensure every derived field exists on Cards; create the missing ones.

    Schema mutation is allowed for derived fields only (each create call refuses
    a non-allowlisted name). ``⚙ Buckets`` is created as a multipleSelects seeded
    with the full crosswalk vocabulary (Airtable requires the options to exist
    before any can be written); ``⚙ Otags`` as multilineText. Dry-run by default:
    returns the names it would create without issuing any request unless
    ``dry_run=False and apply=True``.
    """
    existing = set(client.list_fields())
    missing = [f for f in DERIVED_FIELDS if f.name not in existing]
    names = [f.name for f in missing]
    if dry_run or not apply:
        log.info('[dry-run] would create derived fields: %s', names)
        return names
    for f in missing:
        client.create_field(f.name, f.airtable_type, f.options)
    return names


def sync(
    cards: dict[str, str],
    *,
    name_to_oracle_id: dict[str, str],
    card_otag: dict[str, set[str]],
    client: SupportsWrite | None = None,
    dry_run: bool = True,
    apply: bool = False,
    field_names_present: frozenset[str] | None = None,
) -> PushReport:
    """Sync per-card derived otag facts onto Cards records. Dry-run by default.

    Args:
        cards: ``{airtable_record_id: card_name}`` — the pulled Cards records.
        name_to_oracle_id: ``{card_name: oracle_id}`` durable-key resolver.
        card_otag: ``{oracle_id: {slug}}`` rolled-up slug closure per card.
        client: an :class:`AllowlistWriteClient` (or test double). Only touched
            when a real (``dry_run=False and apply=True``) run is requested; a
            dry-run needs no client and issues no requests.
        dry_run: when True (default) no write request is issued; the intended
            diff is computed and returned.
        apply: a real write requires both ``dry_run=False`` and ``apply=True``.
        field_names_present: the live Cards field names (from a read-only meta
            schema read). When provided in a dry-run, ``fields_to_create`` is the
            real set of derived fields that don't yet exist (accurate creation
            preview); when ``None`` in a dry-run, the plan is unknown — no schema
            was resolvable (no API access) — and no cards/creation are claimed.

    Returns:
        A :class:`PushReport`. In dry-run, ``write_requests_issued == 0``. Cards
        without a robust ``oracle_id`` match are in ``report.skipped``.
    """
    resolved, skipped = resolve_cards(cards, name_to_oracle_id=name_to_oracle_id, card_otag=card_otag)
    plan = [CardWrite(record_id=r.record_id, fields=build_payload(r)) for r in resolved]
    # Guard every planned payload up front — before any request is even eligible.
    for w in plan:
        assert_no_human_fields(w.fields.keys())

    do_write = (not dry_run) and apply
    report = PushReport(dry_run=not do_write, applied=do_write, planned=plan, skipped=skipped)
    # How many PATCH chunks this plan maps to (Airtable's 10-records-per-request
    # cap). Reported in both modes: in dry-run it is what we would send.
    report.chunks_planned = _chunk_count(len(plan))

    if not do_write:
        if field_names_present is None:
            # No live schema was resolvable (offline dry-run, no credential): the
            # field-id-keyed lake couldn't be read and field existence can't be
            # checked, so any resolved-count / would-create claim is meaningless.
            # Report unknown rather than a misleading vacuous 0 / bogus creation.
            report.plan_unknown_no_api = True
            return report
        # Accurate creation preview: only the derived fields that don't yet exist.
        report.fields_to_create = [f.name for f in DERIVED_FIELDS if f.name not in field_names_present]
        return report

    if client is None:
        raise ValueError('A live sync (dry_run=False, apply=True) requires a client.')

    # Schema first: create any missing engine ⚙ fields, then resolve name->id.
    ensure_fields(client, dry_run=False, apply=True)
    name_to_id = client.list_fields()
    # Only the field names this plan actually writes need a resolved id. This otag
    # sync emits just the two ⚙ fields (build_payload); the wider card-dim derived
    # Scryfall columns on the allowlist are written by the dual-write path and
    # need not resolve here. Requiring ids for the whole allowlist would wrongly
    # fail on a base that (correctly) lacks a ⚙ field only until ensure creates it,
    # or on the never-created Scryfall columns in an otag-only run.
    written_names = {n for w in plan for n in w.fields}
    missing_ids = written_names - set(name_to_id)
    if missing_ids:
        raise RuntimeError(f'Derived fields missing ids after ensure: {sorted(missing_ids)}')

    # Chunk to Airtable's 10-records-per-PATCH cap. We chunk the name-keyed plan
    # so the human-field guard runs on real field names for each chunk before it
    # leaves the process (the allowlist is a set of names, not field ids); only
    # then do we re-key that chunk by field id (use_field_ids=True) for the wire.
    # Sleep between chunks to respect the ~5 req/s rate limit, and record how many
    # chunks succeeded. On a chunk error, stop (do not silently continue) and
    # surface partial progress via ChunkWriteError so an idempotent re-run (upsert
    # by record id) can resume safely.
    plan_chunks = _chunk(plan)
    for i, plan_chunk in enumerate(plan_chunks):
        # Politeness delay before every chunk after the first (never before the
        # first, so a single-chunk apply pays no latency).
        if i > 0 and INTER_CHUNK_DELAY_S > 0:
            time.sleep(INTER_CHUNK_DELAY_S)
        # Guard this chunk's payloads (field names) before it is transmitted.
        chunk_records: list[dict[str, Any]] = []
        for w in plan_chunk:
            assert_no_human_fields(w.fields.keys())
            chunk_records.append(
                {
                    'id': w.record_id,
                    'fields': {name_to_id[n]: v for n, v in w.fields.items()},
                }
            )
        try:
            client.patch_records(chunk_records)
        except Exception as exc:
            log.error(
                'PATCH chunk %d/%d failed (%d chunk(s) completed): %s. '
                'Stopping; re-run to resume (idempotent upsert by record id).',
                i + 1,
                len(plan_chunks),
                report.write_requests_issued,
                exc,
            )
            raise ChunkWriteError(
                chunks_completed=report.write_requests_issued,
                chunks_planned=len(plan_chunks),
            ) from exc
        report.write_requests_issued += 1
    return report


# --------------------------------------------------------------------------- #
# Card-dim derived-column write primitive + explicit refresh.
# --------------------------------------------------------------------------- #


class _SupportsGetCard(Protocol):
    """The CardResolver seam the primitive needs: name -> enriched Card | None."""

    def get_card(self, name: str) -> Any | None: ...


def write_derived_fields(
    cards: dict[str, str],
    *,
    resolver: _SupportsGetCard,
    price_fetcher: Any,
    client: SupportsWrite | None = None,
    apply: bool = False,
    dry_run: bool = True,
) -> DerivedWriteReport:
    """Guarded write of the nine Scryfall-derived columns onto Cards. Dry-run default.

    The primitive both derived-write parts share. For each pulled Card it sources
    the nine derived columns from the lake ``resolver.get_card`` + a live price
    (``price_fetcher(name)`` — price is not on the Card contract), maps them to the
    exact Airtable field names via :func:`build_derived_card_payload`, and writes
    them through the same allowlist guard (:func:`assert_no_human_fields` +
    :meth:`AllowlistWriteClient._guarded_body`). Keyed on the Airtable record id;
    re-running writes identical values (idempotent). Chunked at
    :data:`MAX_RECORDS_PER_PATCH`.

    Backend guard: when ``resolve_backend() != 'airtable'`` (local mode) this is a
    strict no-op — it makes zero Airtable calls (does not even touch ``client``)
    and returns a plan flagged ``skipped_no_airtable``. The lake is canonical; the
    derived columns are only synced when Airtable is active.

    The nine columns already exist on the live base — this never creates them
    (no ``ensure_fields`` for the Scryfall cols). It resolves the existing ids and
    fails loud if any is missing.

    Args:
        cards: ``{airtable_record_id: card_name}`` — the pulled Cards to refresh.
        resolver: the card-dim resolver (name -> enriched ``Card``); unresolvable
            names are skipped, never guessed.
        price_fetcher: a callable ``name -> price string | None`` (the live price;
            consulted only for resolved cards).
        client: an :class:`AllowlistWriteClient` (or double). Only touched on a
            real (``dry_run=False and apply=True``) airtable run.
        apply / dry_run: a real write requires both ``dry_run=False`` and
            ``apply=True`` (belt-and-suspenders).
    """
    from pipeline.collection.store import resolve_backend

    backend = resolve_backend()

    do_write = (not dry_run) and apply

    if backend != 'airtable':
        # No-op: local mode never touches Airtable. Return a flagged empty plan.
        return DerivedWriteReport(
            backend=backend,
            dry_run=not do_write,
            applied=False,
            skipped_no_airtable=True,
        )

    # Resolve + project each card to its derived payload (skip the unresolvable).
    plan: list[CardWrite] = []
    skipped: list[str] = []
    for record_id in sorted(cards):
        name = cards[record_id]
        card = resolver.get_card(name)
        if card is None or not card.oracle_id:
            log.warning('SKIP Card %s (%r): resolver returned no card — not guessing.', record_id, name)
            skipped.append(record_id)
            continue
        price_usd = price_fetcher(name)
        payload = build_derived_card_payload(card, price_usd)
        assert_no_human_fields(payload.keys())  # up-front guard, before any request
        plan.append(CardWrite(record_id=record_id, fields=payload))

    report = DerivedWriteReport(backend=backend, dry_run=not do_write, applied=do_write, planned=plan, skipped=skipped)
    report.chunks_planned = _chunk_count(len(plan))

    if not do_write:
        return report

    if client is None:
        raise ValueError('A live derived write (dry_run=False, apply=True) requires a client.')

    # Resolve existing field ids — the nine columns already exist; never create.
    name_to_id = client.list_fields()
    written_names = {n for w in plan for n in w.fields}
    missing_ids = written_names - set(name_to_id)
    if missing_ids:
        raise RuntimeError(
            f'Derived Scryfall columns missing ids on the live table: {sorted(missing_ids)}. '
            'These columns must already EXIST on Inventory Cards — this primitive never '
            'creates them. Reconcile the Airtable schema.'
        )

    plan_chunks = _chunk(plan)
    for i, plan_chunk in enumerate(plan_chunks):
        if i > 0 and INTER_CHUNK_DELAY_S > 0:
            time.sleep(INTER_CHUNK_DELAY_S)
        chunk_records: list[dict[str, Any]] = []
        for w in plan_chunk:
            assert_no_human_fields(w.fields.keys())  # guard each chunk's names
            chunk_records.append({'id': w.record_id, 'fields': {name_to_id[n]: v for n, v in w.fields.items()}})
        try:
            client.patch_records(chunk_records)
        except Exception as exc:
            log.error(
                'Derived PATCH chunk %d/%d failed (%d completed): %s. Re-run to resume.',
                i + 1,
                len(plan_chunks),
                report.write_requests_issued,
                exc,
            )
            raise ChunkWriteError(
                chunks_completed=report.write_requests_issued,
                chunks_planned=len(plan_chunks),
            ) from exc
        report.write_requests_issued += 1
    return report


def write_chase_derived_fields(
    cards: dict[str, str],
    *,
    resolver: _SupportsGetCard,
    price_fetcher: Any,
    client: SupportsWrite | None = None,
    apply: bool = False,
    dry_run: bool = True,
) -> DerivedWriteReport:
    """Guarded write of the eleven Chase Cards derived columns. Dry-run default.

    The Chase Cards analogue of :func:`write_derived_fields`. The Chase table
    carries the same eleven engine-derived columns as Inventory: the nine Scryfall-
    pure columns (including Price (TCGPlayer)) plus the two engine ⚙ otag fields (⚙
    Buckets / ⚙ Otags). For each pulled chase Card it sources the nine derived
    columns from the lake ``resolver.get_card`` plus a live price (``price_fetcher(
    name)`` — price is not on the Card contract) plus the two ⚙ fields from the same
    Card's ``otag_buckets`` / ``otags``, maps them via
    :func:`build_chase_derived_card_payload`, and writes them through the chase-bound
    guard (:func:`assert_no_chase_human_fields` + a chase-bound
    :meth:`AllowlistWriteClient._guarded_body`) to the Chase Cards table. Keyed on the
    Airtable record id; idempotent. Chunked at :data:`MAX_RECORDS_PER_PATCH`.

    Asymmetry vs Inventory (:func:`write_derived_fields`, nine): Inventory's ⚙ fields
    come from the separate otag sync path, but Chase has no otag sync — so this inline
    write is chase's only path to ⚙, hence eleven columns.

    Backend guard: when ``resolve_backend() != 'airtable'`` (local mode) this is a
    strict no-op — zero Airtable calls — returning a plan flagged
    ``skipped_no_airtable``.

    The eleven columns already exist on the live Chase Cards table — this never
    creates them. It resolves the existing ids and fails loud if any is missing.

    Args:
        cards: ``{airtable_record_id: card_name}`` — the chase records to refresh.
        resolver: the card-dim resolver (name -> enriched ``Card``); unresolvable
            names are skipped, never guessed.
        price_fetcher: a callable ``name -> price string | None`` (the live price;
            consulted only for resolved cards — Chase has a Price column).
        client: an :class:`AllowlistWriteClient` built with the chase binding (or a
            double). Only touched on a real (``dry_run=False and apply=True``) run.
        apply / dry_run: a real write requires both ``dry_run=False`` and
            ``apply=True`` (belt-and-suspenders).
    """
    from pipeline.collection.store import resolve_backend

    backend = resolve_backend()
    do_write = (not dry_run) and apply

    if backend != 'airtable':
        # No-op: local mode never touches Airtable. Return a flagged empty plan.
        return DerivedWriteReport(
            backend=backend,
            dry_run=not do_write,
            applied=False,
            skipped_no_airtable=True,
        )

    # Resolve + project each card to its chase derived payload (skip unresolvable).
    plan: list[CardWrite] = []
    skipped: list[str] = []
    for record_id in sorted(cards):
        name = cards[record_id]
        card = resolver.get_card(name)
        if card is None or not card.oracle_id:
            log.warning('SKIP Chase %s (%r): resolver returned no card — not guessing.', record_id, name)
            skipped.append(record_id)
            continue
        price_usd = price_fetcher(name)
        payload = build_chase_derived_card_payload(card, price_usd)
        assert_no_chase_human_fields(payload.keys())  # up-front chase guard
        plan.append(CardWrite(record_id=record_id, fields=payload))

    report = DerivedWriteReport(backend=backend, dry_run=not do_write, applied=do_write, planned=plan, skipped=skipped)
    report.chunks_planned = _chunk_count(len(plan))

    if not do_write:
        return report

    if client is None:
        raise ValueError('A live chase derived write (dry_run=False, apply=True) requires a client.')

    # Resolve existing field ids — the nine columns already exist; never create.
    name_to_id = client.list_fields()
    written_names = {n for w in plan for n in w.fields}
    missing_ids = written_names - set(name_to_id)
    if missing_ids:
        raise RuntimeError(
            f'Chase derived columns missing ids on the live table: {sorted(missing_ids)}. '
            'These columns must already EXIST on Chase Cards — this primitive never '
            'creates them. Reconcile the Airtable schema.'
        )

    plan_chunks = _chunk(plan)
    for i, plan_chunk in enumerate(plan_chunks):
        if i > 0 and INTER_CHUNK_DELAY_S > 0:
            time.sleep(INTER_CHUNK_DELAY_S)
        chunk_records: list[dict[str, Any]] = []
        for w in plan_chunk:
            assert_no_chase_human_fields(w.fields.keys())  # guard each chunk's names
            chunk_records.append({'id': w.record_id, 'fields': {name_to_id[n]: v for n, v in w.fields.items()}})
        try:
            client.patch_records(chunk_records)
        except Exception as exc:
            log.error(
                'Chase derived PATCH chunk %d/%d failed (%d completed): %s. Re-run to resume.',
                i + 1,
                len(plan_chunks),
                report.write_requests_issued,
                exc,
            )
            raise ChunkWriteError(
                chunks_completed=report.write_requests_issued,
                chunks_planned=len(plan_chunks),
            ) from exc
        report.write_requests_issued += 1
    return report


def default_card_resolver() -> Any:
    """Return the package default card-dim resolver (lazy import to avoid cycles).

    Indirected here so the CLI / tests can monkeypatch it on this module without
    reaching into ``collection.resolver``.
    """
    from pipeline.collection.resolver import default_card_resolver as _dcr

    return _dcr()


def _live_price_fetcher() -> Any:
    """Return a ``name -> live USD price string | None`` callable.

    Sources the volatile price live (never the lake Card) via the shared
    ``fetch_card_raw`` path (Scryfall ``prices.usd``), reusing one httpx client
    across the run. Fails open per card: an unresolved/erroring name -> None.
    """
    from pipeline.collection.resolver import fetch_card_raw

    client = httpx.Client(timeout=30, headers=HEADERS_UA)

    def _price(name: str) -> str | None:
        data = fetch_card_raw(name, client=client)
        if not data:
            return None
        return (data.get('prices') or {}).get('usd')

    return _price


def refresh_derived_columns(
    *,
    apply: bool = False,
    dry_run: bool = True,
) -> DerivedWriteReport:
    """Explicit bulk refresh: write the derived columns for all pulled Cards.

    The public entry point behind the ``--refresh`` CLI verb. Dry-run by default
    (a real write requires ``--no-dry-run --apply``). Backend-guarded via
    :func:`write_derived_fields`: in local mode it is a no-op with zero Airtable
    calls. Loads the Cards records from the lake, resolves each via the card dim,
    and sources the live price per card.
    """
    from pipeline.collection.store import resolve_backend

    backend = resolve_backend()
    do_write = (not dry_run) and apply

    # Local mode: never build a client, never call the lake schema. Short-circuit.
    if backend != 'airtable':
        return DerivedWriteReport(backend=backend, dry_run=not do_write, applied=False, skipped_no_airtable=True)

    token = os.environ.get('AIRTABLE_API_KEY')
    if not token:
        raise RuntimeError('Backend is `airtable` but AIRTABLE_API_KEY is not set; cannot refresh derived columns.')
    client = AllowlistWriteClient(token)
    try:
        schema = client.list_fields()  # read-only meta GET (also binds the Cards table)
        card_name_field_id = schema.get(CARD_NAME_FIELD)
        cards, _n2o, _otag = _load_cards_from_lake(card_name_field_id)
        return write_derived_fields(
            cards,
            resolver=default_card_resolver(),
            price_fetcher=_live_price_fetcher(),
            client=client,
            apply=apply,
            dry_run=dry_run,
        )
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _load_cards_from_lake(
    card_name_field_id: str | None = None,
) -> tuple[dict[str, str], dict[str, str], dict[str, set[str]]]:
    """Best-effort: load pulled Cards + the oracle_id join maps from the lake.

    Returns ``(cards, name_to_oracle_id, card_otag)``:
        - ``cards``: ``{airtable_record_id: card_name}`` from ``raw/airtable_cards``.
        - ``name_to_oracle_id``: ``{name: oracle_id}`` from ``raw/oracle_cards``.
        - ``card_otag``: ``{oracle_id: {slug}}`` from ``normalized/card_otag``.

    ``card_name_field_id`` is the per-base Card Name ``fld…`` id (resolved at
    runtime from the meta schema by name — no hard-coded id). When omitted, only
    the ``'Card Name'`` display-name column fallback is used.

    The CLI's job here is to prove mechanics; a real deployment wires the reads.
    Returns empty maps if a source table is absent so ``--dry-run`` still runs.
    """
    try:
        from pipeline import store  # local import; store is optional for the guard
    except Exception:  # pragma: no cover - store always present in-repo
        return {}, {}, {}

    cards: dict[str, str] = {}
    name_to_oracle_id: dict[str, str] = {}
    card_otag: dict[str, set[str]] = {}

    with store.connect() as conn:
        if store.table_exists('raw', 'airtable_cards'):
            rel = store.read_parquet(conn, 'raw', 'airtable_cards')
            cols = rel.columns
            # The Airtable pull stores columns by field id (returnFieldsByFieldId
            # =true, the durable key that survives renames), plus "_record_id".
            # So the human card name lives in the Card Name field-id column, not a
            # "Card Name" name column. Read by the runtime-resolved field id, with
            # a name-keyed fallback in case a pull ever lands display names.
            for row in rel.fetchall():
                rec = dict(zip(cols, row, strict=False))
                rid = rec.get('_record_id')
                name = (rec.get(card_name_field_id) if card_name_field_id else None) or rec.get(CARD_NAME_FIELD)
                if rid and name:
                    cards[str(rid)] = str(name)
        if store.table_exists('raw', 'oracle_cards'):
            rel = store.read_parquet(conn, 'raw', 'oracle_cards')
            for name, oid in rel.select('name, oracle_id').fetchall():
                if name and oid:
                    name_to_oracle_id[str(name)] = str(oid)
        if store.table_exists('normalized', 'card_otag'):
            rel = store.read_parquet(conn, 'normalized', 'card_otag')
            for oid, slug in rel.select('oracle_id, slug').fetchall():
                if oid and slug:
                    card_otag.setdefault(str(oid), set()).add(str(slug))
    return cards, name_to_oracle_id, card_otag


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='python -m pipeline.destinations.airtable',
        description='Sync per-card derived otag facts to Airtable Cards (dry-run by default).',
    )
    parser.add_argument(
        '--dry-run',
        dest='dry_run',
        action='store_true',
        default=True,
        help='Print the diff without writing (DEFAULT).',
    )
    parser.add_argument(
        '--apply',
        dest='apply',
        action='store_true',
        default=False,
        help='Actually write. Requires --no-dry-run too. Live writes need AIRTABLE_API_KEY.',
    )
    parser.add_argument(
        '--no-dry-run',
        dest='dry_run',
        action='store_false',
        help='Disable dry-run (still requires --apply to write).',
    )
    parser.add_argument(
        '--refresh',
        dest='refresh',
        action='store_true',
        default=False,
        help=(
            'CARD-DIM DERIVED refresh: write the nine Scryfall-derived Cards columns '
            '(Card Type, Mana Cost, CMC, Power / Toughness, Oracle Text, Card Art, '
            'Scryfall URL, Price (TCGPlayer), Color Identity) for every pulled Card, '
            'sourced from the card dim + a live price. Dry-run by default; a real write '
            'requires --no-dry-run --apply. NO-OP (zero Airtable calls) unless the '
            'active backend is airtable.'
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

    # --refresh runs the card-dim derived-column primitive instead of the
    # otag ⚙-field sync. Backend-guarded + dry-run by default inside the entry point.
    if args.refresh:
        report = refresh_derived_columns(apply=args.apply, dry_run=args.dry_run)
        print(report.render())
        return 0

    client: AllowlistWriteClient | None = None
    do_write = (not args.dry_run) and args.apply
    card_name_field_id: str | None = None
    #: The live Cards field names, from a read-only meta read. ``None`` means we
    #: had no API access to resolve them (offline dry-run) -> the plan is unknown.
    field_names_present: frozenset[str] | None = None
    token = os.environ.get('AIRTABLE_API_KEY')
    if do_write and not token:
        raise RuntimeError('AIRTABLE_API_KEY is not set; cannot apply a live write.')
    # Safety: whenever a credential is available, resolve the schema via a
    # read-only meta call regardless of do_write. In a dry-run this makes the
    # preview truthful — it resolves the Card Name field id (so the field-id-keyed
    # lake yields the real cards) and reads which derived fields already exist (so
    # only genuinely-missing ones are reported as "would create"). This issues no
    # writes (list_fields is a GET); a real write still requires --no-dry-run
    # --apply below. Without a credential a dry-run stays offline and reports the
    # plan as unknown rather than a misleading vacuous 0 / bogus creation claim.
    if token:
        client = AllowlistWriteClient(token)
        schema = client.list_fields()  # read-only meta GET
        card_name_field_id = schema.get(CARD_NAME_FIELD)
        field_names_present = frozenset(schema)
    cards, name_to_oracle_id, card_otag = _load_cards_from_lake(card_name_field_id)
    try:
        report = sync(
            cards,
            name_to_oracle_id=name_to_oracle_id,
            card_otag=card_otag,
            client=client if do_write else None,
            dry_run=args.dry_run,
            apply=args.apply,
            field_names_present=field_names_present,
        )
    finally:
        if client is not None:
            client.close()
    print(report.render())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
