"""OFFLINE tests for the per-CARD derived write-back adapter.

The engine writes two fields onto the *Cards* table (``⚙ Buckets`` +
``⚙ Otags``) — not the Decks table.

The core safety proofs:
    - The guard refuses any payload containing a human-edited card field
      (parametrized over every Cards human field enumerated in the golden
      contract) and any non-allowlisted field.
    - A dry-run issues zero write requests (the client is a spy; no
      POST/PATCH/DELETE is recorded).
    - Payload maps a card's (buckets, otags) -> exactly the two allowlisted
      fields; buckets as a list, otags as multiline text.
    - Idempotency: the same input twice yields the same intended plan.
    - Cards whose oracle_id does not robustly resolve are skipped, not written.

No network: the only "client" is an in-memory spy that records every method it is
asked to perform. In dry-run the client is never even constructed.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pipeline import config
from pipeline.destinations import airtable as wb


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Settings is an lru_cached singleton; read env fresh for each test."""
    config.get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Fixtures: a representative Cards pull + oracle_id/otag join maps + a spy.
# --------------------------------------------------------------------------- #

# oracle_ids are arbitrary durable strings for the offline join.
_OID_CULTIVATE = 'oid-cultivate'
_OID_SOL_RING = 'oid-sol-ring'
_OID_UNTAGGED = 'oid-untagged'

#: Pulled Cards: {airtable_record_id: card_name}.
_CARDS = {
    'recCultivate': 'Cultivate',
    'recSolRing': 'Sol Ring',
}

#: Card name -> oracle_id (the durable-key resolver, from Scryfall oracle-cards).
_NAME_TO_OID = {
    'Cultivate': _OID_CULTIVATE,
    'Sol Ring': _OID_SOL_RING,
    'Some Untagged Card': _OID_UNTAGGED,
}

#: oracle_id -> rolled-up slug closure (from otag_rollup). Cultivate rolls up to
#: ramp AND tutor slugs; Sol Ring to ramp.
_CARD_OTAG: dict[str, set[str]] = {
    _OID_CULTIVATE: {'ramp', 'tutor'},
    _OID_SOL_RING: {'ramp'},
    # _OID_UNTAGGED intentionally absent -> no otag data -> skipped.
}


def _resolution(
    record_id: str = 'recCultivate',
    oracle_id: str = _OID_CULTIVATE,
    buckets: tuple[str, ...] = ('ramp', 'tutor'),
    otags: tuple[str, ...] = ('ramp', 'tutor'),
) -> wb.CardResolution:
    return wb.CardResolution(record_id=record_id, oracle_id=oracle_id, buckets=buckets, otags=otags)


class SpyClient:
    """In-memory stand-in for AllowlistWriteClient that records every call.

    Records nothing that hits the network; ``methods`` accumulates a label for
    each write-shaped operation so a test can assert ZERO writes in dry-run.
    """

    def __init__(self, existing_fields: dict[str, str] | None = None) -> None:
        self.methods: list[str] = []
        self._fields = dict(existing_fields or {})
        self.patched_chunks: list[list[dict[str, Any]]] = []

    def list_fields(self) -> dict[str, str]:
        self.methods.append('GET:list_fields')  # a read, not a write
        return dict(self._fields)

    def create_field(self, name: str, airtable_type: str, options: dict[str, Any] | None) -> None:
        self.methods.append(f'POST:create_field:{name}')
        # simulate the field now existing with a synthetic id
        self._fields[name] = f'fld_{len(self._fields)}'

    def patch_records(self, records: list[dict[str, Any]]) -> Any:
        self.methods.append('PATCH:patch_records')
        self.patched = records
        self.patched_chunks.append(list(records))
        return None

    @property
    def write_methods(self) -> list[str]:
        return [m for m in self.methods if m.split(':', 1)[0] in {'POST', 'PATCH', 'PUT', 'DELETE'}]


def _push(**overrides: Any) -> wb.PushReport:
    """Call sync() with the standard offline join maps (override as needed)."""
    kwargs: dict[str, Any] = {
        'cards': _CARDS,
        'name_to_oracle_id': _NAME_TO_OID,
        'card_otag': _CARD_OTAG,
    }
    kwargs.update(overrides)
    return wb.sync(**kwargs)


# --------------------------------------------------------------------------- #
# Guard: refuses human CARD fields (parametrized over the golden-contract Cards).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize('human_field', sorted(wb._human_denylist()))
def test_guard_refuses_every_human_card_field(human_field: str) -> None:
    """assert_no_human_fields RAISES for EACH Cards human field in the contract."""
    with pytest.raises(wb.HumanFieldWriteError):
        wb.assert_no_human_fields([human_field])


@pytest.mark.parametrize('human_field', sorted(wb._human_denylist()))
def test_guard_refuses_human_field_mixed_with_allowed(human_field: str) -> None:
    """Even mixed with a legit derived field, a single human field is refused."""
    good = next(iter(wb.ALLOWLIST_NAMES))
    with pytest.raises(wb.HumanFieldWriteError):
        wb.assert_no_human_fields([good, human_field])


def test_guard_refuses_non_allowlisted_unknown_field() -> None:
    """A field that is neither human nor engine-owned is still refused (closed set)."""
    with pytest.raises(wb.NonAllowlistFieldError):
        wb.assert_no_human_fields(['Some New Column'])


def test_guard_allows_pure_allowlist() -> None:
    """The allowlist itself passes the guard (no false positive)."""
    wb.assert_no_human_fields(wb.ALLOWLIST_NAMES)


