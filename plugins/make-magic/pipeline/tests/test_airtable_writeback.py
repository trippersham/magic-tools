"""OFFLINE tests for the per-CARD derived write-back adapter (Phase 5, retargeted).

The engine now writes TWO fields onto the *Cards* table (``⚙ Buckets`` +
``⚙ Otags``) — NOT the Decks table. The safety architecture is unchanged; only
the target and the payload changed.

The core safety proofs:
    - The guard REFUSES any payload containing a human-edited CARD field
      (parametrized over EVERY Cards human field enumerated in the golden
      contract) and any non-allowlisted field.
    - A dry-run issues ZERO write requests (the client is a spy; no
      POST/PATCH/DELETE is recorded).
    - Payload maps a card's (buckets, otags) -> exactly the two allowlisted
      fields; buckets as a list, otags as multiline text.
    - Idempotency: the same input twice yields the same intended plan.
    - Cards whose oracle_id does not robustly resolve are SKIPPED, not written.

No network: the only "client" is an in-memory spy that records every method it is
asked to perform. In dry-run the client is never even constructed.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pipeline.adapters import airtable_writeback as wb

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
    """Call push() with the standard offline join maps (override as needed)."""
    kwargs: dict[str, Any] = {
        'cards': _CARDS,
        'name_to_oracle_id': _NAME_TO_OID,
        'card_otag': _CARD_OTAG,
    }
    kwargs.update(overrides)
    return wb.push(**kwargs)


# --------------------------------------------------------------------------- #
# Guard: refuses human CARD fields (parametrized over the golden-contract Cards).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize('human_field', sorted(wb.HUMAN_DENYLIST))
def test_guard_refuses_every_human_card_field(human_field: str) -> None:
    """assert_no_human_fields RAISES for EACH Cards human field in the contract."""
    with pytest.raises(wb.HumanFieldWriteError):
        wb.assert_no_human_fields([human_field])


@pytest.mark.parametrize('human_field', sorted(wb.HUMAN_DENYLIST))
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


def test_allowlist_is_exactly_the_two_card_fields() -> None:
    """The allowlist is EXACTLY the two engine-owned Cards fields."""
    assert {f'{wb.NS}Buckets', f'{wb.NS}Otags'} == wb.ALLOWLIST_NAMES


def test_allowlist_and_denylist_are_disjoint() -> None:
    """Structural invariant: engine-owned names never collide with human names."""
    assert not (wb.ALLOWLIST_NAMES & wb.HUMAN_DENYLIST)


def test_denylist_is_the_cards_human_fields_from_golden_contract() -> None:
    """The denylist really comes from the golden contract's CARDS fields."""
    denylist = wb.load_human_denylist()
    # Spot-check the marquee human Card fields the writer must never touch.
    for f in (
        'Card Name',
        'Sets',
        'Number Owned',
        'Sources',
        'Condition',
        'Card Type',
        'Mana Cost',
        'CMC',
        'Power / Toughness',
        'Oracle Text',
        'Scryfall URL',
        'Price (TCGPlayer)',
        'Color Identity',
        'Decks',
    ):
        assert f in denylist
    # Deck-only human fields (Strategy/Commander/Owner/Format) are NOT Cards
    # fields, so they are not in this (Cards-scoped) denylist.
    assert 'Strategy' not in denylist
    assert 'Commander' not in denylist
    assert denylist == wb.HUMAN_DENYLIST


def test_target_table_is_cards_not_decks() -> None:
    """The retarget: the adapter now points at the Cards table id."""
    assert wb.CARDS_TABLE_ID == 'tbl3UgZZPJGQhEFo8'
    assert not hasattr(wb, 'DECKS_TABLE_ID')


# --------------------------------------------------------------------------- #
# Payload shape: (buckets, otags) -> exactly the two allowlisted fields.
# --------------------------------------------------------------------------- #


def test_build_payload_maps_the_two_derived_fields() -> None:
    payload = wb.build_payload(_resolution())
    assert set(payload) == set(wb.ALLOWLIST_NAMES)
    # buckets -> a LIST (multipleSelects)
    assert payload[f'{wb.NS}Buckets'] == ['ramp', 'tutor']
    assert isinstance(payload[f'{wb.NS}Buckets'], list)
    # otags -> multiline TEXT
    assert payload[f'{wb.NS}Otags'] == 'ramp\ntutor'
    assert isinstance(payload[f'{wb.NS}Otags'], str)


def test_build_payload_only_contains_allowlist_never_human() -> None:
    payload = wb.build_payload(_resolution())
    assert not (set(payload) & wb.HUMAN_DENYLIST)


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
    """push() with no flags must NOT write."""
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
    assert set(names) == set(wb.ALLOWLIST_NAMES)  # would create both
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


def test_real_client_full_apply_does_not_crash_on_derived_field_ids() -> None:
    """END-TO-END on the REAL client (mocked transport): a live apply resolves
    the schema (name->id), re-keys the payload by FIELD ID, and PATCHes — the
    wire guard must PASS its own derived ids. This is the exact --no-dry-run
    --apply path that would previously CRASH with NonAllowlistFieldError."""
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
                            'id': wb.CARDS_TABLE_ID,
                            'fields': [
                                {'name': f'{wb.NS}Buckets', 'id': buckets_id},
                                {'name': f'{wb.NS}Otags', 'id': otags_id},
                                {'name': 'Card Name', 'id': 'fldHUMANxxxxxxxx'},
                                {'name': 'Oracle Text', 'id': 'fldHUMAN2xxxxxxx'},
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
    report = wb.push(
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

    report = wb.push(
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
    # per chunk before each PATCH leaves the process).
    allow = frozenset(wb.ALLOWLIST_NAMES)
    per_record_guards = [g for g in guard_calls if g == allow]
    assert len(per_record_guards) >= 25
    # Every record patched exactly once, keyed by record id, no dupes.
    all_ids = [rec['id'] for chunk in spy.patched_chunks for rec in chunk]
    assert len(all_ids) == 25
    assert len(set(all_ids)) == 25


def test_chunk_size_never_exceeds_ten() -> None:
    cards, name_to_oid, card_otag = _many_cards(23)
    existing = {f.name: f'fld_{i}' for i, f in enumerate(wb.DERIVED_FIELDS)}
    spy = GuardSpyClient(existing_fields=existing)
    wb.push(
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
    report = wb.push(
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
        wb.push(
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
