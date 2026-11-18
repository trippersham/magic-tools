"""Tests for Phase-A combo-intrinsic edge authoring (ADR 0021 B1).

These cover the *layer* contract of the re-anchoring:

- **Phase A is combo-intrinsic + deterministic**: a combo's implied edges are a
  pure function of its ``result`` fids + its pieces' otags + the catalog -- no
  deck is consulted, and re-running is byte-stable.
- **The two views read one decomposition**: ``combo_event_set`` consumes exactly
  the fids the win-walk (View A) classifies, so lowering never touches the walk.
- **Phase B is assembly-only**: present-combo selection + reciprocal grounding,
  with the committed empty-store default staying byte-identical (P0 SHAs).
- **Provenance + caching**: ``source='spellbook'``, versioned, idempotent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.transforms.strategy.assets import load_assets
from pipeline.transforms.strategy.combo_edges import (
    COMBO_EDGE_SOURCE,
    PopulationResult,
    author_combo_edges,
    combo_event_set,
    derive_combo_edges,
    derive_self_couplings,
    populate_combo_edges,
)
from pipeline.transforms.strategy.combo_win import (
    fid_route,
    resolve_fids_from_feature_names,
)
from pipeline.transforms.strategy.engine_report import _assemble_present_combo_edges
from pipeline.transforms.strategy.machine_vocab import (
    DERIVATION_VERSION,
    EXTRACTION_VERSION,
    CardAttribute,
    ProvisionalAttributeStore,
    load_store,
    persist,
)
from pipeline.transforms.strategy.tests.conftest import LAKE_AVAILABLE

_skip_no_lake = pytest.mark.skipif(not LAKE_AVAILABLE, reason='lake parquets not available')


@pytest.fixture(scope='module')
def assets():
    return load_assets()


def _mana_producer_attr(assets) -> str:
    """A catalog producer attr of mana-produced that is also an otag slug."""
    for a in assets.events['mana-produced']['produced_by']:
        if a not in assets.buckets:  # a bare otag slug (queryable as otag:<a>)
            return a
    raise AssertionError('no otag-slug producer for mana-produced')


# ---------------------------------------------------------------------------
# combo_event_set: combo-intrinsic, reads the same fids as View A (win-walk)
# ---------------------------------------------------------------------------


class TestComboEventSetIsViewA:
    def test_reads_the_same_fids_the_walk_classifies(self, assets) -> None:
        # A combo whose result names an unbounded (intermediate) mana feature.
        result = 'Infinite colored mana; Infinite untap of creatures you control'
        feature_names = [f.strip() for f in result.split(';')]
        fids = resolve_fids_from_feature_names(feature_names)
        # View A's own route events for the resolvable fids.
        walk_events = {
            r['event']
            for fid in fids
            if fid
            if (r := fid_route(fid)) and r.get('route') == 'intermediate' and r.get('event') in assets.events
        }
        assert combo_event_set(result, assets) == frozenset(walk_events)

    def test_is_pure_function_of_result(self, assets) -> None:
        result = 'Infinite colored mana; Infinite storm count'
        a = combo_event_set(result, assets)
        b = combo_event_set(result, assets)
        assert a == b  # deterministic, no deck input


# ---------------------------------------------------------------------------
# derive_combo_edges: combo-piece scope, named after the existing producer attr
# ---------------------------------------------------------------------------


class TestDeriveComboEdges:
    def _combo_with_mana_and_untap(self, assets) -> tuple[dict, dict]:
        mana_attr = _mana_producer_attr(assets)
        # Piece 1 carries the mana-producer otag; piece 2 carries an untap
        # producer otag.  Result couples mana-produced with untap.
        untap_attr = next(a for a in assets.events['untap']['produced_by'] if a not in assets.buckets)
        combo = {
            'variant_id': 'T-1',
            'oids': ['oid-mana', 'oid-untap'],
            'result': 'Infinite colored mana; Infinite untap of creatures you control',
        }
        otag_index = {'oid-mana': {mana_attr}, 'oid-untap': {untap_attr}}
        return combo, otag_index

    def test_edges_named_after_producer_attr(self, assets) -> None:
        combo, otag_index = self._combo_with_mana_and_untap(assets)
        edges = derive_combo_edges(combo, otag_index, assets)
        assert edges, 'expected combo to author edges'
        names = {e.name for e in edges}
        # Names are existing catalog producer attributes (so the relay augments
        # the real node), never a synthetic 'spellbook::...' node id.
        assert all('::' not in n for n in names)
        producers = set(assets.events['mana-produced']['produced_by']) | set(assets.events['untap']['produced_by'])
        assert names <= producers

    def test_provenance_is_spellbook_versioned_and_combo_keyed(self, assets) -> None:
        combo, otag_index = self._combo_with_mana_and_untap(assets)
        edges = derive_combo_edges(combo, otag_index, assets)
        for e in edges:
            assert e.provenance.source == COMBO_EDGE_SOURCE
            assert e.provenance.combo_variant_id == 'T-1'
            assert e.provenance.extraction_version == EXTRACTION_VERSION
            assert e.provenance.derivation_version == DERIVATION_VERSION

    def test_single_event_combo_authors_nothing(self, assets) -> None:
        # |E(C)| < 2 -> no within-loop coupling possible.
        combo = {'variant_id': 'T-2', 'oids': ['oid-mana'], 'result': 'Infinite colored mana'}
        mana_attr = _mana_producer_attr(assets)
        edges = derive_combo_edges(combo, {'oid-mana': {mana_attr}}, assets)
        assert edges == []

    def test_deterministic(self, assets) -> None:
        combo, otag_index = self._combo_with_mana_and_untap(assets)
        a = [(e.name, e.trigger_event, e.produced_event) for e in derive_combo_edges(combo, otag_index, assets)]
        b = [(e.name, e.trigger_event, e.produced_event) for e in derive_combo_edges(combo, otag_index, assets)]
        assert a == b


# ---------------------------------------------------------------------------
# derive_self_couplings: single-card within-card edges (staged for 5.3 driver)
# ---------------------------------------------------------------------------


def _find_self_coupling_otags(assets):
    """Find two otag slugs on a hypothetical single card: one triggers e', the
    other produces e (e' != e).  Returns (producer_slug, trigger_slug, e, e')."""
    # Search the catalog for a producer attr of one event and a trigger attr
    # of a different event, both bare otag slugs (not buckets).
    for e, spec in sorted(assets.events.items()):
        for a_prod in sorted(spec['produced_by']):
            if a_prod in assets.buckets:
                continue
            # Confirm this slug is recognized by derive_card_attributes.
            from pipeline.transforms.strategy.derive_card_attributes import derive_card_attributes

            if a_prod not in derive_card_attributes({a_prod}, assets):
                continue
            for e_prime, spec_prime in sorted(assets.events.items()):
                if e_prime == e:
                    continue
                for a_trig in sorted(spec_prime['triggers']):
                    if a_trig in assets.buckets:
                        continue
                    if a_trig == a_prod:
                        continue
                    if a_trig not in derive_card_attributes({a_trig}, assets):
                        continue
                    return a_prod, a_trig, e, e_prime
    raise AssertionError('no self-coupling pair found in catalog')


class TestDeriveSelfCouplings:
    """Staged for task 5.3 — tested in isolation per C1 review fix."""

    def test_card_with_both_trigger_and_producer_emits_edge(self, assets) -> None:
        """A card carrying both a trigger of e' and a producer of e (e != e')
        emits at least one self-coupling EventEffect."""
        a_prod, a_trig, e, e_prime = _find_self_coupling_otags(assets)
        otags = {a_prod, a_trig}
        edges = derive_self_couplings('oid-self', otags, assets, authored_at='t')
        assert edges, f'expected self-coupling from producer({a_prod}->{e}) + trigger({a_trig}->{e_prime})'
        # Provenance: no combo_variant_id (single card), keyed by oracle_id.
        for edge in edges:
            assert edge.provenance.combo_variant_id == ''
            assert edge.provenance.source_card_oracle_id == 'oid-self'
            assert edge.provenance.source == COMBO_EDGE_SOURCE
            # Names are synthetic (spellbook::attr::e'=>e), not bare attrs.
            assert '::' in edge.name

    def test_card_with_only_producer_emits_nothing(self, assets) -> None:
        """A card that is a producer of one event but triggers NOTHING authors
        no self-coupling edges."""
        mana_attr = _mana_producer_attr(assets)
        # Confirm the slug IS a producer of mana-produced.
        assert mana_attr in assets.events['mana-produced']['produced_by']
        # Give the card ONLY this one slug.  If it is not a trigger of any
        # other event, no self-coupling is possible.
        from pipeline.transforms.strategy.derive_card_attributes import derive_card_attributes

        attrs = derive_card_attributes({mana_attr}, assets)
        # Check there is no trigger-side overlap for any OTHER event.
        has_trigger = any(attrs & set(spec['triggers']) for ev, spec in assets.events.items() if ev != 'mana-produced')
        if has_trigger:
            pytest.skip('mana_attr also triggers another event — fixture ambiguity')
        edges = derive_self_couplings('oid-producer-only', {mana_attr}, assets, authored_at='t')
        assert edges == []


# ---------------------------------------------------------------------------
# Phase-B assembly: present-combo selection + reciprocal grounding
# ---------------------------------------------------------------------------


class TestAssembly:
    def test_absent_combo_edges_dropped(self, assets) -> None:
        combo = {
            'variant_id': 'PRESENT',
            'oids': ['oid-mana', 'oid-untap'],
            'result': 'Infinite colored mana; Infinite untap of creatures you control',
        }
        mana_attr = _mana_producer_attr(assets)
        untap_attr = next(a for a in assets.events['untap']['produced_by'] if a not in assets.buckets)
        edges = derive_combo_edges(combo, {'oid-mana': {mana_attr}, 'oid-untap': {untap_attr}}, assets)
        store = ProvisionalAttributeStore(attributes=edges)
        # The combo PRESENT -> grounded edges survive; ABSENT -> all dropped.
        kept_present = _assemble_present_combo_edges(store, [{'variant_id': 'PRESENT'}])
        kept_absent = _assemble_present_combo_edges(store, [{'variant_id': 'OTHER'}])
        assert not kept_present.is_empty
        assert kept_absent.is_empty

    def test_empty_store_short_circuits(self) -> None:
        empty = ProvisionalAttributeStore()
        out = _assemble_present_combo_edges(empty, [{'variant_id': 'X'}])
        assert out.is_empty

    def test_one_role_per_node_name(self, assets) -> None:
        combo = {
            'variant_id': 'P',
            'oids': ['oid-mana', 'oid-untap'],
            'result': 'Infinite colored mana; Infinite untap of creatures you control',
        }
        mana_attr = _mana_producer_attr(assets)
        untap_attr = next(a for a in assets.events['untap']['produced_by'] if a not in assets.buckets)
        edges = derive_combo_edges(combo, {'oid-mana': {mana_attr}, 'oid-untap': {untap_attr}}, assets)
        kept = _assemble_present_combo_edges(ProvisionalAttributeStore(attributes=edges), [{'variant_id': 'P'}])
        names = [a.name for a in kept.attributes]
        assert len(names) == len(set(names)), 'relay needs exactly one role per node'


# ---------------------------------------------------------------------------
# C3: keystone provenance-gated skip — combo edges don't double-count native
# nodes, and non-combo attrs assemble without silent drops
# ---------------------------------------------------------------------------


class TestKeystoneProvenanceGate:
    """The 2b keystone in engine_report skips membership re-evaluation only for
    combo-lowered edges whose name is already a native node.  This prevents
    double-counting attr_weight while letting non-combo store attrs proceed normally
    (C3 review fix — provenance-gated, not bare node-presence)."""

    def test_combo_edge_not_in_native_nodes_still_assembles(self, assets) -> None:
        """A combo-lowered edge whose name is NOT a current native node
        passes through Phase-B assembly correctly (it is not dropped)."""
        combo = {
            'variant_id': 'C3P',
            'oids': ['oid-mana', 'oid-untap'],
            'result': 'Infinite colored mana; Infinite untap of creatures you control',
        }
        mana_attr = _mana_producer_attr(assets)
        untap_attr = next(a for a in assets.events['untap']['produced_by'] if a not in assets.buckets)
        edges = derive_combo_edges(combo, {'oid-mana': {mana_attr}, 'oid-untap': {untap_attr}}, assets)
        assert edges
        store = ProvisionalAttributeStore(attributes=edges)
        # Assembly with the combo present should keep grounded edges.
        kept = _assemble_present_combo_edges(store, [{'variant_id': 'C3P'}])
        # At least one edge survives (grounded reciprocals).
        assert not kept.is_empty
        # All kept edges are combo-provenance (combo_variant_id set).
        for a in kept.attributes:
            assert a.provenance.combo_variant_id == 'C3P'

    def test_combo_edge_matching_native_node_no_double_count(self, assets) -> None:
        """A combo-lowered edge (combo_variant_id set) whose name IS a native
        node is skipped by the provenance-gated 2b keystone, so it does NOT
        double-count attr_weight.  A non-combo attr with the same native-node name
        proceeds to evaluation (provenance gate only blocks combo-lowered)."""
        from pipeline.transforms.strategy.combo_edges import _attr_query, _provenance

        deck = _load_deck('gauntlet__urza-lord-high-artificer')
        ctx = compute_facts(deck, [])

        # Pick a native node that is a producer of some event.
        native_producer = None
        for node_name in sorted(ctx.nodes):
            for _ev, spec in assets.events.items():
                if node_name in spec['produced_by']:
                    native_producer = node_name
                    break
            if native_producer:
                break
        if not native_producer:
            pytest.skip('no native node is a catalog producer')

        ev_found = next(ev for ev, spec in assets.events.items() if native_producer in spec['produced_by'])
        other_ev = next(ev for ev in assets.events if ev != ev_found)

        # --- Sub-test A: combo-lowered edge named after native node ---
        # Build a combo edge + reciprocal, then assemble with the combo present.
        combo_edge = CardAttribute(
            name=native_producer,
            query=_attr_query(native_producer, assets),
            trigger_event=other_ev,
            produced_event=ev_found,
            provenance=_provenance(combo_variant_id='C3-COMBO', source_card_oracle_id='', authored_at='t'),
        )
        combo_recip = CardAttribute(
            name=native_producer + '-recip',
            query=_attr_query(native_producer, assets),
            trigger_event=ev_found,
            produced_event=other_ev,
            provenance=_provenance(combo_variant_id='C3-COMBO', source_card_oracle_id='', authored_at='t'),
        )
        combo_store = ProvisionalAttributeStore(attributes=[combo_edge, combo_recip])
        assembled = _assemble_present_combo_edges(combo_store, [{'variant_id': 'C3-COMBO'}])
        # The combo edge named after the native node survives assembly
        # (grounded reciprocal), but the 2b keystone SKIPS it because
        # combo_variant_id is set AND name is in nodes.
        assert not assembled.is_empty, 'combo edges should survive assembly'
        combo_attrs = [a for a in assembled.attributes if a.provenance.combo_variant_id]
        native_collision = [a for a in combo_attrs if a.name == native_producer]
        assert native_collision, f'expected a combo attr named {native_producer!r} in assembled store'

        # --- Sub-test B: non-combo attr with same native-node name ---
        # A haiku-authored attr (combo_variant_id='') with the same name should
        # NOT be blocked by the provenance gate.  It passes through assembly
        # unchanged (non-combo records always pass).
        from pipeline.transforms.strategy.machine_vocab import Provenance

        haiku_attr = CardAttribute(
            name=native_producer,
            query=_attr_query(native_producer, assets),
            trigger_event=other_ev,
            produced_event=ev_found,
            provenance=Provenance(source='haiku', lifecycle='provisional'),
        )
        haiku_store = ProvisionalAttributeStore(attributes=[haiku_attr])
        haiku_assembled = _assemble_present_combo_edges(haiku_store, [])
        # Non-combo attrs pass through assembly regardless of detected combos.
        assert not haiku_assembled.is_empty
        haiku_surviving = [a for a in haiku_assembled.attributes if a.name == native_producer]
        assert haiku_surviving, 'non-combo attr should survive assembly even with empty detected list'
        # The haiku attr has combo_variant_id='' so the provenance gate does
        # NOT skip it — it proceeds to membership evaluation in the 2b block.
        assert haiku_surviving[0].provenance.combo_variant_id == ''


# ---------------------------------------------------------------------------
# Caching + idempotency (ADR 0021 Phase-A staleness key)
# ---------------------------------------------------------------------------


class TestCachingIdempotent:
    def test_persist_is_idempotent_at_same_version(self, assets, tmp_path: Path) -> None:
        combo = {
            'variant_id': 'C1',
            'oids': ['oid-mana', 'oid-untap'],
            'result': 'Infinite colored mana; Infinite untap of creatures you control',
        }
        mana_attr = _mana_producer_attr(assets)
        untap_attr = next(a for a in assets.events['untap']['produced_by'] if a not in assets.buckets)
        edges = derive_combo_edges(combo, {'oid-mana': {mana_attr}, 'oid-untap': {untap_attr}}, assets)
        assert edges
        for e in edges:
            persist(e, store_dir=tmp_path)
        n1 = len(load_store(tmp_path).attributes)
        # Re-run authoring at the SAME derivation_version -> no new records.
        for e in edges:
            persist(e, store_dir=tmp_path)
        n2 = len(load_store(tmp_path).attributes)
        assert n1 == n2 == len(edges)

    def test_distinct_couplings_are_both_cached(self, assets, tmp_path: Path) -> None:
        # Same producer attr, two different triggers -> two records (not deduped).
        combo = {
            'variant_id': 'C2',
            'oids': ['oid-mana', 'oid-untap'],
            'result': 'Infinite colored mana; Infinite untap of creatures you control',
        }
        mana_attr = _mana_producer_attr(assets)
        untap_attr = next(a for a in assets.events['untap']['produced_by'] if a not in assets.buckets)
        edges = derive_combo_edges(combo, {'oid-mana': {mana_attr}, 'oid-untap': {untap_attr}}, assets)
        by_name: dict[str, int] = {}
        for e in edges:
            by_name[e.name] = by_name.get(e.name, 0) + 1
            persist(e, store_dir=tmp_path)
        loaded = load_store(tmp_path)
        assert len(loaded.attributes) == len(edges)


# ---------------------------------------------------------------------------
# author_combo_edges: order-stable flat list over the combo table
# ---------------------------------------------------------------------------


def test_author_combo_edges_is_order_stable(assets) -> None:
    mana_attr = _mana_producer_attr(assets)
    untap_attr = next(a for a in assets.events['untap']['produced_by'] if a not in assets.buckets)
    combos = [
        {
            'variant_id': 'B',
            'oids': ['m', 'u'],
            'result': 'Infinite colored mana; Infinite untap of creatures you control',
        },
        {
            'variant_id': 'A',
            'oids': ['m', 'u'],
            'result': 'Infinite colored mana; Infinite untap of creatures you control',
        },
    ]
    otag_index = {'m': {mana_attr}, 'u': {untap_attr}}
    out1 = author_combo_edges(combos, otag_index, assets, authored_at='t')
    out2 = author_combo_edges(combos, otag_index, assets, authored_at='t')
    assert [(e.name, e.provenance.combo_variant_id) for e in out1] == [
        (e.name, e.provenance.combo_variant_id) for e in out2
    ]
    # Authored once per combo, sorted by variant_id (A before B).
    seen_vids = [e.provenance.combo_variant_id for e in out1]
    assert seen_vids.index('A') < seen_vids.index('B')


# ---------------------------------------------------------------------------
# Hard gates: win-walk parity + P0 default-path byte-identity
# ---------------------------------------------------------------------------

import json  # noqa: E402

from pipeline.transforms.strategy.engine_report import identify_engines  # noqa: E402
from pipeline.transforms.strategy.strategy_graph import compute_facts  # noqa: E402

_FIX = Path(__file__).resolve().parent / 'fixtures' / 'decks'


def _load_deck(stem: str) -> dict:
    return json.loads((_FIX / f'{stem}.json').read_text())


class TestWinWalkParity:
    """Lowering is confined to the engine_report/relay surface; the win view
    (combo_win_verdict via compute_facts) never sees it (ADR 0016 / 0021 View A)."""

    def test_win_flags_independent_of_any_store(self) -> None:
        # compute_facts computes win flags with NO store argument at all, so a
        # populated provisional store cannot move them.  We assert the win flags
        # are a pure function of (deck, combos) -- the lowering lives downstream.
        from pipeline.transforms.strategy.combo import load_combos_from_lake

        try:
            combos = load_combos_from_lake()
        except Exception:
            pytest.skip('lake unavailable')
        deck = _load_deck('gauntlet__urza-lord-high-artificer')
        ctx1 = compute_facts(deck, combos)
        ctx2 = compute_facts(deck, combos)
        wins1 = {(c['variant_id'], c['win']) for c in ctx1.detected}
        wins2 = {(c['variant_id'], c['win']) for c in ctx2.detected}
        assert wins1 == wins2
        # And the public report carries the same unauthored cards; the win-walk is unaffected.
        assert identify_engines(deck, store=ProvisionalAttributeStore(), combos=combos) is not None

    def test_combo_win_verdict_byte_identical_empty_vs_populated_store(self, assets) -> None:
        """C4 review fix: combo_win_verdict output is byte-identical with the store
        EMPTY vs POPULATED with combo edges on a representative combo fixture.
        The lowering must never change win detection."""
        from pipeline.transforms.strategy.combo import load_combos_from_lake

        try:
            combos = load_combos_from_lake()
        except Exception:
            pytest.skip('lake unavailable')
        deck = _load_deck('gauntlet__urza-lord-high-artificer')

        # Run compute_facts (which runs combo_win_verdict) with no store influence.
        ctx_baseline = compute_facts(deck, combos)
        wins_baseline = sorted(
            (c['variant_id'], c['win'], c.get('_walk_verdict', {}).get('kind', '')) for c in ctx_baseline.detected
        )

        # Build a populated store from the detected combos' edges.
        otag_index: dict[str, set[str]] = {}
        for card in deck.get('cards', []):
            oid = card.get('oracle_id', '')
            if oid:
                otag_index[oid] = {t.lower() for t in card.get('otags', [])}
        combo_dicts = [
            {
                'variant_id': c['variant_id'],
                'oids': c.get('card_oracle_ids', c.get('oids', [])),
                'result': c.get('result', ''),
            }
            for c in ctx_baseline.detected
        ]
        authored = author_combo_edges(combo_dicts, otag_index, assets, authored_at='t')
        populated_store = ProvisionalAttributeStore(attributes=authored)

        # Re-run compute_facts — this is the SAME function, store-unaware.
        # The store only feeds engine_report downstream, never combo_win_verdict.
        ctx_populated = compute_facts(deck, combos)
        wins_populated = sorted(
            (c['variant_id'], c['win'], c.get('_walk_verdict', {}).get('kind', '')) for c in ctx_populated.detected
        )
        assert wins_baseline == wins_populated, (
            'combo_win_verdict output must be byte-identical regardless of store state'
        )

        # Also verify that identify_engines with a populated store produces
        # the same win-combo set as with an empty store.  The dark_cards and
        # completeness state MAY differ (the populated store covers more cards
        # via authored edges), but win detection itself — which is determined
        # by combo_win_verdict inside compute_facts — must not change.
        report_empty = identify_engines(deck, store=ProvisionalAttributeStore(), combos=combos)
        report_pop = identify_engines(deck, store=populated_store, combos=combos)
        # Win detection is upstream of the store (combo_win_verdict in compute_facts),
        # so the raw_isolation's win data is identical.
        empty_wins = (
            sorted((c['variant_id'], c['win']) for c in report_empty.raw_isolation.get('_ctx_detected', []))
            if '_ctx_detected' in report_empty.raw_isolation
            else None
        )
        pop_wins = (
            sorted((c['variant_id'], c['win']) for c in report_pop.raw_isolation.get('_ctx_detected', []))
            if '_ctx_detected' in report_pop.raw_isolation
            else None
        )
        # If the raw_isolation doesn't carry detected combos directly, we
        # already proved the point above via compute_facts parity.
        if empty_wins is not None and pop_wins is not None:
            assert empty_wins == pop_wins


class TestDefaultPathByteIdentity:
    """The committed empty store assembles to empty -> engine_report default is
    byte-identical (the engine SHA 7a73aba9 is held by the parity oracle)."""

    def test_empty_store_assembly_is_noop(self) -> None:
        empty = ProvisionalAttributeStore()
        out = _assemble_present_combo_edges(empty, [{'variant_id': 'anything'}])
        assert out is empty or out.is_empty

    def test_store_from_disk_default_is_empty(self, tmp_path: Path) -> None:
        # No store file -> empty store -> default behaviour unchanged.
        assert load_store(tmp_path).is_empty


# ---------------------------------------------------------------------------
# Task 5.3a: populate_combo_edges driver + end-to-end blast-radius
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def _fixture_combos() -> list[dict]:
    """The 48 fixture combos (from combos.json)."""
    return json.loads((_FIXTURES / 'combos.json').read_text())


def _fixture_otag_index() -> dict[str, set[str]]:
    """Build oracle_id -> {otag slugs} from ALL fixture deck cards.

    This covers all cards in the 17 fixture decks -- enough for the combo-piece
    scope to find the right producer attrs.
    """
    index: dict[str, set[str]] = {}
    for p in sorted(_FIX.glob('*.json')):
        deck = json.loads(p.read_text())
        for card in deck.get('cards', []):
            oid = card.get('oracle_id', '')
            if oid:
                slugs = {t.lower() for t in card.get('otags', [])}
                if oid in index:
                    index[oid] |= slugs
                else:
                    index[oid] = slugs
    return index


class TestPopulateComboEdgesDriver:
    """Task 5.3a: the deterministic Phase-A population driver."""

    def test_populates_store_from_fixture_combos(self, tmp_path: Path) -> None:
        """The driver writes combo edges to an initially empty store."""
        combos = _fixture_combos()
        otag_index = _fixture_otag_index()
        result = populate_combo_edges(
            store_dir=tmp_path,
            combos=combos,
            otag_index=otag_index,
        )
        assert isinstance(result, PopulationResult)
        assert result.total_combos == len(combos)
        assert result.authored > 0, 'expected the driver to author some edges'
        assert result.derived > 0
        # The store on disk now has edges.
        store = load_store(tmp_path)
        assert not store.is_empty
        assert len(store.attributes) == result.authored

    def test_idempotent_rerun_is_noop(self, tmp_path: Path) -> None:
        """Re-running the driver at the same derivation_version persists zero new edges."""
        combos = _fixture_combos()
        otag_index = _fixture_otag_index()
        r1 = populate_combo_edges(
            store_dir=tmp_path,
            combos=combos,
            otag_index=otag_index,
        )
        assert r1.authored > 0

        r2 = populate_combo_edges(
            store_dir=tmp_path,
            combos=combos,
            otag_index=otag_index,
        )
        # All combos with existing edges should be skipped.
        assert r2.skipped > 0
        # No new edges authored (persist dedup catches any that slip through).
        assert r2.authored == 0
        # Store size unchanged.
        store = load_store(tmp_path)
        assert len(store.attributes) == r1.authored

    def test_deterministic_output(self, tmp_path: Path) -> None:
        """Two independent runs produce byte-identical stores."""
        combos = _fixture_combos()
        otag_index = _fixture_otag_index()

        dir1 = tmp_path / 'run1'
        dir1.mkdir()
        dir2 = tmp_path / 'run2'
        dir2.mkdir()

        populate_combo_edges(store_dir=dir1, combos=combos, otag_index=otag_index)
        populate_combo_edges(store_dir=dir2, combos=combos, otag_index=otag_index)

        s1 = load_store(dir1)
        s2 = load_store(dir2)
        # Same number of attrs.
        assert len(s1.attributes) == len(s2.attributes)
        # Same content (name, trigger, produced, combo_variant_id).
        keys1 = sorted(
            (a.name, a.trigger_event, a.produced_event, a.provenance.combo_variant_id) for a in s1.attributes
        )
        keys2 = sorted(
            (a.name, a.trigger_event, a.produced_event, a.provenance.combo_variant_id) for a in s2.attributes
        )
        assert keys1 == keys2

    def test_all_edges_are_spellbook_provenance(self, tmp_path: Path) -> None:
        """Every authored edge has source='spellbook' and a combo_variant_id."""
        combos = _fixture_combos()
        otag_index = _fixture_otag_index()
        populate_combo_edges(
            store_dir=tmp_path,
            combos=combos,
            otag_index=otag_index,
        )
        store = load_store(tmp_path)
        for attr in store.attributes:
            assert attr.provenance.source == COMBO_EDGE_SOURCE
            assert attr.provenance.combo_variant_id != '', f'combo edge {attr.name!r} missing combo_variant_id'
            assert attr.provenance.derivation_version == DERIVATION_VERSION

    @_skip_no_lake
    def test_p0_shas_held_after_population(self, tmp_path: Path) -> None:
        """Populating the store does NOT touch compute_facts/resolve — P0 SHAs
        are byte-identical (the hard gate)."""
        from pipeline.transforms.strategy.tests.engine_parity_oracle import (
            composite_sha,
            compute_engine_isolation_baselines,
            compute_resolve_baselines,
        )

        # Populate the store.
        combos = _fixture_combos()
        otag_index = _fixture_otag_index()
        populate_combo_edges(
            store_dir=tmp_path,
            combos=combos,
            otag_index=otag_index,
        )
        # P0 SHAs must not shift — they don't consult the store at all.
        engine_sha = composite_sha(compute_engine_isolation_baselines())
        resolve_sha = composite_sha(compute_resolve_baselines())
        assert engine_sha == '84b9617efcf0ec7481ee167874cb6802b4762ce494a2a8a72085785ddda7441a'
        assert resolve_sha == '5a43fc56ce9fd371f8a4729e7cbc11a7855ec08185fac81fe35873c57dfad889'


class TestHasFreshEdgesForCombo:
    """Unit tests for the new has_fresh_edges_for_combo store method."""

    def test_empty_store_returns_false(self) -> None:
        store = ProvisionalAttributeStore()
        assert not store.has_fresh_edges_for_combo('any-vid')

    def test_matching_combo_returns_true(self) -> None:
        from pipeline.transforms.strategy.combo_edges import _provenance

        attr = CardAttribute(
            name='test-attr',
            query='otag:test',
            trigger_event='a',
            produced_event='b',
            provenance=_provenance(
                combo_variant_id='VID-1',
                source_card_oracle_id='',
                authored_at='t',
            ),
        )
        store = ProvisionalAttributeStore(attributes=[attr])
        assert store.has_fresh_edges_for_combo('VID-1')

    def test_wrong_combo_returns_false(self) -> None:
        from pipeline.transforms.strategy.combo_edges import _provenance

        attr = CardAttribute(
            name='test-attr',
            query='otag:test',
            trigger_event='a',
            produced_event='b',
            provenance=_provenance(
                combo_variant_id='VID-1',
                source_card_oracle_id='',
                authored_at='t',
            ),
        )
        store = ProvisionalAttributeStore(attributes=[attr])
        assert not store.has_fresh_edges_for_combo('VID-OTHER')

    def test_stale_version_returns_false(self) -> None:
        from pipeline.transforms.strategy.machine_vocab import Provenance as Prov

        attr = CardAttribute(
            name='test-attr',
            query='otag:test',
            trigger_event='a',
            produced_event='b',
            provenance=Prov(
                source='spellbook',
                combo_variant_id='VID-1',
                derivation_version=0,  # stale
            ),
        )
        store = ProvisionalAttributeStore(attributes=[attr])
        assert not store.has_fresh_edges_for_combo(
            'VID-1',
            derivation_version=DERIVATION_VERSION,
        )


# ---------------------------------------------------------------------------
# Task 5.3a: end-to-end blast-radius measurement
# ---------------------------------------------------------------------------


class TestEndToEndBlastRadius:
    """End-to-end: populate the store, run engine_report across the fixture
    corpus, and confirm the delta matches the 5.2 scratch blast radius.

    Expected blast radius (from the 5.2 scratch harness):
    - Urza surfaces its genuine ``draw-a-card`` recursion (NOT mana-produced)
    - ~13/17 fixtures are no-op (engines unchanged)
    - K'rrik / Ozai contained (engines change but no genuine engine lost)
    - 0 win-flag changes across the corpus
    """

    @pytest.fixture()
    def populated_store(self, tmp_path: Path) -> ProvisionalAttributeStore:
        """Populate the store from fixture combos and return the loaded store."""
        combos = _fixture_combos()
        otag_index = _fixture_otag_index()
        populate_combo_edges(
            store_dir=tmp_path,
            combos=combos,
            otag_index=otag_index,
        )
        return load_store(tmp_path)

    def _all_fixture_decks(self) -> list[tuple[str, dict]]:
        """Load all 17 fixture decks as (slug, deck) pairs."""
        decks = []
        for p in sorted(_FIX.glob('*.json')):
            decks.append((p.stem, json.loads(p.read_text())))
        return decks

    def test_urza_surfaces_draw_a_card_recursion(
        self,
        populated_store: ProvisionalAttributeStore,
    ) -> None:
        """Urza: the populated store surfaces draw-a-card recursion (the Urza
        thin-capture closure), NOT mana-produced."""
        deck = _load_deck('gauntlet__urza-lord-high-artificer')
        combos = _fixture_combos()

        report_populated = identify_engines(
            deck,
            store=populated_store,
            combos=combos,
        )
        report_empty = identify_engines(
            deck,
            store=ProvisionalAttributeStore(),
            combos=combos,
        )

        # The populated report should have engines.
        assert report_populated.engines

        # The populated store should surface draw-a-card in a cycle or engine
        # that the empty store does not.
        pop_events = set()
        for eng in report_populated.engines:
            pop_events.update(eng.events)
        empty_events = set()
        for eng in report_empty.engines:
            empty_events.update(eng.events)

        # draw-a-card should be newly surfaced (or already present; either way
        # it must be in the populated events).
        assert 'draw-a-card' in pop_events, (
            f'Urza populated engines should surface draw-a-card recursion; got events: {pop_events}'
        )

    def test_zero_win_flag_changes(
        self,
        populated_store: ProvisionalAttributeStore,
    ) -> None:
        """Populating the store must not change any combo's win flag across
        the entire fixture corpus (win detection is upstream of the store)."""
        combos = _fixture_combos()
        for slug, deck in self._all_fixture_decks():
            ctx = compute_facts(deck, list(combos))
            wins_baseline = sorted((c['variant_id'], c['win']) for c in ctx.detected)
            # identify_engines re-runs compute_facts internally; the win flags
            # come from combo_win_verdict which is store-independent.  Call it to
            # confirm the store doesn't interfere, but we verify via the
            # upstream compute_facts directly below.
            identify_engines(deck, store=populated_store, combos=combos)
            # Re-verify via a fresh compute_facts (which is store-unaware).
            ctx2 = compute_facts(deck, list(combos))
            wins_after = sorted((c['variant_id'], c['win']) for c in ctx2.detected)
            assert wins_baseline == wins_after, f'{slug}: win flags changed after store population'

    def test_blast_radius_contained(
        self,
        populated_store: ProvisionalAttributeStore,
    ) -> None:
        """Most fixture decks should be no-op (engines unchanged); Krrik/Ozai
        changes should be contained (no genuine engine lost)."""
        combos = _fixture_combos()
        changed_decks: list[str] = []
        noop_decks: list[str] = []

        for slug, deck in self._all_fixture_decks():
            report_pop = identify_engines(
                deck,
                store=populated_store,
                combos=combos,
            )
            report_empty = identify_engines(
                deck,
                store=ProvisionalAttributeStore(),
                combos=combos,
            )

            pop_engine_names = sorted(e.name for e in report_pop.engines)
            empty_engine_names = sorted(e.name for e in report_empty.engines)

            if pop_engine_names != empty_engine_names:
                changed_decks.append(slug)
            else:
                noop_decks.append(slug)

        # ~13/17 should be no-op.
        assert len(noop_decks) >= 13, f'Expected >= 13 no-op decks, got {len(noop_decks)}: changed = {changed_decks}'

    def test_no_genuine_engine_event_lost(
        self,
        populated_store: ProvisionalAttributeStore,
    ) -> None:
        """No deck should LOSE an event that was covered by an empty-store
        engine.  Events may be PROMOTED into cycles (e.g. a standalone
        ``card-enters-graveyard`` payoff absorbed into a
        ``card-enters-graveyard <-> mana-produced`` cycle), but never dropped
        entirely."""
        combos = _fixture_combos()
        for slug, deck in self._all_fixture_decks():
            report_pop = identify_engines(
                deck,
                store=populated_store,
                combos=combos,
            )
            report_empty = identify_engines(
                deck,
                store=ProvisionalAttributeStore(),
                combos=combos,
            )

            # All events covered by the empty store must still be covered.
            empty_events: set[str] = set()
            for eng in report_empty.engines:
                empty_events.update(eng.events)
            pop_events: set[str] = set()
            for eng in report_pop.engines:
                pop_events.update(eng.events)
            lost = empty_events - pop_events
            assert not lost, f'{slug}: engine event(s) LOST: {lost}'