#: The full card-dim / engine-derived Scryfall field set:
#: engine-writable presentation/rules columns plus the two ⚙ namespace fields.
_DERIVED_SCRYFALL_FIELDS = frozenset(
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
#: The genuinely human/collection-authoritative Inventory Cards fields: the
#: write-back must never touch these — even though skills read some of them.
_HUMAN_CARD_FIELDS = frozenset(
    {
        'Card Name',
        'Sets',
        'Number Owned',
        'Sources',
        'Condition',
        'Repeat Number in Decks',
        'Number in Decks',
        'Number in Library',
        'Decks',
    }
)


def test_allowlist_is_the_full_card_dim_derived_set() -> None:
    """The allowlist expands from the two ⚙ fields to the full card-dim set:
    the engine-derived Scryfall columns plus ⚙ Buckets / ⚙ Otags."""
    expected = _DERIVED_SCRYFALL_FIELDS | {f'{wb.NS}Buckets', f'{wb.NS}Otags'}
    assert expected == wb.ALLOWLIST_NAMES


def test_gear_fields_still_in_allowlist() -> None:
    """The two engine-namespaced ⚙ fields remain writable (unchanged)."""
    assert {f'{wb.NS}Buckets', f'{wb.NS}Otags'} <= wb.ALLOWLIST_NAMES


@pytest.mark.parametrize('derived_field', sorted(_DERIVED_SCRYFALL_FIELDS))
def test_each_derived_scryfall_field_is_writable(derived_field: str) -> None:
    """Each newly-allowed derived Scryfall field passes the guard now."""
    wb.assert_no_human_fields([derived_field])
    assert derived_field in wb.ALLOWLIST_NAMES
    assert derived_field not in wb._human_denylist()


@pytest.mark.parametrize('human_field', sorted(_HUMAN_CARD_FIELDS))
def test_each_human_field_is_still_blocked(human_field: str) -> None:
    """Each genuinely human/collection field is still denied."""
    with pytest.raises(wb.HumanFieldWriteError):
        wb.assert_no_human_fields([human_field])
    assert human_field in wb._human_denylist()
    assert human_field not in wb.ALLOWLIST_NAMES


def test_allowlist_and_denylist_are_disjoint() -> None:
    """Structural invariant: engine-owned names never collide with human names."""
    assert not (wb.ALLOWLIST_NAMES & wb._human_denylist())


def test_allowlist_and_denylist_partition_the_card_fields() -> None:
    """The derived allowlist and the human denylist partition the classified
    Inventory Cards fields with no overlap and no unclassified derived field."""
    assert _DERIVED_SCRYFALL_FIELDS <= wb.ALLOWLIST_NAMES
    assert wb._human_denylist() == _HUMAN_CARD_FIELDS
    assert not (wb.ALLOWLIST_NAMES & wb._human_denylist())


def test_allowlist_disjoint_from_collection_owned_facts_writes() -> None:
    """The card-dim derived allowlist must not overlap the owned-facts write
    surface on the Inventory Cards table. If any field is written by both the
    derived and the owned-facts paths, that is a design conflict — fail here.

    The owned-facts Inventory Cards write surface is the OwnedCard fact columns its
    adapter creates/updates: Card Name (key), Number Owned, Foil Count, Condition,
    Sets, Sources (see AirtableCollectionStore.add_card / set_quantity / _set_owned_fields)."""
    from pipeline.collection.adapters.airtable_collection import AirtableCollectionStore as ACS

    sixs_inventory_writes = {
        ACS._INV_NAME,
        ACS._INV_OWNED,
        ACS._INV_FOIL,
        ACS._INV_CONDITION,
        ACS._INV_SETS,
        ACS._INV_SOURCES,
    }
    overlap = wb.ALLOWLIST_NAMES & sixs_inventory_writes
    assert not overlap, f'DESIGN CONFLICT: #5 derived allowlist overlaps #6 owned-facts writes: {sorted(overlap)}'


def test_denylist_loads_non_empty_with_card_name() -> None:
    """SAFETY: the human denylist MUST load non-empty and contain the primary
    Card field. An empty denylist would silently disable the write-back guard —
    which would happen if CARDS_TABLE_NAME stopped matching the golden-contract
    table key (e.g. after the 'Cards' -> 'Inventory Cards' rename). This asserts
    the key still resolves against the contract."""
    denylist = wb._human_denylist()
    assert denylist, 'human denylist loaded EMPTY — the write-back guard is disabled (REGRESSION)'
    assert 'Card Name' in denylist
    # The contract key the loader indexes by must be the live table name.
    assert wb.CARDS_TABLE_NAME == 'Inventory Cards'
    # And the lazy accessor must likewise be populated (cached, same object).
    assert wb._human_denylist()
    assert 'Card Name' in wb._human_denylist()


def test_denylist_is_the_cards_human_fields_from_golden_contract() -> None:
    """The denylist really comes from the golden contract's CARDS human fields,
    with the card-dim / engine-derived fields excluded."""
    denylist = wb.load_human_denylist()
    # The genuinely human/collection Card fields the writer must never touch.
    for f in (
        'Card Name',
        'Sets',
        'Number Owned',
        'Sources',
        'Condition',
        'Decks',
    ):
        assert f in denylist
    # The Scryfall-derived columns are no longer human-owned — they moved to
    # the engine allowlist and must not appear in the denylist.
    for f in (
        'Card Type',
        'Mana Cost',
        'CMC',
        'Power / Toughness',
        'Oracle Text',
        'Card Art',
        'Scryfall URL',
        'Price (TCGPlayer)',
        'Color Identity',
    ):
        assert f not in denylist
    # Deck-only human fields (Strategy/Commander/Owner/Format) are NOT Cards
    # fields, so they are not in this (Cards-scoped) denylist.
    assert 'Strategy' not in denylist
    assert 'Commander' not in denylist
    assert denylist == wb._human_denylist()


def test_target_table_is_cards_not_decks() -> None:
    """The retarget: the adapter targets the Cards table BY NAME (env-driven).

    The base/table/field ids are no longer hard-coded — the Cards table id is
    resolved at runtime from the meta schema by the env-configured table NAME.
    """
    from pipeline import config

    config.get_settings.cache_clear()
    client = wb.AllowlistWriteClient('tok')
    assert client._cards_table_name == config.get_settings().cards_table  # env-driven NAME
    assert client._cards_table_id is None  # not resolved until list_fields() runs
    assert not hasattr(wb, 'CARDS_TABLE_ID')  # no hard-coded id
    assert not hasattr(wb, 'DECKS_TABLE_ID')
    assert not hasattr(wb, 'BASE_ID')


# --------------------------------------------------------------------------- #
# Payload shape: (buckets, otags) -> exactly the two allowlisted fields.
# --------------------------------------------------------------------------- #


def test_build_payload_maps_the_two_derived_fields() -> None:
    payload = wb.build_payload(_resolution())
    # build_payload emits ONLY the two ⚙ otag fields (a subset of the allowlist);
    # the wider Scryfall derived columns are written by the dual-write path (5b),
    # not this otag payload builder.
    assert set(payload) == {f'{wb.NS}Buckets', f'{wb.NS}Otags'}
    assert set(payload) <= set(wb.ALLOWLIST_NAMES)
    # buckets -> a LIST (multipleSelects)
    assert payload[f'{wb.NS}Buckets'] == ['ramp', 'tutor']
    assert isinstance(payload[f'{wb.NS}Buckets'], list)
    # otags -> multiline TEXT
    assert payload[f'{wb.NS}Otags'] == 'ramp\ntutor'
    assert isinstance(payload[f'{wb.NS}Otags'], str)


def test_build_payload_only_contains_allowlist_never_human() -> None:
    payload = wb.build_payload(_resolution())
    assert not (set(payload) & wb._human_denylist())


def test_buckets_field_is_multiselect_with_full_crosswalk_options() -> None:
    """⚙ Buckets is created as multipleSelects seeded with the crosswalk vocab
    (incl. flagged gap buckets) so any bucket buckets_for emits is writable."""
    buckets_field = next(f for f in wb.DERIVED_FIELDS if f.name == f'{wb.NS}Buckets')
    assert buckets_field.airtable_type == 'multipleSelects'
    option_names = {c['name'] for c in buckets_field.options['choices']}
    assert option_names == set(wb.BUCKET_OPTIONS)
    # The flagged gap buckets are present in the option set.
    for gap in ('stax', 'extra_combat', 'wincon'):
        assert gap in option_names


def test_otags_field_is_multiline_text() -> None:
    otags_field = next(f for f in wb.DERIVED_FIELDS if f.name == f'{wb.NS}Otags')
    assert otags_field.airtable_type == 'multilineText'


# --------------------------------------------------------------------------- #
# Resolution + skip-on-no-match.
# --------------------------------------------------------------------------- #


def test_resolve_cards_joins_by_oracle_id() -> None:
    resolved, skipped = wb.resolve_cards(_CARDS, name_to_oracle_id=_NAME_TO_OID, card_otag=_CARD_OTAG)
    by_id = {r.record_id: r for r in resolved}
    assert set(by_id) == {'recCultivate', 'recSolRing'}
    assert by_id['recCultivate'].oracle_id == _OID_CULTIVATE
    assert by_id['recCultivate'].buckets == ('ramp', 'tutor')  # Cultivate multi-label
    assert by_id['recSolRing'].buckets == ('ramp',)
    assert skipped == []


def test_printing_annotation_name_normalizes_and_matches() -> None:
    """A card name carrying a trailing printing annotation (e.g.
    "Parallel Lives (Borderless)") normalizes to its oracle name and resolves,
    rather than dropping into `skipped`."""
    name_to_oid = {'Parallel Lives': 'oid-parallel-lives'}
    card_otag = {'oid-parallel-lives': {'tokens'}}
    cards = {'recPL': 'Parallel Lives (Borderless)'}
    resolved, skipped = wb.resolve_cards(cards, name_to_oracle_id=name_to_oid, card_otag=card_otag)
    assert skipped == []
    assert [r.oracle_id for r in resolved] == ['oid-parallel-lives']
    # The helper itself strips only the trailing parenthetical.
    assert wb._strip_printing_annotation('Parallel Lives (Borderless)') == 'Parallel Lives'
    assert wb._strip_printing_annotation('Sol Ring (Retro)') == 'Sol Ring'
    assert wb._strip_printing_annotation('Cultivate') == 'Cultivate'


def test_genuinely_unresolvable_name_still_skipped_after_normalization() -> None:
    """Normalization is a fallback only — a name that matches nothing even
    after stripping the annotation keeps the loud-skip behavior."""
    cards = {'recX': 'Nonexistent Card (Borderless)'}
    resolved, skipped = wb.resolve_cards(cards, name_to_oracle_id=_NAME_TO_OID, card_otag=_CARD_OTAG)
    assert resolved == []
    assert skipped == ['recX']


def test_card_with_unresolvable_oracle_id_is_skipped() -> None:
    """A card name with no oracle_id match is SKIPPED (logged), never guessed."""
    cards = {'recMystery': 'Totally Unknown Card'}
    resolved, skipped = wb.resolve_cards(cards, name_to_oracle_id=_NAME_TO_OID, card_otag=_CARD_OTAG)
    assert resolved == []
    assert skipped == ['recMystery']


def test_card_with_no_otag_data_is_skipped() -> None:
    """A resolvable card with an oracle_id but no otag rows has nothing to write."""
    cards = {'recUntagged': 'Some Untagged Card'}  # -> _OID_UNTAGGED, absent in card_otag
    resolved, skipped = wb.resolve_cards(cards, name_to_oracle_id=_NAME_TO_OID, card_otag=_CARD_OTAG)
    assert resolved == []
    assert skipped == ['recUntagged']


def test_skipped_cards_never_enter_the_write_plan() -> None:
    cards = dict(_CARDS)
    cards['recMystery'] = 'Totally Unknown Card'  # unresolvable -> must be skipped
    plan = wb.plan_writes(cards, name_to_oracle_id=_NAME_TO_OID, card_otag=_CARD_OTAG)
    assert [w.record_id for w in plan] == ['recCultivate', 'recSolRing']
    assert 'recMystery' not in {w.record_id for w in plan}


def test_push_report_records_skipped() -> None:
    cards = dict(_CARDS)
    cards['recMystery'] = 'Totally Unknown Card'
    report = _push(cards=cards)
    assert 'recMystery' in report.skipped
    assert {w.record_id for w in report.planned} == {'recCultivate', 'recSolRing'}


# --------------------------------------------------------------------------- #
# Idempotency.
# --------------------------------------------------------------------------- #


def test_build_payload_is_idempotent() -> None:
    r = _resolution()
    assert wb.build_payload(r) == wb.build_payload(r)


def test_plan_writes_is_idempotent_and_keyed_by_record_id() -> None:
    p1 = wb.plan_writes(_CARDS, name_to_oracle_id=_NAME_TO_OID, card_otag=_CARD_OTAG)
    p2 = wb.plan_writes(_CARDS, name_to_oracle_id=_NAME_TO_OID, card_otag=_CARD_OTAG)
    assert [w.record_id for w in p1] == [
        'recCultivate',
        'recSolRing',
    ]  # sorted, no dupes
    assert [(w.record_id, w.fields) for w in p1] == [(w.record_id, w.fields) for w in p2]


# --------------------------------------------------------------------------- #
# Dry-run issues ZERO write requests.
# --------------------------------------------------------------------------- #


def test_dry_run_issues_zero_writes_without_client() -> None:
    report = _push(dry_run=True)
    assert report.dry_run is True
    assert report.applied is False
    assert report.write_requests_issued == 0
    assert [w.record_id for w in report.planned] == ['recCultivate', 'recSolRing']


def test_dry_run_issues_zero_writes_even_with_spy_client() -> None:
    spy = SpyClient()
    report = _push(client=spy, dry_run=True)
    assert report.write_requests_issued == 0
    assert spy.write_methods == []  # no POST/PATCH/DELETE ever fired


def test_default_is_dry_run() -> None:
    """sync() with no flags must NOT write."""
    spy = SpyClient()
    report = _push(client=spy)
    assert report.dry_run is True
    assert spy.write_methods == []


def test_apply_alone_without_no_dry_run_still_dry() -> None:
    """apply=True but dry_run left True (default) => still a dry-run (belt+braces)."""
    spy = SpyClient()
    report = _push(client=spy, apply=True)  # dry_run defaults True
    assert report.write_requests_issued == 0
    assert spy.write_methods == []


def test_ensure_fields_dry_run_creates_nothing() -> None:
    spy = SpyClient(existing_fields={})  # nothing exists yet
    names = wb.ensure_fields(spy, dry_run=True)
    # ensure_fields only CREATES the two engine ⚙ fields (DERIVED_FIELDS); the
    # wider card-dim Scryfall columns already exist on the live table and are
    # never created by this adapter.
    assert set(names) == {f.name for f in wb.DERIVED_FIELDS}
    assert set(names) == {f'{wb.NS}Buckets', f'{wb.NS}Otags'}
    assert spy.write_methods == []  # but issued no create


# --------------------------------------------------------------------------- #
# Apply path (with the SPY only) — proves upsert mechanics + guard at the wire.
# --------------------------------------------------------------------------- #


def test_apply_with_spy_creates_fields_then_patches_by_field_id() -> None:
    spy = SpyClient(existing_fields={})
    report = _push(client=spy, dry_run=False, apply=True)
    assert report.applied is True
    assert report.write_requests_issued == 1
    # Both derived fields created, then one PATCH.
    assert sum(1 for m in spy.methods if m.startswith('POST:create_field')) == 2
    assert 'PATCH:patch_records' in spy.methods
    # The patch is keyed by FIELD ID, and every field id maps back to an allowlist name.
    patched = spy.patched
    assert {p['id'] for p in patched} == {'recCultivate', 'recSolRing'}
    name_by_id = {v: k for k, v in spy.list_fields().items()}
    for rec in patched:
        for fid in rec['fields']:
            assert name_by_id[fid] in wb.ALLOWLIST_NAMES


def test_apply_reruns_are_idempotent_no_new_fields() -> None:
    """Second apply with fields already present creates no new fields (no thrash)."""
    existing = {f.name: f'fld_{i}' for i, f in enumerate(wb.DERIVED_FIELDS)}
    spy = SpyClient(existing_fields=existing)
    _push(client=spy, dry_run=False, apply=True)
    assert sum(1 for m in spy.methods if m.startswith('POST:create_field')) == 0
    assert spy.methods.count('PATCH:patch_records') == 1


def test_live_push_requires_client() -> None:
    with pytest.raises(ValueError, match='requires a client'):
        _push(dry_run=False, apply=True)


# --------------------------------------------------------------------------- #
# The real AllowlistWriteClient guards the wire (offline, mocked transport).
# --------------------------------------------------------------------------- #


def test_real_client_create_field_refuses_human_name() -> None:
    client = wb.AllowlistWriteClient('tok')
    with pytest.raises(wb.NonAllowlistFieldError):
        client.create_field('Card Name', 'singleLineText', None)


def test_real_client_patch_refuses_human_field_at_wire() -> None:
    """Even if a caller hand-builds a record with a human field, patch_records
    RAISES before issuing the request."""
    client = wb.AllowlistWriteClient('tok')
    with pytest.raises((wb.HumanFieldWriteError, wb.NonAllowlistFieldError)):
        client.patch_records([{'id': 'recX', 'fields': {'Oracle Text': 'clobber'}}])


# --------------------------------------------------------------------------- #
# The wire guard validates a FIELD-ID-keyed body (the real apply path). By the
# wire the payload is keyed by Airtable field IDs, not names — the guard must
# resolve the allowlist name->id map into allowed_ids / id_to_name and guard by
# id, so a real --no-dry-run --apply does not crash on its OWN derived field ids.
# --------------------------------------------------------------------------- #

#: A realistic resolved schema map: the two derived fields plus a human field.
_NAME_TO_ID_REAL = {
    f'{wb.NS}Buckets': 'fldAAA',
    f'{wb.NS}Otags': 'fldBBB',
    'Card Name': 'fldHUMAN',
}


def _client_with_field_map() -> wb.AllowlistWriteClient:
    """Real client with its allowlist name->id map resolved (allowed_ids built)."""
    client = wb.AllowlistWriteClient('tok')
    client.resolve_field_map(_NAME_TO_ID_REAL)
    return client


def test_wire_guard_passes_id_keyed_body_of_derived_field_ids() -> None:
    """A body keyed by the two DERIVED field ids passes the wire guard (this is
    exactly the shape a real apply produces — must NOT crash)."""
    client = _client_with_field_map()
    records = [
        {
            'id': 'recCultivate',
            'fields': {'fldAAA': ['ramp', 'tutor'], 'fldBBB': 'ramp\ntutor'},
        },
        {'id': 'recSolRing', 'fields': {'fldAAA': ['ramp'], 'fldBBB': 'ramp'}},
    ]
    # Must not raise: these are the client's OWN legitimately-produced field ids.
    client._guarded_body(records)


def test_wire_guard_rejects_human_field_id() -> None:
    """A body carrying a HUMAN field id (fldHUMAN) is rejected at the wire."""
    client = _client_with_field_map()
    with pytest.raises((wb.HumanFieldWriteError, wb.NonAllowlistFieldError)):
        client._guarded_body([{'id': 'recX', 'fields': {'fldHUMAN': 'clobber'}}])


def test_wire_guard_rejects_unknown_field_id() -> None:
    """A body carrying an UNKNOWN id (no mapping at all) is rejected at the wire."""
    client = _client_with_field_map()
    with pytest.raises((wb.HumanFieldWriteError, wb.NonAllowlistFieldError)):
        client._guarded_body([{'id': 'recX', 'fields': {'fldZZZ': 'mystery'}}])


def test_wire_guard_rejects_derived_ids_mixed_with_human_id() -> None:
    """Legit derived ids mixed with a single human id are still rejected."""
    client = _client_with_field_map()
    with pytest.raises((wb.HumanFieldWriteError, wb.NonAllowlistFieldError)):
        client._guarded_body([{'id': 'recX', 'fields': {'fldAAA': ['ramp'], 'fldHUMAN': 'clobber'}}])


def test_wire_guard_derives_allowed_ids_and_id_to_name() -> None:
    """The client derives allowed_ids (the two derived field ids) and id_to_name
    (reverse map) from the resolved name->id map — the human id is NOT allowed."""
    client = _client_with_field_map()
    assert client._allowed_ids == {'fldAAA', 'fldBBB'}
    # id_to_name is the reverse map (id -> name), restricted to allowed ids.
    assert client._id_to_name == {
        'fldAAA': f'{wb.NS}Buckets',
        'fldBBB': f'{wb.NS}Otags',
    }
    assert 'fldHUMAN' not in client._allowed_ids


def test_client_resolves_cards_table_by_name_and_env_base_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client resolves the Cards table id BY NAME and uses the env base id —
    proving the identity is env-driven / runtime-resolved, not hard-coded."""
    monkeypatch.setenv('AIRTABLE_BASE_ID', 'appOTHER')
    monkeypatch.setenv('AIRTABLE_CARDS_TABLE', 'Inventory Cards')
    config.get_settings.cache_clear()

    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                'tables': [
                    {'id': 'tblOTHER', 'name': 'Some Other Table', 'fields': []},
                    {
                        'id': 'tblINVENTORY',
                        'name': 'Inventory Cards',  # matches the env override
                        # Carries the human Card fields so it structurally is the
                        # Cards table, plus a derived field.
                        'fields': [{'name': f'{wb.NS}Buckets', 'id': 'fldB'}, *_cards_table_fields()[2:]],
                    },
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = wb.AllowlistWriteClient('tok', _client=httpx.Client(transport=transport))
    name_to_id = client.list_fields()
    assert name_to_id[f'{wb.NS}Buckets'] == 'fldB'
    assert 'Card Name' in name_to_id  # the resolved table is really the Cards table
    assert client._cards_table_id == 'tblINVENTORY'  # resolved by NAME
    # The meta URL used the ENV base id, not the old hard-coded appw7QPMoqktrgDc1.
    assert any('/meta/bases/appOTHER/tables' in u for u in seen_urls)


def test_client_raises_clear_error_when_cards_table_name_absent() -> None:
    """A base missing the configured Cards table NAME fails LOUDLY, not silently."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={'tables': [{'id': 'tblX', 'name': 'Not Cards', 'fields': []}]})

    transport = httpx.MockTransport(handler)
    client = wb.AllowlistWriteClient('tok', _client=httpx.Client(transport=transport))
    with pytest.raises(config.AirtableConfigError, match="'Inventory Cards' not found"):
        client.list_fields()


#: A meta payload whose resolved Cards table has the human Card fields present —
#: i.e. it structurally IS the inventory Cards table (the denylist is a subset).
def _cards_table_fields() -> list[dict[str, str]]:
    fields = [{'name': f'{wb.NS}Buckets', 'id': 'fldB'}, {'name': f'{wb.NS}Otags', 'id': 'fldO'}]
    fields += [{'name': human, 'id': f'fldH{i}'} for i, human in enumerate(sorted(wb._human_denylist()))]
    return fields


def test_list_fields_accepts_real_cards_table() -> None:
    """A resolved table that contains the human Card fields (structurally
    the inventory Cards table) passes the wrong-table binding check."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={'tables': [{'id': 'tblCARDS', 'name': 'Inventory Cards', 'fields': _cards_table_fields()}]},
        )

    transport = httpx.MockTransport(handler)
    client = wb.AllowlistWriteClient('tok', _client=httpx.Client(transport=transport))
    name_to_id = client.list_fields()  # must NOT raise
    assert 'Card Name' in name_to_id
    assert client._cards_table_id == 'tblCARDS'


def test_list_fields_rejects_wrong_table_missing_card_fields() -> None:
    """A misconfigured AIRTABLE_CARDS_TABLE that resolves the wrong table
    (e.g. Decks, which lacks the Card human fields) is refused with a clear
    AirtableConfigError, before any create_field/PATCH can litter that table."""

    def handler(request: httpx.Request) -> httpx.Response:
        # A 'Decks'-shaped table: it has none of the Cards human fields, so the
        # denylist is not a subset -> the write target is not the Cards table.
        return httpx.Response(
            200,
            json={
                'tables': [
                    {
                        'id': 'tblDECKS',
                        'name': 'Inventory Cards',  # name matches, but structure does not
                        'fields': [
                            {'name': 'Deck Name', 'id': 'fldD0'},
                            {'name': 'Commander', 'id': 'fldD1'},
                            {'name': f'{wb.NS}Buckets', 'id': 'fldB'},
                        ],
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = wb.AllowlistWriteClient('tok', _client=httpx.Client(transport=transport))
    with pytest.raises(config.AirtableConfigError, match='not the inventory Cards table'):
        client.list_fields()


def test_import_is_side_effect_free_when_contract_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Importing the module (already imported) must not depend on the
    filesystem. Point the lazy loader at a missing path and prove:
      - re-importing / accessing module attrs does not crash;
      - the first denylist access raises a clear runtime error (fail-closed)."""
    import importlib

    missing = tmp_path / 'nope' / 'golden_contract.json'
    # Reload the module fresh with a patched path resolver to prove import itself
    # performs NO filesystem read / assert even when the contract is absent.
    monkeypatch.setattr(wb, '_golden_contract_path', lambda: missing)
    wb._human_denylist.cache_clear()
    # Import (reload) succeeds with the contract absent — no import-time crash.
    reloaded = importlib.reload(wb)
    # But the FIRST real use fails loudly (missing file).
    monkeypatch.setattr(reloaded, '_golden_contract_path', lambda: missing)
    reloaded._human_denylist.cache_clear()
    with pytest.raises(FileNotFoundError):
        reloaded._human_denylist()
    # Restore a clean module state for subsequent tests.
    importlib.reload(wb)
    wb._human_denylist.cache_clear()


def test_lazy_loader_asserts_fire_at_first_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """The non-empty + disjointness asserts live in the lazy loader and
    fail-close on first use (not at import)."""
    # Empty denylist -> the non-empty assert must fire.
    monkeypatch.setattr(wb, 'load_human_denylist', lambda: frozenset())
    wb._human_denylist.cache_clear()
    with pytest.raises(AssertionError, match='denylist is empty'):
        wb._human_denylist()
    wb._human_denylist.cache_clear()

    # A denylist that OVERLAPS the allowlist -> the disjointness assert must fire.
    overlapping = frozenset({next(iter(wb.ALLOWLIST_NAMES))})
    monkeypatch.setattr(wb, 'load_human_denylist', lambda: overlapping)
    wb._human_denylist.cache_clear()
    with pytest.raises(AssertionError, match='overlaps the human denylist'):
        wb._human_denylist()
    wb._human_denylist.cache_clear()


def test_real_client_full_apply_does_not_crash_on_derived_field_ids() -> None:
    """End-to-end on the real client (mocked transport): a live apply resolves
    the schema (name->id), re-keys the payload by field id, and PATCHes — the
    wire guard must pass its own derived ids. This is the exact --no-dry-run
    --apply path."""
    buckets_id = 'fldk4CO3LsxEi2dgH'  # realistic Airtable fld... ids
    otags_id = 'fldZa9Q0oQ0qkm2Ab'
    patched_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == 'GET':  # meta schema read
            return httpx.Response(
                200,
                json={
                    'tables': [
                        {
                            'id': 'tblCARDSresolved',
                            'name': 'Inventory Cards',  # resolved by NAME (env-driven default)
                            'fields': [
                                {'name': f'{wb.NS}Buckets', 'id': buckets_id},
                                {'name': f'{wb.NS}Otags', 'id': otags_id},
                                # All the human Card fields so the resolved table
                                # structurally is the Cards table.
                                *[
                                    {'name': human, 'id': f'fldHUMAN{i}'}
                                    for i, human in enumerate(sorted(wb._human_denylist()))
                                ],
                            ],
                        }
                    ]
                },
            )
        # PATCH records — capture the id-keyed body that left the process.
        patched_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'records': []})

    transport = httpx.MockTransport(handler)
    client = wb.AllowlistWriteClient('tok', _client=httpx.Client(transport=transport))
    report = wb.sync(
        _CARDS,
        name_to_oracle_id=_NAME_TO_OID,
        card_otag=_CARD_OTAG,
        client=client,
        dry_run=False,
        apply=True,
    )
    assert report.applied is True
    assert report.write_requests_issued == 1
    # The wire body is keyed by the REAL derived field ids and passed the guard.
    assert len(patched_bodies) == 1
    for rec in patched_bodies[0]['records']:
        assert set(rec['fields']).issubset({buckets_id, otags_id})


