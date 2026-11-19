"""Hermetic unit tests for the reproducibility instrument (Components 2+3).

No lake, no DuckDB, no network: every test drives the pure record builder and
the differ over synthetic dump fixtures. The lake-backed CLI paths are exercised
by the smoke steps in the subagent's verification, not here.
"""

from __future__ import annotations

import copy

from pipeline.transforms.strategy.tests.repro import semantic_diff as sd
from pipeline.transforms.strategy.tests.repro import semantic_dump as dump

# --------------------------------------------------------------------------- #
# Synthetic resolve() record + deck, mirroring the real field layout.
# --------------------------------------------------------------------------- #


def _synthetic_rec() -> dict:
    return {
        'name': 'Test Deck',
        'bundle': 'casual',
        'source': 'gauntlet',
        'bracket': 3,
        'archetype_identity': [
            {'strategy': 'aggro', 'weighted_quantity': 10.123456789, 'share': 0.5000004},
        ],
        'ranked_identity': [['aggro', 10.1, True], ['control', 5.0, False]],
        'combo_identity': None,
        'pre_combo_name': 'aggro',
        'confidence': 'HIGH',
        'n_win_combos': 0,
        'n_detected_combos': 1,
        'combo_override_applied': False,
        'facts': {
            'attributes_present': {'tokens': 12.0, 'aggro': 9.0},
            'engines': {
                'engine_units': {
                    'engines': [
                        {
                            'event': 'combat-damage-dealt',
                            'producers': ['b', 'a'],
                            'consumers': ['c'],
                            'payoff_archetypes': ['aggro'],
                            'mass': 20.0,
                            'kind': 'recursive-engine',
                        },
                        {
                            'event': 'token-created',
                            'producers': ['x'],
                            'consumers': ['y'],
                            'payoff_archetypes': [],
                            'mass': 5.0,
                            'kind': 'static-payoff',
                        },
                    ],
                    'ranked_units': [
                        {
                            'event': 'combat-damage-dealt',
                            'mass': 20.0,
                            'recursive': True,
                            'payoff_archetypes': ['aggro'],
                            'share': 0.8,
                        },
                    ],
                    'forward_chains': [['token-created', 'combat-damage-dealt']],
                    'commander_loop': None,
                    'determinism': 'mass-elected',
                },
            },
        },
    }


def _synthetic_deck() -> dict:
    return {'cards': [{'name': 'B', 'oracle_id': '2'}, {'name': 'A', 'oracle_id': '1'}]}


# --------------------------------------------------------------------------- #
# Component 2 — build_record / dump
# --------------------------------------------------------------------------- #


def test_build_record_layers_and_normalization() -> None:
    rec = _synthetic_rec()
    record = dump.build_record(_synthetic_deck(), rec)

    assert set(record) == set(dump.LAYERS)

    # attributes: sorted names, deduped
    assert record['attributes'] == ['aggro', 'tokens']

    # events: every per-event producer/consumer membership, sorted alpha
    assert record['events'] == {
        'combat-damage-dealt': {'producers': ['a', 'b'], 'consumers': ['c']},
        'token-created': {'producers': ['x'], 'consumers': ['y']},
    }

    # eventeffects: event->event edges
    assert record['eventeffects'] == [['token-created', 'combat-damage-dealt']]

    # engines: the elected ranked_units, enriched with producers/consumers/kind
    assert record['engines'] == {
        'combat-damage-dealt': {
            'producers': ['a', 'b'],
            'consumers': ['c'],
            'payoff_archetypes': ['aggro'],
            'mass': 20.0,
            'kind': 'recursive-engine',
        },
    }

    # archetype_identity: the nine named fields; floats rounded to 6dp
    strat = record['archetype_identity']
    assert strat['archetype_identity'] == [{'strategy': 'aggro', 'weighted_quantity': 10.123457, 'share': 0.5}]
    assert strat['ranked_identity'] == [['aggro', 10.1, True], ['control', 5.0, False]]
    assert strat['combo_identity'] is None
    assert strat['headline'] == 'aggro'
    assert strat['pre_combo_name'] == 'aggro'
    assert strat['confidence'] == 'HIGH'
    assert strat['bracket'] == 3
    assert strat['n_win_combos'] == 0
    assert strat['n_detected_combos'] == 1

    # pool: sorted [name, oid] list
    assert record['pool'] == [['A', '1'], ['B', '2']]


def test_headline_from_combo_override() -> None:
    rec = _synthetic_rec()
    rec['combo_override_applied'] = True
    rec['combo_identity'] = 'infinite combo [A+B]'
    record = dump.build_record(_synthetic_deck(), rec)
    assert record['archetype_identity']['headline'] == 'alt-wincon/combo'
    assert record['archetype_identity']['combo_identity'] == 'infinite combo [A+B]'


def test_build_record_is_pure() -> None:
    rec = _synthetic_rec()
    before = copy.deepcopy(rec)
    dump.build_record(_synthetic_deck(), rec)
    assert rec == before  # no mutation of the source record


def test_deck_key_uniqueness_across_bundles() -> None:
    a = dict(_synthetic_rec(), bundle='casual', name='Reaper')
    b = dict(_synthetic_rec(), bundle='mid', name='Reaper')
    assert dump.deck_key(a) != dump.deck_key(b)


