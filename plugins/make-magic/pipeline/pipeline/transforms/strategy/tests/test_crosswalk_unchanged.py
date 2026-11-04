"""The committed crosswalk is consumed import-only and is UNCHANGED.

The strategy engine is additive: it must not mutate (nor require editing) the
committed ``crosswalk.buckets_for`` / ``BUCKET_ROOTS``. These assertions pin the
crosswalk's public surface so an accidental edit to the excluded consumer would
fail here.
"""

from __future__ import annotations

from pipeline.transforms import crosswalk
from pipeline.transforms.crosswalk import BUCKETS, buckets_for


def test_buckets_for_is_pure_multilabel():
    # Cultivate-like card: ramp AND tutor (multi-label is a feature)
    assert buckets_for({'ramp', 'tutor'}) == {'ramp', 'tutor'}
    # a drain spell lands in burn via life-loss, no damage needed
    assert 'burn' in buckets_for({'opponent-loses-life'})
    # no match -> honest empty set, not a guess
    assert buckets_for({'totally-unknown-slug'}) == set()


def test_crosswalk_bucket_vocabulary_frozen():
    assert BUCKETS == (
        'removal', 'ramp', 'draw', 'tokens', 'counters', 'burn', 'tutor', 'sac',
        'counterspells', 'flicker', 'typal', 'anthem', 'combat', 'protection',
        'stax', 'extra_combat', 'wincon',
    )
    assert frozenset({'stax', 'extra_combat', 'wincon'}) == crosswalk.GAP_BUCKETS


def test_strategy_does_not_reimport_buckets_for():
    # the engine does NOT route via crosswalk.buckets_for: archetype_of_attr + the
    # v4 vocab are a parallel reimplementation (acknowledged duplication the
    # Phase-4 migration will reconcile). Pin that boundary so nobody "fixes" the
    # docstring by silently wiring buckets_for into the engine namespace.
    from pipeline.transforms import strategy
    from pipeline.transforms.strategy import assets, facts, naming, resolve

    for mod in (strategy, assets, facts, naming, resolve):
        assert not hasattr(mod, 'buckets_for'), f'{mod.__name__} unexpectedly imports buckets_for'


def test_degree_protection_axis_reuses_crosswalk_root_readonly():
    # the ONLY crosswalk reuse is a read-only import of BUCKET_ROOTS['protection']
    # in degree.py — so the protection degree-axis can never drift from the
    # canonical root (finding #4). Assert identity of the slug set, not a copy.
    from pipeline.transforms.strategy.degree import DEGREE_AXES

    assert DEGREE_AXES['protection'] == set(crosswalk.BUCKET_ROOTS['protection'])