# --------------------------------------------------------------------------- #
# Chunking: PATCH requests are batched to Airtable's 10-records-per-request cap.
# --------------------------------------------------------------------------- #


def _many_cards(n: int) -> tuple[dict[str, str], dict[str, str], dict[str, set[str]]]:
    """Build n resolvable cards with distinct record ids / oracle_ids / otags."""
    cards: dict[str, str] = {}
    name_to_oid: dict[str, str] = {}
    card_otag: dict[str, set[str]] = {}
    for i in range(n):
        name = f'Card {i:03d}'
        rid = f'rec{i:03d}'
        oid = f'oid-{i:03d}'
        cards[rid] = name
        name_to_oid[name] = oid
        card_otag[oid] = {'ramp'}
    return cards, name_to_oid, card_otag


class GuardSpyClient(SpyClient):
    """Spy that records each chunk it is handed and can simulate an HTTP error on
    the Nth (1-based) patch call to exercise the stop-and-report-partial path."""

    def __init__(
        self,
        existing_fields: dict[str, str] | None = None,
        *,
        fail_on_chunk: int | None = None,
    ) -> None:
        super().__init__(existing_fields)
        self.fail_on_chunk = fail_on_chunk

    def patch_records(self, records: list[dict[str, Any]]) -> Any:
        call_index = self.methods.count('PATCH:patch_records') + 1
        if self.fail_on_chunk is not None and call_index == self.fail_on_chunk:
            self.methods.append('PATCH:patch_records')
            raise httpx.HTTPStatusError(
                'simulated 429',
                request=None,
                response=None,  # type: ignore[arg-type]
            )
        return super().patch_records(records)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep chunking tests fast: neutralize the inter-chunk politeness delay."""
    monkeypatch.setattr(wb, 'INTER_CHUNK_DELAY_S', 0)


def test_apply_chunks_25_writes_into_three_patches_of_10_10_5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cards, name_to_oid, card_otag = _many_cards(25)
    existing = {f.name: f'fld_{i}' for i, f in enumerate(wb.DERIVED_FIELDS)}
    spy = GuardSpyClient(existing_fields=existing)

    # Spy on the human-field guard to prove it runs for EVERY chunk's payloads.
    guard_calls: list[frozenset[str]] = []
    real_guard = wb.assert_no_human_fields

    def _tracking_guard(payload_fields: Any) -> None:
        guard_calls.append(frozenset(payload_fields))
        return real_guard(payload_fields)

    monkeypatch.setattr(wb, 'assert_no_human_fields', _tracking_guard)

    report = wb.sync(
        cards,
        name_to_oracle_id=name_to_oid,
        card_otag=card_otag,
        client=spy,
        dry_run=False,
        apply=True,
    )
    assert [len(c) for c in spy.patched_chunks] == [10, 10, 5]
    assert report.write_requests_issued == 3
    # The guard fired at least once per record across the 3 chunks (defense runs
    # per chunk before each PATCH leaves the process). Each per-record payload is
    # the two ⚙ otag fields (a subset of the wider allowlist), so the guard is
    # invoked with EXACTLY that payload field set per record.
    payload_fields = frozenset({f'{wb.NS}Buckets', f'{wb.NS}Otags'})
    assert payload_fields <= frozenset(wb.ALLOWLIST_NAMES)
    per_record_guards = [g for g in guard_calls if g == payload_fields]
    assert len(per_record_guards) >= 25
    # Every record patched exactly once, keyed by record id, no dupes.
    all_ids = [rec['id'] for chunk in spy.patched_chunks for rec in chunk]
    assert len(all_ids) == 25
    assert len(set(all_ids)) == 25


def test_chunk_size_never_exceeds_ten() -> None:
    cards, name_to_oid, card_otag = _many_cards(23)
    existing = {f.name: f'fld_{i}' for i, f in enumerate(wb.DERIVED_FIELDS)}
    spy = GuardSpyClient(existing_fields=existing)
    wb.sync(
        cards,
        name_to_oracle_id=name_to_oid,
        card_otag=card_otag,
        client=spy,
        dry_run=False,
        apply=True,
    )
    assert all(len(c) <= 10 for c in spy.patched_chunks)
    assert sum(len(c) for c in spy.patched_chunks) == 23


def test_dry_run_reports_chunk_count_and_issues_zero_writes() -> None:
    cards, name_to_oid, card_otag = _many_cards(25)
    spy = GuardSpyClient()
    report = wb.sync(
        cards,
        name_to_oracle_id=name_to_oid,
        card_otag=card_otag,
        client=spy,
        dry_run=True,
    )
    assert report.write_requests_issued == 0
    assert spy.write_methods == []
    # It reports the number of chunks it WOULD send: 25 -> 3.
    assert report.chunks_planned == 3


def test_chunk_error_stops_and_reports_partial_progress() -> None:
    cards, name_to_oid, card_otag = _many_cards(25)
    existing = {f.name: f'fld_{i}' for i, f in enumerate(wb.DERIVED_FIELDS)}
    # Fail on the 2nd chunk: chunk 1 succeeds, chunk 2 errors, chunk 3 never sent.
    spy = GuardSpyClient(existing_fields=existing, fail_on_chunk=2)
    with pytest.raises(wb.ChunkWriteError) as excinfo:
        wb.sync(
            cards,
            name_to_oracle_id=name_to_oid,
            card_otag=card_otag,
            client=spy,
            dry_run=False,
            apply=True,
        )
    # Partial progress is surfaced: chunk 1 completed before chunk 2 blew up.
    err = excinfo.value
    assert err.chunks_completed == 1
    assert err.chunks_planned == 3
    assert isinstance(err.__cause__, httpx.HTTPStatusError)  # original error chained
    # Only chunk 1 was actually recorded as complete; chunk 3 never fired.
    assert len(spy.patched_chunks) == 1
    assert spy.methods.count('PATCH:patch_records') == 2  # attempted 1 ok + 1 failed


# --------------------------------------------------------------------------- #
# The destination CLI dry-run plan must not be a vacuous safety preview.
#
# A dry-run with a credential must resolve the real card count (by reading the
# Card Name field-ID column keyed to the meta-resolved field id) and report only
# the ⚙ fields that DON'T yet exist as "would create" — via a READ-ONLY meta
# call, regardless of do_write. A dry-run with NO credential must report the plan
# as "unknown (no API access...)" rather than a misleading 0-resolved / all-would-
# create claim. No writes issue in either case.
# --------------------------------------------------------------------------- #


class MetaOnlyClient:
    """Read-only stand-in for AllowlistWriteClient: serves a fixed field schema
    via list_fields() and records ZERO write-shaped calls. A dry-run must only
    ever call list_fields() (a GET) on it."""

    def __init__(self, fields: dict[str, str]) -> None:
        self._fields = dict(fields)
        self.methods: list[str] = []
        self.closed = False

    def list_fields(self) -> dict[str, str]:
        self.methods.append('GET:list_fields')
        return dict(self._fields)

    def create_field(self, name: str, airtable_type: str, options: dict[str, Any] | None) -> None:  # pragma: no cover
        self.methods.append(f'POST:create_field:{name}')

    def patch_records(self, records: list[dict[str, Any]]) -> Any:  # pragma: no cover
        self.methods.append('PATCH:patch_records')
        return None

    def close(self) -> None:
        self.closed = True

    @property
    def write_methods(self) -> list[str]:
        return [m for m in self.methods if m.split(':', 1)[0] in {'POST', 'PATCH', 'PUT', 'DELETE'}]


def test_dry_run_with_credential_resolves_real_cards_and_only_missing_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--dry-run` with AIRTABLE_API_KEY set does a read-only meta resolution
    (resolves the Card Name field id + reads the real schema), so the plan shows
    the real resolved card count and reports as "would create" only the ⚙ field
    that does not yet exist — never a card count of 0, never a bogus creation of
    fields that already exist. No writes fire."""
    monkeypatch.setenv('AIRTABLE_API_KEY', 'tok')
    # The live schema: Card Name exists, ⚙ Buckets ALREADY exists, ⚙ Otags does NOT.
    card_name_fid = 'fldCARDNAME'
    schema = {'Card Name': card_name_fid, f'{wb.NS}Buckets': 'fldBUCKETS'}
    client = MetaOnlyClient(schema)
    monkeypatch.setattr(wb, 'AllowlistWriteClient', lambda *a, **k: client)

    # A populated lake: two resolvable cards keyed by the Card Name field id.
    captured_fid: dict[str, str | None] = {}

    def fake_load(card_name_field_id: str | None = None):
        captured_fid['fid'] = card_name_field_id
        return _CARDS, _NAME_TO_OID, _CARD_OTAG

    monkeypatch.setattr(wb, '_load_cards_from_lake', fake_load)

    printed: list[str] = []
    monkeypatch.setattr('builtins.print', lambda s='': printed.append(s))

    rc = wb.main(['--dry-run'])
    assert rc == 0
    # The Card Name field id was resolved from the meta schema and handed to the loader.
    assert captured_fid['fid'] == card_name_fid
    # The lake resolved the REAL cards, not an empty map.
    out = '\n'.join(printed)
    assert 'cards resolved: 2' in out
    # Only ⚙ Otags is reported as "would create" — ⚙ Buckets already exists.
    assert f'{wb.NS}Otags' in out
    assert 'Fields that WOULD be created' in out
    assert f'{wb.NS}Buckets' not in out.split('Fields that WOULD be created')[1].split('\n')[0]
    # READ-ONLY: no write-shaped call ever fired, and the client was closed.
    assert client.write_methods == []
    assert client.closed is True


