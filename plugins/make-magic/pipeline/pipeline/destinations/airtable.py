"""Airtable destination: SYNC engine-derived per-CARD otag facts -> Airtable Cards.

This reverse-ETL destination owns exactly TWO derived fields on the *Cards* table. The base
id, the Cards table id, and the Card Name / field ids are NOT hard-coded:
the base id + Cards table NAME come from env-driven :mod:`pipeline.config`, and
the ``tbl…``/``fld…`` ids are resolved at runtime via the Airtable meta API — so
the write-back is not locked to one Airtable instance. A card's otags are a PURE
FUNCTION of the card (keyed to its Scryfall ``oracle_id``), so storing them on
Cards is authoritative and never stale; Airtable rolls them up to decks natively
(the "derived/bulk -> Local authoritative -> push to Airtable" authority rule).

    - ``⚙ Buckets`` — Airtable **multipleSelects** — the crosswalked functional
      buckets for the card (``crosswalk.buckets_for``: removal / ramp / draw /
      counters / burn / tutor / typal / ...), the option set == the crosswalk
      vocabulary (incl. the flagged gap buckets).
    - ``⚙ Otags``  — Airtable **multilineText** — the card's raw rolled-up
      Scryfall otag slugs (newline-joined), kept for fidelity / debugging.

THE GOVERNING SAFETY PROPERTY: this module may write ONLY the small
:data:`DERIVED_FIELDS` allowlist it owns — engine-computed, namespace-prefixed
fields on the Cards table. It must NEVER create, update, or delete any
human-edited Card field (Card Name, Sets, Number Owned, Sources,
Condition, Card Type, Mana Cost, CMC, Power / Toughness, Oracle Text, Card Art,
Scryfall URL, Price (TCGPlayer), Color Identity, Decks, ...). That property is
enforced STRUCTURALLY, not by convention, at three layers:

    1. Payload construction. :func:`build_payload` emits ONLY keys drawn from
       :data:`DERIVED_FIELDS`. There is no code path that copies an arbitrary
       field name into a payload.
    2. A hard guard. :func:`assert_no_human_fields` computes the intersection of
       (payload field names) and (the human DENYLIST loaded from
       ``regression/golden_contract.json`` — the CARDS table's human fields) and
       RAISES :class:`HumanFieldWriteError` if it is non-empty — before any
       request is built. Every write path calls it.
    3. A guarded client. :class:`AllowlistWriteClient` wraps the private httpx
       client; every mutating request is routed through :meth:`_guarded_body`,
       which re-runs the guard on the exact body about to leave the process. The
       httpx client is name-mangled so callers cannot bypass the choke point.

Join model: each Airtable Card record -> its ``oracle_id`` (resolved from the
card name against the Scryfall oracle-card table) -> its ``card_otag`` slug
closure -> ``buckets_for``. A card that does not robustly resolve to an
``oracle_id`` (or carries no otag data) is SKIPPED and logged — never guessed.
Upsert is keyed on the Airtable Card **record id** with ``use_field_ids=True``,
so re-running is idempotent (no dupes, no thrash).

Dry-run is the DEFAULT. :func:`sync` and :func:`ensure_fields` print the diff /
schema mutation they WOULD apply and issue ZERO write requests unless the caller
passes ``dry_run=False`` AND ``apply=True`` (belt-and-suspenders: a real write
requires both).

Transport mirrors ``sources/airtable.py`` (httpx + Bearer PAT); ``pyairtable`` is
declared as the ``airtable`` extra but is NOT required here.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from pipeline.config import AirtableConfigError, get_settings
from pipeline.transforms.crosswalk import BUCKETS, buckets_for

log = logging.getLogger('make_magic.destinations.airtable')

API_ROOT = 'https://api.airtable.com/v0'
META_ROOT = 'https://api.airtable.com/v0/meta'
HEADERS_UA = {'User-Agent': 'make-magic-plugin/2.0'}

#: Card Name (primary) field NAME. The pull lands columns by field id, so the
#: lake loader resolves this NAME to its per-base id at runtime (via the meta
#: schema) to read the card-name column — no hard-coded ``fld…`` id.
CARD_NAME_FIELD = 'Card Name'

#: Airtable caps a single PATCH ``records[]`` array at 10 records. The apply path
#: MUST chunk the resolved writes to this size; a full-inventory run of hundreds
#: of cards would otherwise send one oversized request and be rejected.
MAX_RECORDS_PER_PATCH = 10

#: Airtable's REST API rate limit is ~5 requests/sec per base. We sleep this long
#: between chunk PATCHes to stay comfortably under it (5 req/s => 0.2s spacing).
#: Kept simple on purpose; a real backoff-on-429 layer can come later.
INTER_CHUNK_DELAY_S = 0.25

#: Namespace prefix marking a field as ENGINE-OWNED. The gear glyph makes it
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


#: THE MULTI-SELECT OPTION SET for ``⚙ Buckets``. Airtable requires every
#: multipleSelects option to exist before it can be written; we seed the field
#: with the FULL crosswalk vocabulary (incl. the flagged gap buckets) at
#: :func:`ensure_fields` time, so any bucket ``buckets_for`` can emit is writable.
BUCKET_OPTIONS: tuple[str, ...] = BUCKETS

#: THE ALLOWLIST. The ONLY Airtable fields this adapter may ever touch. Each maps
#: a per-card derived value to a namespace-prefixed CARDS field. Adding a field
#: here is the ONLY way to authorize a new write; nothing else in this module
#: enumerates field names.
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

#: The set of field NAMES the allowlist authorizes. Frozen so it can be used as a
#: membership check and diffed against the denylist at import time.
ALLOWLIST_NAMES: frozenset[str] = frozenset(f.name for f in DERIVED_FIELDS)

#: Convenience handles onto the two field names (single source of truth = tuple).
BUCKETS_FIELD: str = f'{NS}Buckets'
OTAGS_FIELD: str = f'{NS}Otags'


class HumanFieldWriteError(RuntimeError):
    """Raised when a write payload would touch a human-edited (denylisted) field.

    This is the enforcement of the per-field authority invariant: the derived
    write-back must NEVER create/update/delete a human field. Raised BEFORE any
    request is constructed.
    """


class ChunkWriteError(RuntimeError):
    """Raised when a chunked apply fails on one of its PATCH batches.

    Carries the partial progress so an operator (or a re-run) knows exactly how
    far the apply got before it stopped: ``chunks_completed`` chunks were PATCHed
    successfully out of ``chunks_planned`` total. Because the upsert is keyed on
    the Airtable record id (idempotent), a re-run re-issues the SAME plan and the
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
# Human DENYLIST — the CARDS table's human-edited fields, from the golden
# contract so it stays in sync.
# --------------------------------------------------------------------------- #

