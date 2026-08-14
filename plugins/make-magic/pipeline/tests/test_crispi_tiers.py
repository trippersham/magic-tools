"""TDD tests for the CRISPI named-card tier data (``transforms/crispi_tiers``).

These lock the rubric's EXPLICITLY named cards to their tier label + point value
across the three tables (draw / tutor / interaction), plus the normalization
contract of the ``tier_for`` lookup helper (case- and punctuation-insensitive)
and its miss behaviour (unknown card -> ``None``).

No network. Static data only — this is the Phase 0 alignment anchor.
"""

from __future__ import annotations

import pytest

from pipeline.transforms.crispi_tiers import (
    DRAW_TIERS,
    EXTRA_TURNS,
    GAME_CHANGERS,
    INTERACTION_TIERS,
    MASS_LAND_DENIAL,
    TUTOR_TIERS,
    normalize_card_name,
    tier_for,
)

# --------------------------------------------------------------------------- #
# tier_for — the rubric's named tutors
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ('name', 'label', 'points'),
    [
        # 6pt premium true tutors (CMC <=2, unrestricted).
        ('Demonic Tutor', 'premium', 6),
        ('Vampiric Tutor', 'premium', 6),
        ('Imperial Seal', 'premium', 6),
        ('Enlightened Tutor', 'premium', 6),
        ('Mystical Tutor', 'premium', 6),
        ('Worldly Tutor', 'premium', 6),
        ('Gamble', 'premium', 6),
        # 6pt repeatable tutor engines.
        ('Survival of the Fittest', 'repeatable-engine', 6),
        ('Birthing Pod', 'repeatable-engine', 6),
        ('Fauna Shaman', 'repeatable-engine', 6),
        # 4pt combat-conditioned tutor engines (attack triggers).
        ('Zur the Enchanter', 'combat-tutor', 4),
        ('Armored Skyhunter', 'combat-tutor', 4),
        # 4pt standard true tutors (CMC 3-4 or restricted).
        ('Grim Tutor', 'standard', 4),
        ('Wishclaw Talisman', 'standard', 4),
        ('Fabricate', 'standard', 4),
        ('Eldritch Evolution', 'standard', 4),
        ('Finale of Devastation', 'standard', 4),
        ("Green Sun's Zenith", 'standard', 4),
        # 4pt combo-enablers-that-tutor.
        ('Demonic Consultation', 'combo-enabler', 4),
        ('Tainted Pact', 'combo-enabler', 4),
        ('Doomsday', 'combo-enabler', 4),
        # Graveyard-destination tutors (gated on a recursion package).
        ('Entomb', 'graveyard-tutor', 4),
        ('Buried Alive', 'graveyard-tutor', 4),
    ],
)
def test_tier_for_tutors(name: str, label: str, points: int) -> None:
    assert tier_for(name, TUTOR_TIERS) == (label, points)


# --------------------------------------------------------------------------- #
# tier_for — the rubric's named draw engines
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ('name', 'label', 'points'),
    [
        # 6pt burst draw.
        ('Ad Nauseam', 'burst', 6),
        ('Wheel of Fortune', 'burst', 6),
        ('Necropotence', 'burst', 6),
        # 5pt premium asymmetric engines.
        ('Rhystic Study', 'premium-asymmetric', 5),
        ('Mystic Remora', 'premium-asymmetric', 5),
        ('Esper Sentinel', 'premium-asymmetric', 5),
        ('Sylvan Library', 'premium-asymmetric', 5),
        # 4pt standard repeatable engines (normal game actions).
        ('Tatyova, Benthic Druid', 'standard-repeatable', 4),
        ('Beast Whisperer', 'standard-repeatable', 4),
        ('Consecrated Sphinx', 'premium-asymmetric', 5),
        # 3pt card selection / filtering.
        ('Brainstorm', 'selection', 3),
        ('Ponder', 'selection', 3),
        ('Preordain', 'selection', 3),
        ("Sensei's Divining Top", 'selection', 3),
        # 3pt combat-conditioned draw.
        ('Cold-Eyed Selkie', 'combat-conditioned', 3),
        ('Toski, Bearer of Secrets', 'combat-conditioned', 3),
        # 2pt one-shot draw (ETB / sac-itself).
        ('Prime Speaker Zegana', 'one-shot', 2),
        ("Commander's Sphere", 'one-shot', 2),
        # 2pt one-shot draw (burst-ish, non-repeatable).
        ("Night's Whisper", 'one-shot', 2),
        ('Read the Bones', 'one-shot', 2),
        # 2pt symmetric engines.
        ('Howling Mine', 'symmetric', 2),
        ('Temple Bell', 'symmetric', 2),
        ('Dictate of Kruphix', 'symmetric', 2),
    ],
)
def test_tier_for_draw(name: str, label: str, points: int) -> None:
    assert tier_for(name, DRAW_TIERS) == (label, points)


def test_draw_punishers_are_not_draw_sources() -> None:
    """Opponent-draw punishers score as Interaction, never as draw sources."""
    for name in ('Sheoldred, the Apocalypse', 'Orcish Bowmasters', 'Underworld Dreams'):
        assert tier_for(name, DRAW_TIERS) is None


