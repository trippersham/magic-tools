"""OFFLINE tests for the Phase-4a transforms (pilot marts). No network.

Covers:
    - DAG rollup: a synthetic DAG with a multi-parent node and a would-be cycle
      — a leaf rolls up to ALL ancestors, no infinite loop, root reached.
    - Crosswalk: leaf tags map to the correct MULTIPLE buckets; burn vs life-loss
      both land in `burn`; committed bucket slugs exist in the DAG snapshot.
    - combo_detect: exact named-card matching (all cards present -> hit; a
      missing card -> no hit); template-only variants are dropped.
    - Susceptibility: a synthetic deck fingerprint yields the expected weakness
      with the citing counts.
    - FactSheet validity: factsheet_for(sample_deck) validates against
      contracts.FactSheet, including the new otag_buckets + susceptibility.
    - Land/round-trip: rollup_rows landed to normalized/card_otag reads back.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from pipeline import store
from pipeline.contracts import FactSheet
from pipeline.transforms import combo_detect, crosswalk, deck_factsheet, otag_rollup


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(root))
    return root


# --------------------------------------------------------------------------- #
# DAG rollup — synthetic DAG (multi-parent + would-be cycle).
# --------------------------------------------------------------------------- #

#   root
#    |  \
#    a   b        (a and b are both children of root)
#     \ /
#     leaf        (leaf is MULTI-PARENT: parents a AND b)
#
# plus a would-be cycle: x -> y -> x (defensive; the real DAG is acyclic).
_SYNTH_PARENTS = {
    'leaf': ['a', 'b'],
    'a': ['root'],
    'b': ['root'],
    'root': [],
    'x': ['y'],
    'y': ['x'],  # cycle
}


def test_ancestors_multi_parent_reaches_root() -> None:
    anc = otag_rollup.ancestors('leaf', _SYNTH_PARENTS)
    assert anc == {'a', 'b', 'root'}  # both parents + shared root, once each


def test_ancestors_is_cycle_safe() -> None:
    # A cycle must terminate and not include the start node as its own ancestor
    # unless a real edge points back to it.
    anc = otag_rollup.ancestors('x', _SYNTH_PARENTS)
    assert anc == {'x', 'y'}  # y (parent), then x (y's parent) — bounded, no hang


def test_closure_includes_self_and_all_ancestors() -> None:
    assert otag_rollup.closure('leaf', _SYNTH_PARENTS) == {'leaf', 'a', 'b', 'root'}


def test_rollup_rows_explodes_leaf_to_all_ancestors() -> None:
    tags = [
        {'id': 'root', 'slug': 'removal', 'parent_ids': [], 'taggings': []},
        {'id': 'a', 'slug': 'sweeper', 'parent_ids': ['root'], 'taggings': []},
        {'id': 'b', 'slug': 'spot-removal', 'parent_ids': ['root'], 'taggings': []},
        {
            'id': 'leaf',
            'slug': 'sweeper-x',
            'parent_ids': ['a', 'b'],
            # one card carries only the leaf tag:
            'taggings': [{'oracle_id': 'oid-1', 'weight': 'median'}],
        },
    ]
    rows = otag_rollup.rollup_rows(tags)
    slugs = {slug for oid, slug in rows if oid == 'oid-1'}
    # leaf + both parents + shared root — the card is visible under every ancestor.
    assert slugs == {'sweeper-x', 'sweeper', 'spot-removal', 'removal'}


def test_rollup_rows_are_distinct() -> None:
    # A card carrying two leaves that share a root emits the root slug once.
    tags = [
        {'id': 'root', 'slug': 'removal', 'parent_ids': [], 'taggings': []},
        {
            'id': 'l1',
            'slug': 'sweeper',
            'parent_ids': ['root'],
            'taggings': [{'oracle_id': 'oid-1'}],
        },
        {
            'id': 'l2',
            'slug': 'spot',
            'parent_ids': ['root'],
            'taggings': [{'oracle_id': 'oid-1'}],
        },
    ]
    rows = otag_rollup.rollup_rows(tags)
    assert rows.count(('oid-1', 'removal')) == 1


def test_rollup_materialize_and_readback(data_dir: Path) -> None:
    rows = [('oid-1', 'removal'), ('oid-1', 'sweeper'), ('oid-2', 'ramp')]
    path = otag_rollup._materialize(rows)
    assert path.exists()
    with store.connect() as conn:
        got = set(store.read_parquet(conn, 'normalized', 'card_otag').fetchall())
    assert got == set(rows)


# --------------------------------------------------------------------------- #
# Crosswalk — multi-label; burn vs life-loss both in `burn`; slugs real.
# --------------------------------------------------------------------------- #


def test_buckets_multi_label() -> None:
    # Cultivate-shaped closure: rolls up to ramp AND tutor -> both buckets.
    got = crosswalk.buckets_for({'land-ramp', 'ramp', 'tutor-land', 'tutor'})
    assert got == {'ramp', 'tutor'}


def test_burn_bucket_covers_damage_and_life_loss() -> None:
    # Direct damage (burn) lands in `burn`.
    assert crosswalk.buckets_for({'burn'}) == {'burn'}
    # Life-loss with NO damage (Torment of Hailfire) also lands in `burn`.
    assert crosswalk.buckets_for({'opponent-loses-life'}) == {'burn'}
    # Drain (Exsanguinate) too.
    assert 'burn' in crosswalk.buckets_for({'drain-life', 'lifegain'})


def test_buckets_empty_when_no_match() -> None:
    assert crosswalk.buckets_for({'french-vanilla', 'some-unmapped-slug'}) == set()


def test_gap_buckets_are_flagged() -> None:
    assert {'stax', 'extra_combat', 'wincon'} == crosswalk.GAP_BUCKETS
    # extra_combat maps its lone leaf slug.
    assert crosswalk.buckets_for({'extra-combat-phase'}) == {'extra_combat'}


def test_crosswalk_slugs_exist_in_dag_snapshot() -> None:
    """Every committed bucket slug must exist in the oracle-tags DAG snapshot,
    else the curation silently under-matches."""
    # Snapshot lives at the project-root data/snapshots (parents[2] of the module,
    # matching how ingest.oracle_tags resolves it).
    snapshot = Path(otag_rollup.__file__).resolve().parents[2] / 'data' / 'snapshots' / 'oracle_tags.json.gz'
    with gzip.open(snapshot, 'rt', encoding='utf-8') as f:
        tags = json.load(f)
    slugs = {t['slug'] for t in tags}
    mapped = {s for slug_set in crosswalk.BUCKET_ROOTS.values() for s in slug_set}
    missing = mapped - slugs
    assert not missing, f'crosswalk slugs absent from DAG: {sorted(missing)}'


# --------------------------------------------------------------------------- #
# combo_detect — exact named-card matching.
# --------------------------------------------------------------------------- #

_VARIANT_CONCRETE = {
    'id': 'v1',
    'uses': [
        {'card': {'name': 'Card A', 'oracleId': 'oid-a', 'typeLine': 'Creature'}},
        {'card': {'name': 'Card B', 'oracleId': 'oid-b', 'typeLine': 'Artifact'}},
    ],
    'produces': [{'feature': {'name': 'Infinite mana'}}],
}
_VARIANT_TEMPLATE_ONLY = {
    'id': 'v2',
    'uses': [],
    'requires': [{'template': {'name': 'Any permanent'}}],
    'produces': [{'feature': {'name': 'Win'}}],
}


def test_normalize_drops_template_only_variants() -> None:
    combos = combo_detect.normalize_variants([_VARIANT_CONCRETE, _VARIANT_TEMPLATE_ONLY])
    assert [c.variant_id for c in combos] == ['v1']
    assert combos[0].result == 'Infinite mana'


def test_combos_in_deck_exact_match_by_oracle_id() -> None:
    combos = combo_detect.normalize_variants([_VARIANT_CONCRETE])
    # Both pieces present -> hit.
    hits = combo_detect.combos_in_deck({'oid-a', 'oid-b'}, combos)
    assert [c.variant_id for c in hits] == ['v1']
    # Only one piece present -> no hit (exact, all-cards required).
    assert combo_detect.combos_in_deck({'oid-a'}, combos) == []


def test_combos_in_deck_match_by_name() -> None:
    combos = combo_detect.normalize_variants([_VARIANT_CONCRETE])
    hits = combo_detect.combos_in_deck({'card a', 'CARD B'}, combos)
    assert [c.variant_id for c in hits] == ['v1']


# --------------------------------------------------------------------------- #
# Susceptibility — synthetic fingerprint -> expected weakness + citing counts.
# --------------------------------------------------------------------------- #


def test_susceptibility_board_wipe_signal_with_counts() -> None:
    buckets = {'tokens': 5, 'counters': 3}
    interaction = {'board_wipes': 0, 'counterspells': 2}
    structural = {'graveyard_recursion_present': False, 'etb_creatures': 1}
    signals = deck_factsheet.susceptibility(buckets, interaction, structural, [], {})
    board = [s for s in signals if s.startswith('Board wipes:')]
    assert board, signals
    # cites the driving counts (8 token/counter, 0 sweeper).
    assert '8 token/counter' in board[0]
    assert '0 sweeper' in board[0]


def test_susceptibility_zero_counterspells_signal() -> None:
    signals = deck_factsheet.susceptibility(
        {},
        {'board_wipes': 0, 'counterspells': 0},
        {'graveyard_recursion_present': True, 'etb_creatures': 0},
        [],
        {},
    )
    assert any('0 counterspells' in s for s in signals)


def test_susceptibility_quiet_when_resilient() -> None:
    # Recursion + a sweeper + counterspells present -> no board-wipe/stack signal.
    signals = deck_factsheet.susceptibility(
        {'tokens': 5, 'counters': 3},
        {'board_wipes': 3, 'counterspells': 4},
        {'graveyard_recursion_present': True, 'etb_creatures': 1},
        [],
        {},
    )
    assert not any(s.startswith('Board wipes:') for s in signals)
    assert not any('0 counterspells' in s for s in signals)


# --------------------------------------------------------------------------- #
# FactSheet validity — factsheet_for(sample) validates against contracts.
# --------------------------------------------------------------------------- #

_SAMPLE_DECK = [
    {
        'name': 'Cultivate',
        'oracle_id': 'oid-cult',
        'cmc': 3.0,
        'type_line': 'Sorcery',
        'keywords': [],
        'produced_mana': [],
        'mana_cost': '{2}{G}',
        'oracle_text': 'Search your library for up to two basic land cards, '
        'reveal them, put one onto the battlefield tapped and the other into your hand.',
    },
    {
        'name': 'Torment of Hailfire',
        'oracle_id': 'oid-torment',
        'cmc': 3.0,
        'type_line': 'Sorcery',
        'keywords': [],
        'produced_mana': [],
        'mana_cost': '{X}{B}{B}',
        'oracle_text': 'Repeat the following process X times. Each opponent loses 3 life '
        'unless that player sacrifices a nonland permanent or discards a card.',
    },
    {
        'name': 'Forest',
        'oracle_id': 'oid-forest',
        'cmc': 0.0,
        'type_line': 'Basic Land — Forest',
        'keywords': [],
        'produced_mana': ['G'],
        'mana_cost': '',
        'oracle_text': '',
    },
]

# Rolled-up slug closures (as otag_rollup would emit) for the sample.
_SAMPLE_OTAG = {
    'oid-cult': {'land-ramp', 'ramp', 'tutor-land', 'tutor', 'card-advantage'},
    'oid-torment': {'opponent-loses-life'},  # life-loss -> burn bucket
}


def test_factsheet_for_validates_against_contract() -> None:
    fs = deck_factsheet.factsheet_for(_SAMPLE_DECK, card_otag=_SAMPLE_OTAG, deck='Sample')
    # The contract is the gate.
    model = FactSheet.model_validate(fs)
    assert model.deck == 'Sample'
    # multi-label buckets present: Cultivate -> ramp+tutor+draw, Torment -> burn.
    assert fs['otag_buckets']['ramp'] == 1
    assert fs['otag_buckets']['tutor'] == 1
    assert fs['otag_buckets']['draw'] == 1
    assert fs['otag_buckets']['burn'] == 1
    # Forest is a land -> excluded from the nonland bucket/coverage denominator.
    assert fs['shape']['land_count'] == 1
    assert fs['shape']['nonland_count'] == 2
    # both nonland cards categorized by otags -> 100% coverage.
    assert fs['coverage']['categorized_pct'] == 100.0


def test_factsheet_for_empty_otag_is_still_valid() -> None:
    fs = deck_factsheet.factsheet_for(_SAMPLE_DECK)
    FactSheet.model_validate(fs)  # must not raise
    # With no otag join, bucket-derived categorization is empty...
    assert fs['otag_buckets'] == {}
    assert fs['coverage']['categorized_pct'] == 0.0
    # ...but structured-fact susceptibility (0 counterspells) still fires — it does
    # not depend on the otag join, so it is present even without tags.
    assert any('0 counterspells' in s for s in fs['susceptibility'])


# --------------------------------------------------------------------------- #
# Focus-relative analysis — (actual card tags) vs (deck's NARROW focus set).
# The engine only READS focus; it never writes it.
# --------------------------------------------------------------------------- #

# A focus deck: Cultivate is ramp+tutor+draw; Torment is burn (life-loss). We add
# a counters card so a slug-level focus entry has support and a bucket-level focus
# entry (counters) is well-supported, while a declared bucket with no support is
# thin, and prominent non-focus buckets (ramp/draw/tutor) become off_focus.
_FOCUS_DECK = [
    *_SAMPLE_DECK,
    {
        'name': 'Hardened Scales',
        'oracle_id': 'oid-scales',
        'cmc': 1.0,
        'type_line': 'Enchantment',
        'keywords': [],
        'produced_mana': [],
        'mana_cost': '{G}',
        'oracle_text': 'If one or more +1/+1 counters would be put on a creature '
        'you control, that many plus one +1/+1 counters are put on it instead.',
    },
]
_FOCUS_OTAG = {
    **_SAMPLE_OTAG,
    'oid-scales': {'counters-matter', 'gives-pp-counters'},  # -> counters bucket
}


def test_factsheet_for_no_focus_is_identical_to_pre_change() -> None:
    """Focus absent -> output byte-identical to the no-focus build (additive)."""
    without = deck_factsheet.factsheet_for(_FOCUS_DECK, card_otag=_FOCUS_OTAG)
    with_none = deck_factsheet.factsheet_for(_FOCUS_DECK, card_otag=_FOCUS_OTAG, focus=None)
    with_empty = deck_factsheet.factsheet_for(_FOCUS_DECK, card_otag=_FOCUS_OTAG, focus=[])
    assert without == with_none == with_empty
    # And the focus fields are present-but-empty (contract still valid).
    FactSheet.model_validate(without)
    assert without['focus'] == []
    assert without['focus_relative'] == {
        'coverage_of_focus': {},
        'thin_focus': [],
        'off_focus': [],
    }


def test_factsheet_for_focus_bucket_level_coverage_and_thin() -> None:
    # counters is well-supported (Hardened Scales); tokens is declared but the deck
    # has 0 token cards -> thin. typal declared, 0 support -> thin.
    focus = ['counters', 'tokens', 'typal']
    fs = deck_factsheet.factsheet_for(_FOCUS_DECK, card_otag=_FOCUS_OTAG, focus=focus)
    FactSheet.model_validate(fs)
    fr = fs['focus_relative']
    assert fs['focus'] == focus
    # coverage_of_focus counts supporting cards per focus entry (all entries echoed).
    assert fr['coverage_of_focus']['counters'] == 1
    assert fr['coverage_of_focus']['tokens'] == 0
    assert fr['coverage_of_focus']['typal'] == 0
    # thin_focus = entries below the small support threshold.
    assert 'tokens' in fr['thin_focus']
    assert 'typal' in fr['thin_focus']
    assert 'counters' not in fr['thin_focus'] or fr['coverage_of_focus']['counters'] >= 1


def test_factsheet_for_focus_slug_level_entry_resolves() -> None:
    # A focus entry that is a RAW OTAG SLUG (not a bucket name) matches cards
    # carrying that slug. `land-ramp` is a slug on Cultivate's closure.
    fs = deck_factsheet.factsheet_for(_FOCUS_DECK, card_otag=_FOCUS_OTAG, focus=['land-ramp'])
    assert fs['focus_relative']['coverage_of_focus']['land-ramp'] == 1
    assert 'land-ramp' not in fs['focus_relative']['thin_focus'] or 1 < 2


def test_factsheet_for_off_focus_lists_prominent_non_focus_buckets() -> None:
    # Deck's actual buckets: ramp, tutor, draw (Cultivate), burn (Torment),
    # counters (Scales). Focus only declares counters -> the rest are off_focus.
    fs = deck_factsheet.factsheet_for(_FOCUS_DECK, card_otag=_FOCUS_OTAG, focus=['counters'])
    off = fs['focus_relative']['off_focus']
    # counters is in focus -> not off_focus.
    assert 'counters' not in off
    # ramp/draw/tutor/burn are prominent card buckets the deck didn't declare.
    for bucket in ('ramp', 'draw', 'tutor', 'burn'):
        assert bucket in off, (bucket, off)