# --------------------------------------------------------------------------- #
# Component 3 — differ / classifier
# --------------------------------------------------------------------------- #


def _dump_of(record: dict, key: str = 'casual::Test Deck') -> dict:
    return {'meta': {'created_at': 'x', 'env': {}}, 'decks': {key: record}}


def _record() -> dict:
    return dump.build_record(_synthetic_deck(), _synthetic_rec())


def test_identical_dumps_exit_zero() -> None:
    a = _dump_of(_record())
    b = _dump_of(copy.deepcopy(_record()))
    diff = sd.diff_dumps(a, b)
    assert sd.exit_code(diff) == 0
    assert diff['decks_moved'] == 0


def test_meta_excluded_from_comparison() -> None:
    a = _dump_of(_record())
    b = _dump_of(_record())
    b['meta'] = {'created_at': 'DIFFERENT', 'env': {'PYTHONHASHSEED': '4242'}, 'lake_file_shas': {'x': 'y'}}
    diff = sd.diff_dumps(a, b)
    assert sd.exit_code(diff) == 0


def test_benign_ordering_is_normalization_gap() -> None:
    a = _dump_of(_record())
    b_rec = _record()
    b_rec['attributes'] = ['tokens', 'aggro']  # same set, wrong order
    b = _dump_of(b_rec)
    diff = sd.diff_dumps(a, b)
    assert sd.exit_code(diff) == 1
    assert diff['counts']['attributes']['benign'] == 1
    assert len(diff['benign_list']) == 1


def test_substantive_attribute_disappeared() -> None:
    a = _dump_of(_record())
    b_rec = _record()
    b_rec['attributes'] = ['aggro']  # lost 'tokens'
    b = _dump_of(b_rec)
    diff = sd.diff_dumps(a, b)
    assert sd.exit_code(diff) == 2
    assert diff['counts']['attributes']['substantive'] == 1


def test_substantive_engine_lost() -> None:
    a = _dump_of(_record())
    b_rec = _record()
    b_rec['engines'] = {}  # engine gone
    b = _dump_of(b_rec)
    diff = sd.diff_dumps(a, b)
    assert sd.exit_code(diff) == 2
    assert diff['counts']['engines']['substantive'] == 1
    row = next(r for r in diff['substantive_list'] if r['layer'] == 'engines')
    assert row['deck'] == 'casual::Test Deck'


def test_substantive_engine_mass_changed() -> None:
    a = _dump_of(_record())
    b_rec = _record()
    b_rec['engines']['combat-damage-dealt']['mass'] = 31.0
    b = _dump_of(b_rec)
    diff = sd.diff_dumps(a, b)
    assert sd.exit_code(diff) == 2


def test_substantive_strategy_field_flip() -> None:
    a = _dump_of(_record())
    b_rec = _record()
    b_rec['archetype_identity']['confidence'] = 'MED'
    b = _dump_of(b_rec)
    diff = sd.diff_dumps(a, b)
    assert sd.exit_code(diff) == 2
    assert diff['counts']['archetype_identity']['substantive'] == 1


def test_substantive_eventeffect_edge_changed() -> None:
    a = _dump_of(_record())
    b_rec = _record()
    b_rec['eventeffects'] = [['combat-damage-dealt', 'token-created']]  # edge flipped
    b = _dump_of(b_rec)
    diff = sd.diff_dumps(a, b)
    assert sd.exit_code(diff) == 2
    assert diff['counts']['eventeffects']['substantive'] == 1


def test_card_universe_pool_differs() -> None:
    a = _dump_of(_record())
    b_rec = _record()
    b_rec['pool'] = [['A', '1'], ['C', '3']]  # B->C swap
    b = _dump_of(b_rec)
    diff = sd.diff_dumps(a, b)
    assert sd.exit_code(diff) == 3
    assert diff['counts']['pool']['card-universe-change'] == 1
    assert len(diff['card_universe_list']) == 1


def test_card_universe_deck_appears() -> None:
    a = _dump_of(_record())
    b = _dump_of(_record())
    b['decks']['mid::Extra'] = _record()
    diff = sd.diff_dumps(a, b)
    assert sd.exit_code(diff) == 3
    assert 'mid::Extra' in diff['decks_only_in_b']


def test_precedence_card_universe_over_substantive() -> None:
    a = _dump_of(_record())
    b_rec = _record()
    b_rec['archetype_identity']['confidence'] = 'LOW'  # substantive
    b_rec['pool'] = [['Z', '9']]  # card-universe
    b = _dump_of(b_rec)
    diff = sd.diff_dumps(a, b)
    assert sd.exit_code(diff) == 3  # card-universe dominates


def test_report_renders_markdown() -> None:
    a = _dump_of(_record())
    b_rec = _record()
    b_rec['archetype_identity']['confidence'] = 'MED'
    b = _dump_of(b_rec)
    diff = sd.diff_dumps(a, b)
    report = sd.render_report(diff)
    assert '# ' in report or '## ' in report
    assert 'casual::Test Deck' in report
    assert 'substantive' in report.lower()


def test_report_flags_card_universe_as_isolation_breach() -> None:
    a = _dump_of(_record())
    b_rec = _record()
    b_rec['pool'] = [['A', '1']]
    b = _dump_of(b_rec)
    diff = sd.diff_dumps(a, b)
    report = sd.render_report(diff)
    assert 'isolation' in report.lower()
