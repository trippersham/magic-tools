"""The canonical deck ``version`` primitive.

``version(deck)`` is a stable sha256 over ONLY a deck's persisted, non-derived
facts (cards as sorted ``(name, quantity, role)`` tuples, strategy, assessment,
sorted focus_otags, format). It is the ONE primitive reused for sync-drift and
freshness, so its exact properties are load-bearing:

- order-independence: cards / focus_otags in a different order → SAME hash;
- a real edit (qty / role / strategy / assessment / focus_otags / format change)
  MOVES the hash;
- hydrated Scryfall enrichment is EXCLUDED → an enrichment-only difference does
  NOT move the hash (that would make it flap for non-edits).
"""

from __future__ import annotations

from pipeline.contracts import Deck, DeckCard
from pipeline.decks import version


def _deck(**overrides: object) -> Deck:
    """A small commander-shaped deck; overrides patch the persisted facts."""
    base: dict[str, object] = {
        'name': 'Test Deck',
        'strategy': 'go wide tokens',
        'assessment': 'needs more removal',
        'focus_otags': ['tokens', 'go-wide'],
        'format': 'commander',
        'cards': [
            DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander'),
            DeckCard(name='Mountain', quantity=9),
            DeckCard(name='Sol Ring', quantity=1),
        ],
    }
    base.update(overrides)
    return Deck.model_validate(base)


def test_version_is_a_sha256_hex_string() -> None:
    digest = version(_deck())
    assert isinstance(digest, str)
    assert len(digest) == 64
    assert all(c in '0123456789abcdef' for c in digest)


def test_card_order_does_not_change_version() -> None:
    """Cards in a different ORDER → equal version (canonical sort)."""
    a = _deck(
        cards=[
            DeckCard(name='Sol Ring', quantity=1),
            DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander'),
            DeckCard(name='Mountain', quantity=9),
        ]
    )
    b = _deck(
        cards=[
            DeckCard(name='Mountain', quantity=9),
            DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander'),
            DeckCard(name='Sol Ring', quantity=1),
        ]
    )
    assert version(a) == version(b)


def test_focus_otags_order_does_not_change_version() -> None:
    a = _deck(focus_otags=['tokens', 'go-wide'])
    b = _deck(focus_otags=['go-wide', 'tokens'])
    assert version(a) == version(b)


def test_quantity_change_moves_version() -> None:
    base = _deck()
    changed = _deck(
        cards=[
            DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander'),
            DeckCard(name='Mountain', quantity=8),  # 9 -> 8
            DeckCard(name='Sol Ring', quantity=1),
        ]
    )
    assert version(base) != version(changed)


def test_role_change_moves_version() -> None:
    base = _deck()
    changed = _deck(
        cards=[
            DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander'),
            DeckCard(name='Mountain', quantity=9),
            DeckCard(name='Sol Ring', quantity=1, role='sideboard'),  # None -> sideboard
        ]
    )
    assert version(base) != version(changed)


def test_strategy_change_moves_version() -> None:
    assert version(_deck()) != version(_deck(strategy='burn to the face'))


def test_assessment_change_moves_version() -> None:
    assert version(_deck()) != version(_deck(assessment='actually fine as-is'))


def test_focus_otags_change_moves_version() -> None:
    assert version(_deck()) != version(_deck(focus_otags=['tokens', 'go-wide', 'sacrifice']))


def test_format_change_moves_version() -> None:
    assert version(_deck()) != version(_deck(format='modern'))


def test_enrichment_only_difference_does_not_move_version() -> None:
    """Two decks with identical PERSISTED facts but different hydrated Scryfall
    enrichment (mana_value, oracle_text, colors, oracle_id, image urls) hash
    identically — enrichment is volatile/derived and EXCLUDED from the hash."""
    bare = _deck(
        cards=[
            DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander'),
            DeckCard(name='Sol Ring', quantity=1),
        ]
    )
    hydrated = _deck(
        cards=[
            DeckCard(
                name='Krenko, Mob Boss',
                quantity=1,
                role='commander',
                oracle_id='abc-123',
                mana_value=4.0,
                mana_cost='{2}{R}{R}',
                type_line='Legendary Creature — Goblin Warrior',
                colors=['R'],
                oracle_text='Tap: Create X 1/1 Goblins.',
            ),
            DeckCard(
                name='Sol Ring',
                quantity=1,
                oracle_id='def-456',
                mana_value=1.0,
                mana_cost='{1}',
                type_line='Artifact',
                oracle_text='Tap: Add {C}{C}.',
            ),
        ]
    )
    assert version(bare) == version(hydrated)