#: The Cards table KEY in the golden contract (target of this write-back). This
#: indexes the STATIC contract (its table keys use the live name "Inventory
#: Cards"). It is DELIBERATELY distinct from ``config.Settings.cards_table`` (which
#: resolves the LIVE schema by name); the two happen to share the same string but
#: are separate concerns — one keys the committed contract, the other hits the API.
CARDS_TABLE_NAME = 'Inventory Cards'


def _golden_contract_path() -> Path:
    """Locate ``regression/golden_contract.json`` relative to this package.

    This file lives at ``pipeline/pipeline/destinations/airtable.py``;
    ``parents[3]`` is the ``make-magic`` plugin root that holds ``regression/``.
    """
    return Path(__file__).resolve().parents[3] / 'regression' / 'golden_contract.json'


def load_human_denylist(path: Path | None = None) -> frozenset[str]:
    """Load the DENYLIST of human-edited **Cards** field names from the contract.

    The write-back targets the Cards table, so the fields it must never touch
    are the Cards table's human-edited fields: the Cards ``primary_field`` plus
    every ``required_fields`` entry any skill declares for the Cards table (Card
    Name, Sets, Number Owned, Sources, Condition, Card Type, Mana Cost, CMC,
    Power / Toughness, Oracle Text, Card Art, Scryfall URL, Price (TCGPlayer),
    Color Identity, Decks, ...). Deriving it here (rather than hard-coding) keeps
    it in lock-step with the contract: if a Cards human field is added, it is
    automatically denied.
    """
    contract = json.loads((path or _golden_contract_path()).read_text(encoding='utf-8'))
    denied: set[str] = set()
    cards_meta = contract.get('tables', {}).get(CARDS_TABLE_NAME, {})
    pf = cards_meta.get('primary_field')
    if pf:
        denied.add(pf)
    for skill in contract.get('skills', {}).values():
        cards_tbl = skill.get('tables', {}).get(CARDS_TABLE_NAME, {})
        denied.update(cards_tbl.get('required_fields', []))
    return frozenset(denied)


