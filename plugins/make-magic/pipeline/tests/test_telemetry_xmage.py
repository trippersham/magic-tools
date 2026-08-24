"""XMage → the SAME telemetry as Forge, via the UNCHANGED Forge parser (task 2.2).

The XMage engine's harness (``pipeline/sim/java/xmage/``: ``XMageBatch`` + the
``mage.collectors.MakeMagicHooks`` shadow) translates a real XMage
ComputerPlayer7-vs-ComputerPlayer7 game into the SAME line contract the Forge
sim-AI harness emits — ``HANDLOG`` events (piloting) plus ``Turn:`` / ``Life:`` /
``Damage:`` / ``Land:`` / ``Game Result:`` (game features). So the whole
(M1/M2/M3-hardened) Forge telemetry parser consumes XMage logs verbatim — XMage is
a drop-in with identical :class:`PilotingProfile` + :class:`GameFeatures`, and
:mod:`pipeline.sim.telemetry` needs NO XMage-specific code.

The fixture ``tests/fixtures/xmage/cp7_uw_mirror.handlog`` is a REAL 4-game
UW-Control mirror captured from the standalone harness (``Ai(1)`` = candidate).
The exact counts below are locked against it. THE differentiator vs Forge: the
CP7 AI actually CASTS COUNTERS, so ``counter_fire`` is > 0 here — where Forge's
sim-AI is counter-blind (~0). Pure parse, no JVM.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.sim.telemetry import PilotingProfile, extract_match_features, extract_piloting

FIXTURE = Path(__file__).parent / 'fixtures' / 'xmage' / 'cp7_uw_mirror.handlog'

# UW Control classification (hardcoded so the test is deterministic + lake-free),
# matching classify_deck over the real deck. Deprive/Negate are the only counters;
# the removal suite is spot removal + wraths + planeswalkers + a manland.
_COUNTERS = frozenset({'Deprive', 'Negate'})
_REMOVAL = frozenset(
    {
        'Day of Judgment',
        'Gideon Jura',
        'Jace, the Mind Sculptor',
        'Martial Coup',
        'Oblivion Ring',
        'Path to Exile',
        'Tectonic Edge',
    }
)
_INTERACTION = _COUNTERS | _REMOVAL
_COSTS = {
    'Day of Judgment': 4,
    'Deprive': 2,
    'Gideon Jura': 5,
    'Jace, the Mind Sculptor': 4,
    'Martial Coup': 2,
    'Negate': 2,
    'Oblivion Ring': 3,
    'Path to Exile': 1,
    'Tectonic Edge': 0,
}
_LANDS = frozenset(
    {
        'Arid Mesa',
        'Celestial Colonnade',
        'Glacial Fortress',
        'Island',
        'Kabira Crossroads',
        'Misty Rainforest',
        'Plains',
        'Sejiri Steppe',
        'Tectonic Edge',
    }
)


def _log() -> str:
    return FIXTURE.read_text(errors='replace')


def _profile() -> PilotingProfile:
    return extract_piloting(
        _log(),
        candidate_slot='a',
        counters=_COUNTERS,
        removal=_REMOVAL,
        interaction=_INTERACTION,
        costs=_COSTS,
        lands=_LANDS,
    )


# --------------------------------------------------------------------------- #
# PilotingProfile — same shape as Forge, computed by the SAME parser.
# --------------------------------------------------------------------------- #


def test_games_split_on_gameend() -> None:
    assert _profile().games == 4


def test_counter_fire_is_positive_the_cp7_signature() -> None:
    # THE differentiator: CP7 casts counters, so counter-fire > 0 here (Forge ~0).
    p = _profile()
    assert p.counter_opps == 8
    assert p.counter_casts == 4
    assert p.counter_fire is not None and p.counter_fire > 0
    assert p.counter_ci is not None


def test_removal_opportunities_and_casts() -> None:
    p = _profile()
    assert p.removal_opps == 11
    assert p.removal_casts == 6
    assert p.removal_fire is not None and 0.0 < p.removal_fire <= 1.0


def test_stranded_interaction_per_game() -> None:
    assert _profile().interaction_stranded_per_game == 0.5


# --------------------------------------------------------------------------- #
# GameFeatures — kill_turn / wincon / margin all parse from the XMage feature
# lines (Turn:/Life:/Damage:/Game Result:), same as Forge.
# --------------------------------------------------------------------------- #


def test_game_features_full_parity() -> None:
    feats = extract_match_features(_log(), deck_a='UW', deck_b='UW')
    assert len(feats) == 4
    # Every decided game has a kill turn, a combat wincon, and a win margin — the
    # Damage: line carries XMage's OWN "at combat" flag (not a step-heuristic guess).
    for f in feats:
        assert f.kill_turn is not None
        assert f.wincon == 'combat'
        assert f.win_margin_life is not None
        assert f.game_length_ms is not None
    assert [f.winner for f in feats] == ['b', 'b', 'a', 'b']
    assert [f.kill_turn for f in feats] == [20, 27, 15, 29]


def test_never_raises_on_empty_or_garbage() -> None:
    # Same hard robustness contract as the Forge path (shared parser).
    for bad in ('', 'not a handlog\nrandom\n', 'HANDLOG turn=oops event='):
        p = extract_piloting(
            bad, counters=_COUNTERS, removal=_REMOVAL, interaction=_INTERACTION, costs=_COSTS, lands=_LANDS
        )
        assert p.games == 0
        assert extract_match_features(bad, deck_a='UW', deck_b='UW') == []