# --------------------------------------------------------------------------- #
# tier_for — the rubric's named interaction / stack-timing cards
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ('name', 'label', 'points'),
    [
        # 2pt effective counterspells.
        ('Veil of Summer', 'counterspell', 2),
        ("Autumn's Veil", 'counterspell', 2),
        ("Imp's Mischief", 'counterspell', 2),
        ('Bolt Bend', 'counterspell', 2),
        ('Pyroblast', 'counterspell', 2),
        # 2pt free interaction spells (0-mana / alternate-cost reactive).
        ('Force of Will', 'free', 2),
        ('Fierce Guardianship', 'free', 2),
        ('Deflecting Swat', 'free', 2),
        ('Flawless Maneuver', 'free', 2),
        ('Deadly Rollick', 'free', 2),
        ('Snuff Out', 'free', 2),
        # 2pt turn-protection effects.
        ('Silence', 'turn-protection', 2),
        ('Grand Abolisher', 'turn-protection', 2),
        # +1 premium instant-speed removal.
        ('Swords to Plowshares', 'premium-instant', 1),
        ('Path to Exile', 'premium-instant', 1),
        ("Assassin's Trophy", 'premium-instant', 1),
        # +1 premium HARD-scope board wipe.
        ('Toxic Deluge', 'hard-scope-wipe', 1),
        ('Culling Ritual', 'hard-scope-wipe', 1),
    ],
)
def test_tier_for_interaction(name: str, label: str, points: int) -> None:
    assert tier_for(name, INTERACTION_TIERS) == (label, points)


# --------------------------------------------------------------------------- #
# Normalization contract
# --------------------------------------------------------------------------- #


def test_tier_for_normalizes_case() -> None:
    assert tier_for('demonic tutor', TUTOR_TIERS) == ('premium', 6)
    assert tier_for('DEMONIC TUTOR', TUTOR_TIERS) == ('premium', 6)
    assert tier_for('DeMoNiC tUtOr', TUTOR_TIERS) == ('premium', 6)


def test_tier_for_normalizes_punctuation() -> None:
    """Apostrophes / hyphens / commas do not change the lookup result."""
    assert tier_for('green suns zenith', TUTOR_TIERS) == ('standard', 4)
    assert tier_for("Green Sun's Zenith", TUTOR_TIERS) == ('standard', 4)
    assert tier_for('Cold Eyed Selkie', DRAW_TIERS) == ('combat-conditioned', 3)
    assert tier_for('Toski Bearer of Secrets', DRAW_TIERS) == ('combat-conditioned', 3)


def test_tier_for_normalizes_whitespace() -> None:
    assert tier_for('  Demonic   Tutor  ', TUTOR_TIERS) == ('premium', 6)


def test_tier_for_unknown_returns_none() -> None:
    assert tier_for('Not A Real Card', TUTOR_TIERS) is None
    assert tier_for('Not A Real Card', DRAW_TIERS) is None
    assert tier_for('Not A Real Card', INTERACTION_TIERS) is None


def test_normalize_card_name_idempotent() -> None:
    once = normalize_card_name("Green Sun's Zenith")
    assert normalize_card_name(once) == once
    assert normalize_card_name("Green Sun's Zenith") == normalize_card_name('green suns zenith')


# --------------------------------------------------------------------------- #
# Tables are normalized at rest (keys already normalized so lookups are O(1))
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize('table', [DRAW_TIERS, TUTOR_TIERS, INTERACTION_TIERS])
def test_table_keys_are_normalized(table: dict[str, tuple[str, int]]) -> None:
    for key in table:
        assert key == normalize_card_name(key), f'unnormalized key: {key!r}'


# --------------------------------------------------------------------------- #
# Bracket name-lists — scaffold frozensets (Phase 6 re-fetches the live lists)
# --------------------------------------------------------------------------- #


def test_bracket_lists_are_frozensets() -> None:
    assert isinstance(GAME_CHANGERS, frozenset)
    assert isinstance(MASS_LAND_DENIAL, frozenset)
    assert isinstance(EXTRA_TURNS, frozenset)


def test_bracket_lists_have_starter_content() -> None:
    """Scaffold lists carry a non-empty starter set (finalized in Phase 6)."""
    assert GAME_CHANGERS
    assert MASS_LAND_DENIAL
    assert EXTRA_TURNS


def test_bracket_lists_are_normalized() -> None:
    """Name-list members are normalized so membership tests match ``tier_for`` keys."""
    for members in (GAME_CHANGERS, MASS_LAND_DENIAL, EXTRA_TURNS):
        for name in members:
            assert name == normalize_card_name(name), f'unnormalized member: {name!r}'


def test_bracket_lists_membership_via_normalization() -> None:
    """A raw card name normalizes into a scaffold list."""
    assert normalize_card_name('Rhystic Study') in GAME_CHANGERS
    assert normalize_card_name('Armageddon') in MASS_LAND_DENIAL
    assert normalize_card_name('Time Warp') in EXTRA_TURNS