#: Loaded once at import so the guard is cheap and the allowlist/denylist
#: disjointness can be asserted structurally below.
HUMAN_DENYLIST: frozenset[str] = load_human_denylist()

# STRUCTURAL INVARIANT, checked at import time: the fields this module is allowed
# to write and the human fields it must never write are DISJOINT. If a future
# edit ever names an allowlist field after a human field, this import fails loud.
_OVERLAP = ALLOWLIST_NAMES & HUMAN_DENYLIST
assert not _OVERLAP, (
    f'FATAL: derived allowlist overlaps the human denylist: {sorted(_OVERLAP)}. '
    'The write-back must never own a human-edited field name.'
)


def assert_no_human_fields(payload_fields: Any) -> None:
    """Guard: refuse any payload that touches a human (denylisted) field, or any
    field outside the allowlist. Called before every request is built.

    Two independent checks so the failure mode is explicit:
        1. intersection with :data:`HUMAN_DENYLIST` must be EMPTY (the core proof);
        2. every field must be in :data:`ALLOWLIST_NAMES` (closed-set defense).
    """
    fields = set(payload_fields)
    human = fields & HUMAN_DENYLIST
    if human:
        raise HumanFieldWriteError(
            f'REFUSED: write payload contains human-edited Card field(s) {sorted(human)}. '
            'This adapter may only write the derived allowlist '
            f'{sorted(ALLOWLIST_NAMES)}; human fields are pull-only.'
        )
    non_allowed = fields - ALLOWLIST_NAMES
    if non_allowed:
        raise NonAllowlistFieldError(
            f'REFUSED: write payload contains non-allowlisted field(s) '
            f'{sorted(non_allowed)}. Only {sorted(ALLOWLIST_NAMES)} may be written.'
        )


