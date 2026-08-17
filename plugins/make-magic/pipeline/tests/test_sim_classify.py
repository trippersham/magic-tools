"""Tests for :func:`pipeline.sim.classify.classify_deck` (deck -> interaction sets).

A MOCK resolver returns known ``otag_buckets`` / ``mana_value`` / ``type_line``
per card so the classification is asserted exactly, with NO lake dependency:

  * ``counterspells`` -> counters (NOT ``counters``, which is +1/+1 counters).
  * ``removal`` bucket -> removal. The ``burn`` bucket alone does NOT (it folds in
    life-loss / drain — Sign in Blood, Exsanguinate — that can't kill a creature);
    real burn removal carries ``removal`` too, so it still lands in the set (M2).
  * interaction = counters | removal; costs = int(mv) for interaction cards; lands
    from ``type_line`` containing 'Land' + the five basics.
  * an ALL-EMPTY-bucket resolver (the sparse lake) -> UNKNOWN / ``available=False``
    with a reason (never a fabricated 0/0).
"""

from __future__ import annotations

from pipeline.contracts import Card
from pipeline.sim.classify import classify_deck


class _MockResolver:
    """A ``CardResolver`` fed a name -> Card table; unknown names resolve to None."""

    def __init__(self, cards: dict[str, Card]) -> None:
        self._cards = cards

    def get_card(self, name: str) -> Card | None:
        return self._cards.get(name)


def _card(name: str, *, buckets: list[str], mv: float | None = None, type_line: str = 'Instant') -> Card:
    return Card(name=name, otag_buckets=buckets, mana_value=mv, type_line=type_line)


def _ur_resolver() -> _MockResolver:
    return _MockResolver(
        {
            # A true counterspell.
            'Reasonable Doubt': _card('Reasonable Doubt', buckets=['counterspells'], mv=2),
            # +1/+1-counters card: 'counters' bucket must NOT be read as countermagic.
            'Hardened Scales': _card('Hardened Scales', buckets=['counters'], mv=1, type_line='Enchantment'),
            # Damage-based burn removal carries BOTH burn + removal buckets.
            'Burst Lightning': _card('Burst Lightning', buckets=['burn', 'removal'], mv=1),
            'Bombard': _card('Bombard', buckets=['removal'], mv=3),
            'Lightning Bolt': _card('Lightning Bolt', buckets=['burn', 'removal'], mv=1),
            # A burn-BUCKET-only life-loss card (drain/draw, no removal bucket): it
            # can't kill a creature, so it must NOT be classed as removal (M2).
            'Sign in Blood': _card('Sign in Blood', buckets=['burn', 'draw'], mv=2, type_line='Sorcery'),
            # A land (by type_line).
            'Spectacle Summit': _card('Spectacle Summit', buckets=[], type_line='Land'),
            # A vanilla creature (resolves a bucket so the lake reads as populated).
            'Goblin Piker': _card('Goblin Piker', buckets=['creatures'], mv=2, type_line='Creature'),
        }
    )


def test_counters_are_counterspells_not_plus_one_counters() -> None:
    c = classify_deck(['Reasonable Doubt', 'Hardened Scales'], _ur_resolver())
    assert c.available is True
    assert c.counters == frozenset({'Reasonable Doubt'})
    # Hardened Scales is +1/+1 counters ('counters' bucket) — NOT countermagic.
    assert 'Hardened Scales' not in c.counters


def test_removal_is_removal_bucket_not_burn_only() -> None:
    # Damage-based burn removal (removal bucket present) IS removal; a burn-only
    # life-loss card (Sign in Blood — burn+draw, no removal bucket) is NOT (M2).
    c = classify_deck(['Burst Lightning', 'Bombard', 'Lightning Bolt', 'Sign in Blood'], _ur_resolver())
    assert c.removal == frozenset({'Burst Lightning', 'Bombard', 'Lightning Bolt'})
    assert 'Sign in Blood' not in c.removal


def test_interaction_is_counters_union_removal() -> None:
    names = ['Reasonable Doubt', 'Burst Lightning', 'Bombard']
    c = classify_deck(names, _ur_resolver())
    assert c.interaction == frozenset({'Reasonable Doubt', 'Burst Lightning', 'Bombard'})


def test_costs_are_int_mana_values_for_interaction_cards() -> None:
    c = classify_deck(['Reasonable Doubt', 'Burst Lightning', 'Bombard', 'Goblin Piker'], _ur_resolver())
    # Only interaction cards carry a cost entry (Goblin Piker is not interaction).
    assert c.costs == {'Reasonable Doubt': 2, 'Burst Lightning': 1, 'Bombard': 3}


def test_lands_from_type_line_and_basics() -> None:
    c = classify_deck(['Spectacle Summit', 'Island', 'Mountain', 'Reasonable Doubt'], _ur_resolver())
    assert c.lands == frozenset({'Spectacle Summit', 'Island', 'Mountain'})


def test_deck_with_no_interaction_is_available_with_real_zeros() -> None:
    # Some card resolved a bucket (lake populated) but none are counters/removal:
    # available=True, empty counter/removal sets are REAL, not a missing-data hole.
    c = classify_deck(['Goblin Piker', 'Spectacle Summit'], _ur_resolver())
    assert c.available is True
    assert c.counters == frozenset()
    assert c.removal == frozenset()


def test_empty_lake_is_unavailable_not_zero() -> None:
    # Every card resolves but with EMPTY otag_buckets (the "serving live only" env):
    # the classification is UNKNOWN, available=False with a reason — NOT a 0/0.
    empty = _MockResolver(
        {
            'Reasonable Doubt': _card('Reasonable Doubt', buckets=[], mv=2),
            'Burst Lightning': _card('Burst Lightning', buckets=[], mv=1),
        }
    )
    c = classify_deck(['Reasonable Doubt', 'Burst Lightning'], empty)
    assert c.available is False
    assert c.reason is not None
    assert 'otag lake' in c.reason
    assert c.counters == frozenset()
    assert c.removal == frozenset()


def test_unresolved_names_do_not_crash() -> None:
    # A name the resolver can't hydrate (None) is skipped; basics still count as
    # lands. With no bucket resolved at all -> unavailable.
    c = classify_deck(['Totally Fake Card', 'Island'], _MockResolver({}))
    assert c.available is False
