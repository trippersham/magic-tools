"""Phase 0 combo-projection tests — the raw-variant widening (steps + prereqs + typed produces).

Pure projection logic, no JVM and no network. Covers
:func:`~pipeline.transforms.combo_detect._combo_from_variant` (the ``_project`` step named in the
plan) now that it carries the ORDERED ``steps``, the non-empty ``prerequisites``, and the typed
``produces`` (per-feature ``win`` computed with the SAME predicate as :func:`is_game_win_result`) —
without disturbing the pre-existing ``variant_id`` / ``card_names`` / ``card_oracle_ids`` / ``result``
fields or the detection / litmus behavior.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.transforms.combo_detect import (
    Combo,
    _combo_from_variant,
    _materialize,
    is_game_win_result,
    load_combos,
)


def _fixture_variant() -> dict:
    """A raw Spellbook variant dict exercising every widened projection field.

    Multi-line ``description`` (ordered steps, with a blank line + surrounding whitespace to
    prove stripping/empty-dropping), an empty ``easyPrerequisites`` (dropped), a non-empty
    ``notablePrerequisites`` and ``manaNeeded`` (kept in that order), a ``produces`` list holding
    one WIN feature and one non-win resource loop, and two concrete ``uses`` cards.
    """
    return {
        'id': '42-abc',
        'description': (
            'Tap Card A for mana.\n  Cast Card B using that mana.  \n\nRepeat until you have infinite triggers.\n'
        ),
        'easyPrerequisites': '',
        'notablePrerequisites': 'Card A must be untapped.',
        'manaNeeded': '{2}{U}',
        'produces': [
            {'feature': {'name': 'Target opponent loses the game', 'status': 'R', 'uncountable': True}, 'quantity': 1},
            {'feature': {'name': 'Infinite card draw', 'status': 'S', 'uncountable': True}, 'quantity': 1},
        ],
        'uses': [
            {'card': {'name': 'Card A', 'oracleId': 'oid-a'}},
            {'card': {'name': 'Card B', 'oracleId': 'oid-b'}},
        ],
    }


def test_project_carries_ordered_steps() -> None:
    combo = _combo_from_variant(_fixture_variant())
    assert combo is not None
    # newline-split, stripped, empties dropped, ORDER preserved.
    assert combo.steps == (
        'Tap Card A for mana.',
        'Cast Card B using that mana.',
        'Repeat until you have infinite triggers.',
    )


def test_project_carries_nonempty_prerequisites_in_order() -> None:
    combo = _combo_from_variant(_fixture_variant())
    assert combo is not None
    # easy (empty, dropped), notable, manaNeeded — preserve that order.
    assert combo.prerequisites == ('Card A must be untapped.', '{2}{U}')


def test_project_carries_typed_produces_with_per_feature_win() -> None:
    combo = _combo_from_variant(_fixture_variant())
    assert combo is not None
    assert combo.produces == (
        {'name': 'Target opponent loses the game', 'status': 'R', 'win': True},
        {'name': 'Infinite card draw', 'status': 'S', 'win': False},
    )
    # The per-feature `win` uses the SAME predicate as the litmus, applied per feature name.
    assert [p['win'] for p in combo.produces] == [
        is_game_win_result('Target opponent loses the game'),
        is_game_win_result('Infinite card draw'),
    ]


def test_project_leaves_existing_fields_unchanged() -> None:
    combo = _combo_from_variant(_fixture_variant())
    assert combo is not None
    assert combo.variant_id == '42-abc'
    assert combo.card_names == ('Card A', 'Card B')
    assert combo.card_oracle_ids == ('oid-a', 'oid-b')
    # result stays the '; '-joined feature names, order preserved.
    assert combo.result == 'Target opponent loses the game; Infinite card draw'


# --------------------------------------------------------------------------- #
# Lake round-trip: _materialize -> load_combos over the widened schema.
# The projection above is pure; these prove the new `steps VARCHAR[]`,
# `prerequisites VARCHAR[]`, and `produces STRUCT(name,status,win)[]` columns
# survive a real write-then-read through the normalized DuckDB lake (the one
# path the pure-projection tests can't reach). Uses the isolated `data_dir`
# store fixture from conftest.
# --------------------------------------------------------------------------- #


def test_materialize_load_roundtrip_preserves_widened_fields(data_dir: Path) -> None:
    projected = _combo_from_variant(_fixture_variant())
    assert projected is not None

    _materialize([projected])
    loaded = load_combos()

    assert len(loaded) == 1
    got = loaded[0]
    # Every field survives the parquet round-trip byte-for-byte, including the
    # STRUCT(name,status,win)[] produces with its per-feature `win` booleans.
    assert got.variant_id == projected.variant_id
    assert got.card_names == projected.card_names
    assert got.card_oracle_ids == projected.card_oracle_ids
    assert got.result == projected.result
    assert got.steps == projected.steps
    assert got.prerequisites == projected.prerequisites
    assert tuple(dict(p) for p in got.produces) == tuple(dict(p) for p in projected.produces)
    # `win` is a real bool after the DuckDB BOOLEAN round-trip, not truthy junk.
    assert [p['win'] for p in got.produces] == [True, False]
    assert all(isinstance(p['win'], bool) for p in got.produces)


def test_materialize_empty_yields_empty_schema_template(data_dir: Path) -> None:
    # The empty-input branch writes a typed 0-row template (the STRUCT column
    # is declared explicitly); loading it back must yield no combos, not error.
    _materialize([])
    assert load_combos() == []


def test_materialize_load_roundtrip_empty_widened_fields(data_dir: Path) -> None:
    # A legacy-shaped Combo (no steps/prereqs/produces) round-trips to the same
    # empty tuples — the defaults land as empty lists, not nulls.
    legacy = Combo(
        variant_id='legacy-1',
        card_names=('Card A',),
        card_oracle_ids=('oid-a',),
        result='',
    )
    _materialize([legacy])
    got = load_combos()[0]
    assert got.steps == ()
    assert got.prerequisites == ()
    assert got.produces == ()