# --------------------------------------------------------------------------- #
# Card resolution + payload construction.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CardResolution:
    """A single Airtable Card record joined to its derived otag facts.

    ``record_id`` is the durable Airtable record id (the upsert key). ``oracle_id``
    is the resolved Scryfall key (``None`` if the card name did not robustly
    resolve — such rows are SKIPPED, never guessed). ``buckets`` and ``otags`` are
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
    """Join pulled Airtable Cards to their per-card otag facts (OFFLINE, pure).

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
        oid = name_to_oracle_id.get(name)
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

    Emits EXACTLY the two allowlisted fields: ``⚙ Buckets`` (a list of bucket
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


def plan_writes(
    cards: dict[str, str],
    *,
    name_to_oracle_id: dict[str, str],
    card_otag: dict[str, set[str]],
) -> list[CardWrite]:
    """Turn pulled Cards into a deterministic write plan (idempotent).

    Resolves each card to its ``oracle_id`` and derived facts, SKIPPING any card
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
    """httpx wrapper that permits ONLY allowlisted-field writes to Cards.

    Every mutating body is routed through :meth:`_guarded_body`, which re-runs
    :func:`assert_no_human_fields` on the exact fields about to be sent. The
    httpx client is name-mangled (private) so there is no path to issue a raw
    request that skips the guard. Uses ``use_field_ids=True`` by resolving field
    names to ids via the meta schema before patching.
    """

    def __init__(
        self,
        token: str,
        *,
        base_id: str | None = None,
        cards_table_name: str | None = None,
        _client: httpx.Client | None = None,
    ) -> None:
        self.__client = _client or httpx.Client(timeout=30)
        self.__auth = {'Authorization': f'Bearer {token}', **HEADERS_UA}
        #: Env-driven identity. Base id + Cards table NAME come from Settings
        #: (overridable per instance); the Cards table ``tbl…`` id is discovered
        #: from the meta schema by NAME in :meth:`list_fields` (never hard-coded).
        settings = get_settings()
        self._base_id = base_id or settings.airtable_base_id
        self._cards_table_name = cards_table_name or settings.cards_table
        #: Resolved lazily from the meta schema on the first :meth:`list_fields`.
        self._cards_table_id: str | None = None
        #: id-keyed wire guard state, derived from the resolved allowlist
        #: name->id map. Empty until :meth:`resolve_field_map` (or
        #: :meth:`list_fields`) runs — so before resolution NO id can pass the
        #: wire guard (fail-closed).
        self._allowed_ids: frozenset[str] = frozenset()
        self._id_to_name: dict[str, str] = {}

    # --- schema (meta API) -------------------------------------------------- #

    def resolve_field_map(self, name_to_id: dict[str, str]) -> None:
        """Derive the id-keyed wire-guard state from a name->id schema map.

        By the wire the payload is keyed by Airtable field IDs, but the allowlist
        is a set of NAMES. So we translate: ``allowed_ids`` = the ids of the two
        allowlisted derived fields (``⚙ Buckets`` / ``⚙ Otags``), and
        ``id_to_name`` = the reverse map RESTRICTED to those allowed ids. Human
        field ids (and unknown ids) are deliberately excluded, so the wire guard
        can reject anything that is not one of the two derived ids.
        """
        self._allowed_ids = frozenset(fid for name, fid in name_to_id.items() if name in ALLOWLIST_NAMES)
        self._id_to_name = {fid: name for name, fid in name_to_id.items() if name in ALLOWLIST_NAMES}

    def list_fields(self) -> dict[str, str]:
        """Return ``{field_name: field_id}`` for the Cards table (meta API GET).

        The Cards table is located by its env-driven NAME (:attr:`_cards_table_name`),
        NOT a hard-coded ``tbl…`` id; its id is cached in :attr:`_cards_table_id`
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
                self._cards_table_id = table.get('id')
                name_to_id = {f['name']: f['id'] for f in table.get('fields', [])}
                self.resolve_field_map(name_to_id)
                return name_to_id
        raise AirtableConfigError(
            f'table {self._cards_table_name!r} not found in base {self._base_id!r}; '
            'set AIRTABLE_CARDS_TABLE (and AIRTABLE_BASE_ID) for this instance.'
        )

    def create_field(self, name: str, airtable_type: str, options: dict[str, Any] | None) -> None:
        """Create a DERIVED field on Cards. REFUSES any non-allowlisted name."""
        if name not in ALLOWLIST_NAMES:
            raise NonAllowlistFieldError(f'REFUSED: cannot create field {name!r} — not in the derived allowlist.')
        assert_no_human_fields([name])
        table_id = self._require_cards_table_id()
        url = f'{META_ROOT}/bases/{self._base_id}/tables/{table_id}/fields'
        body: dict[str, Any] = {'name': name, 'type': airtable_type}
        if options:
            body['options'] = options
        resp = self.__client.post(url, headers=self.__auth, json=body)
        resp.raise_for_status()

    def _require_cards_table_id(self) -> str:
        """Return the resolved Cards table id, resolving it first if needed.

        The id is discovered from the meta schema by NAME (:meth:`list_fields`);
        this triggers that resolution on demand so create/patch URLs are never
        built from a hard-coded id.
        """
        if self._cards_table_id is None:
            self.list_fields()
        assert self._cards_table_id is not None
        return self._cards_table_id

    # --- records ------------------------------------------------------------ #

    def _guarded_body(self, records: list[dict[str, Any]]) -> None:
        """Re-run the guard on the EXACT field IDs in every record about to be sent.

        By the wire the payload is keyed by Airtable **field IDs**, not names, so
        the name-level allowlist cannot be applied directly. Instead we enforce
        the same guarantee in id-space: every field id MUST be one of the two
        derived ids in :attr:`_allowed_ids` (built from the resolved allowlist
        name->id map). Anything else — a human field id, or an id with no mapping
        — is rejected. As a belt-and-suspenders proof we also translate the
        allowed ids back to their names via :attr:`_id_to_name` and re-run the
        name-level :func:`assert_no_human_fields` on them, so the two guards agree.

        Fail-closed: if the field map has not been resolved yet, ``_allowed_ids``
        is empty and NO id can pass.
        """
        for rec in records:
            field_ids = set((rec.get('fields') or {}).keys())
            unknown = field_ids - self._allowed_ids
            if unknown:
                raise NonAllowlistFieldError(
                    f'REFUSED: write body contains non-allowlisted field id(s) '
                    f'{sorted(unknown)}. Only the derived field ids '
                    f'{sorted(self._allowed_ids)} (== {sorted(ALLOWLIST_NAMES)}) '
                    'may be written; a human field id or unknown id is refused.'
                )
            # Belt-and-suspenders: the mapped names must ALSO pass the name guard.
            assert_no_human_fields(self._id_to_name[fid] for fid in field_ids)

    def patch_records(self, records: list[dict[str, Any]]) -> httpx.Response:
        """PATCH derived fields onto existing Cards records (upsert by id).

        ``records`` are ``{"id": rec, "fields": {field_id: value}}`` already keyed
        by field ID (``use_field_ids=True``). The guard runs on the raw field
        keys one final time before the request leaves the process.
        """
        # The guard runs BEFORE id-resolution too (see sync), but re-check here on
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
    """Result of a (dry-run or applied) push."""

    dry_run: bool
    applied: bool
    planned: list[CardWrite] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    fields_to_create: list[str] = field(default_factory=list)
    write_requests_issued: int = 0
    #: Number of PATCH chunks the plan maps to (Airtable's 10-records-per-request
    #: cap). In a dry-run this is the count it WOULD send; in an applied run it
    #: equals ``write_requests_issued`` on success.
    chunks_planned: int = 0

    def render(self) -> str:
        lines: list[str] = []
        mode = 'DRY-RUN (no writes issued)' if self.dry_run else 'APPLY'
        lines.append(f'=== Airtable derived write-back -> Cards [{mode}] ===')
        lines.append(f'Allowlist (engine-owned): {sorted(ALLOWLIST_NAMES)}')
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
    """Ensure every DERIVED field exists on Cards; create the missing ones.

    Schema mutation is allowed for DERIVED fields ONLY (each create call refuses
    a non-allowlisted name). ``⚙ Buckets`` is created as a multipleSelects seeded
    with the full crosswalk vocabulary (Airtable requires the options to exist
    before any can be written); ``⚙ Otags`` as multilineText. Dry-run by default:
    returns the names it WOULD create without issuing any request unless
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
) -> PushReport:
    """Sync per-card derived otag facts onto Cards records. DRY-RUN BY DEFAULT.

    Args:
        cards: ``{airtable_record_id: card_name}`` — the pulled Cards records.
        name_to_oracle_id: ``{card_name: oracle_id}`` durable-key resolver.
        card_otag: ``{oracle_id: {slug}}`` rolled-up slug closure per card.
        client: an :class:`AllowlistWriteClient` (or test double). Only touched
            when a real (``dry_run=False and apply=True``) run is requested; a
            dry-run needs no client and issues no requests.
        dry_run: when True (default) NO write request is issued; the intended
            diff is computed and returned.
        apply: a real write requires BOTH ``dry_run=False`` AND ``apply=True``.

    Returns:
        A :class:`PushReport`. In dry-run, ``write_requests_issued == 0``. Cards
        without a robust ``oracle_id`` match are in ``report.skipped``.
    """
    resolved, skipped = resolve_cards(cards, name_to_oracle_id=name_to_oracle_id, card_otag=card_otag)
    plan = [CardWrite(record_id=r.record_id, fields=build_payload(r)) for r in resolved]
    # Guard EVERY planned payload up front — before any request is even eligible.
    for w in plan:
        assert_no_human_fields(w.fields.keys())

    do_write = (not dry_run) and apply
    report = PushReport(dry_run=not do_write, applied=do_write, planned=plan, skipped=skipped)
    # How many PATCH chunks this plan maps to (Airtable's 10-records-per-request
    # cap). Reported in BOTH modes: in dry-run it is what we WOULD send.
    report.chunks_planned = _chunk_count(len(plan))

    if not do_write:
        report.fields_to_create = [f.name for f in DERIVED_FIELDS]  # informational
        return report

    if client is None:
        raise ValueError('A live push (dry_run=False, apply=True) requires a client.')

    # Schema first: create any missing derived fields, then resolve name->id.
    ensure_fields(client, dry_run=False, apply=True)
    name_to_id = client.list_fields()
    missing_ids = ALLOWLIST_NAMES - set(name_to_id)
    if missing_ids:
        raise RuntimeError(f'Derived fields missing ids after ensure: {sorted(missing_ids)}')

    # Chunk to Airtable's 10-records-per-PATCH cap. We chunk the NAME-keyed plan
    # so the human-field guard runs on real field NAMES for EACH chunk before it
    # leaves the process (the allowlist is a set of names, not field ids); only
    # then do we re-key that chunk by FIELD ID (use_field_ids=True) for the wire.
    # Sleep between chunks to respect the ~5 req/s rate limit, and record how many
    # chunks succeeded. On a chunk error, STOP (do not silently continue) and
    # surface partial progress via ChunkWriteError so an idempotent re-run (upsert
    # by record id) can resume safely.
    plan_chunks = _chunk(plan)
    for i, plan_chunk in enumerate(plan_chunks):
        # Politeness delay BEFORE every chunk after the first (never before the
        # first, so a single-chunk apply pays no latency).
        if i > 0 and INTER_CHUNK_DELAY_S > 0:
            time.sleep(INTER_CHUNK_DELAY_S)
        # Guard THIS chunk's payloads (field NAMES) before it is transmitted.
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
    runtime from the meta schema by NAME — no hard-coded id). When omitted, only
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
            # The Airtable pull stores columns by FIELD ID (returnFieldsByFieldId
            # =true, the durable key that survives renames), plus "_record_id".
            # So the human card name lives in the Card Name FIELD-ID column, not a
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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

    client: AllowlistWriteClient | None = None
    do_write = (not args.dry_run) and args.apply
    card_name_field_id: str | None = None
    if do_write:
        token = os.environ.get('AIRTABLE_API_KEY')
        if not token:
            raise RuntimeError('AIRTABLE_API_KEY is not set; cannot apply a live write.')
        client = AllowlistWriteClient(token)
        # Resolve the per-base Card Name field id from the meta schema by NAME so
        # the lake loader reads the field-ID-keyed card-name column correctly.
        card_name_field_id = client.list_fields().get(CARD_NAME_FIELD)
    cards, name_to_oracle_id, card_otag = _load_cards_from_lake(card_name_field_id)
    try:
        report = sync(
            cards,
            name_to_oracle_id=name_to_oracle_id,
            card_otag=card_otag,
            client=client,
            dry_run=args.dry_run,
            apply=args.apply,
        )
    finally:
        if client is not None:
            client.close()
    print(report.render())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