def test_dry_run_with_credential_reports_none_to_create_when_both_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both ⚙ fields already exist, a credentialed dry-run reports no
    fields would be created (not a bogus creation claim)."""
    monkeypatch.setenv('AIRTABLE_API_KEY', 'tok')
    schema = {
        'Card Name': 'fldCARDNAME',
        f'{wb.NS}Buckets': 'fldBUCKETS',
        f'{wb.NS}Otags': 'fldOTAGS',
    }
    client = MetaOnlyClient(schema)
    monkeypatch.setattr(wb, 'AllowlistWriteClient', lambda *a, **k: client)
    monkeypatch.setattr(wb, '_load_cards_from_lake', lambda card_name_field_id=None: (_CARDS, _NAME_TO_OID, _CARD_OTAG))

    printed: list[str] = []
    monkeypatch.setattr('builtins.print', lambda s='': printed.append(s))

    rc = wb.main(['--dry-run'])
    assert rc == 0
    out = '\n'.join(printed)
    assert 'cards resolved: 2' in out
    assert 'Fields that WOULD be created' not in out  # both already exist
    assert client.write_methods == []


def test_dry_run_without_credential_reports_unknown_not_vacuous_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An offline dry-run (no AIRTABLE_API_KEY) can't read the field-ID-keyed
    lake or the schema, so it must not claim `cards resolved: 0` or that fields
    "WOULD be created". It reports the plan is unknown pending API access."""
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)

    # Even if the loader is called with no field id, it can't resolve names.
    monkeypatch.setattr(wb, '_load_cards_from_lake', lambda card_name_field_id=None: ({}, {}, {}))
    # AllowlistWriteClient must NEVER be constructed without a credential.
    monkeypatch.setattr(
        wb,
        'AllowlistWriteClient',
        lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not build a client without a credential')),
    )

    printed: list[str] = []
    monkeypatch.setattr('builtins.print', lambda s='': printed.append(s))

    rc = wb.main(['--dry-run'])
    assert rc == 0
    out = '\n'.join(printed)
    # It does NOT print a misleading concrete plan.
    assert 'cards resolved: 0' not in out
    assert 'Fields that WOULD be created' not in out
    # It DOES say the plan is unknown / needs API access.
    assert 'unknown' in out.lower()
    assert 'AIRTABLE_API_KEY' in out
