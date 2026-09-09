"""Phase 0 combo-projection tests — the raw-variant widening (steps + prereqs + typed produces).

Pure projection logic, no JVM and no network. Covers
:func:`~pipeline.transforms.combo_detect._combo_from_variant` (the ``_project`` step named in the
plan) now that it carries the ORDERED ``steps``, the non-empty ``prerequisites``, and the typed
``produces`` (per-feature ``win`` computed with the SAME predicate as :func:`is_game_win_result`) —
without disturbing the pre-existing ``variant_id`` / ``card_names`` / ``card_oracle_ids`` / ``result``
fields or the detection / litmus behavior.
"""

from __future__ import annotations

from pipeline.transforms.combo_detect import _combo_from_variant, is_game_win_result


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
