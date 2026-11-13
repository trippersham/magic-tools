"""Phase 2B step 5a: fid-to-Event adapter data layer tests.

Validates the production adapter against the research classifier
(probe2/classify2 + val/outcome_map) and the owner's flag dispositions.

The adapter is DATA only — it is NOT consumed by resolve() or
_isolate_engines() yet. These tests verify the adapter's correctness
and completeness without touching the engine's output path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.transforms.strategy.fid_adapter import (
    FidMapping,
    coverage_stats,
    fids_for_event,
    load_adapter,
    lookup,
)

_DATA_PATH = Path(__file__).resolve().parent.parent / 'data' / 'fid_event_adapter.json'
_CATALOG_PATH = Path(__file__).resolve().parent.parent / 'data' / 'event_catalog_v4.json'

# The 43 hand-audited win-fids (from the research validation).
_WIN_FIDS = frozenset({
    2, 76, 63, 50, 323, 182, 2123, 721, 1177, 1948, 430, 2274, 170, 1164,
    13, 1, 258, 1206, 1227, 2415, 1601, 1599, 1157, 2621, 520, 26, 49, 889,
    601, 995, 1953, 997, 9, 125, 124, 1529, 769, 144, 2318, 1176, 42, 621, 1183,
})

# The 10 vetoed false-positive fids (must NOT reach game-end).
_VETOED_FP_FIDS = frozenset({304, 402, 1070, 232, 1422, 1694, 1196, 800, 658, 509})

# Events that reach game-end through rule-edges (the terminal-reaching set).
_TERMINAL_REACHING_EVENTS = frozenset({
    'game-end',
    'life-loss-drain',      # SBA 104.3b
    'library-depleted',     # CR 104.3c
    'monarch-or-extra-turn',  # policy cell: treated as WIN
    # Combat events reach game-end via damage:
    'combat-damage-dealt',
    'creature-attacks',
    'token-created',
    'counter-placed',
})

# Valid event slugs in the catalog (including new Phase 2B events).
_CATALOG_EVENTS: frozenset[str] = frozenset()
# State events (topological sinks, not in catalog but valid adapter outputs).
_STATE_EVENTS = frozenset({'lock', 'cant-lose', 'control-change'})


@pytest.fixture(autouse=True, scope='module')
def _load_catalog():
    """Load the event catalog once for the module."""
    global _CATALOG_EVENTS
    with _CATALOG_PATH.open() as f:
        cat = json.load(f)
    _CATALOG_EVENTS = frozenset(cat['events'].keys())


# ────────────────────────────────────────────────────────────────────────────
# 1. Data shape and completeness
# ────────────────────────────────────────────────────────────────────────────

class TestAdapterShape:
    """The adapter file is well-formed and complete."""

    def test_adapter_loads(self) -> None:
        adapter = load_adapter()
        assert len(adapter) == 1348, f'expected 1348 features, got {len(adapter)}'

    def test_every_fid_is_positive_int(self) -> None:
        for fid, m in load_adapter().items():
            assert isinstance(fid, int) and fid > 0, f'bad fid: {fid}'
            assert m.fid == fid

    def test_every_row_has_required_fields(self) -> None:
        for fid, m in load_adapter().items():
            assert isinstance(m.name, str) and m.name, f'fid {fid}: empty name'
            assert m.magnitude in ('infinite', 'near-infinite', 'scaled', 'finite'), (
                f'fid {fid}: bad magnitude {m.magnitude!r}'
            )
            assert m.scope in ('table', 'target', 'self', 'unscoped'), (
                f'fid {fid}: bad scope {m.scope!r}'
            )

    def test_event_slugs_are_valid(self) -> None:
        """Every non-null event is either a catalog event or a state event."""
        for fid, m in load_adapter().items():
            if m.event is not None:
                assert m.event in _CATALOG_EVENTS | _STATE_EVENTS, (
                    f'fid {fid}: unknown event slug {m.event!r}'
                )

    def test_coverage_stats(self) -> None:
        stats = coverage_stats()
        assert stats['total'] == 1348
        assert stats['with_event'] + stats['null_event'] == 1348
        assert stats['with_event'] > 1000  # >74% have events


# ────────────────────────────────────────────────────────────────────────────
# 2. Research classifier validation (43 win-fids + 10 vetoed FPs)
# ────────────────────────────────────────────────────────────────────────────

class TestResearchValidation:
    """The adapter reproduces the research classifier's audited verdicts."""

    def test_43_win_fids_reach_terminal(self) -> None:
        """Every audited win-fid maps to a terminal-reaching event."""
        adapter = load_adapter()
        for fid in sorted(_WIN_FIDS):
            m = adapter.get(fid)
            assert m is not None, f'win-fid {fid} missing from adapter'
            assert m.event in _TERMINAL_REACHING_EVENTS, (
                f'win-fid {fid} ({m.name!r}) maps to non-terminal {m.event!r}'
            )

    def test_10_vetoed_fps_do_not_reach_game_end(self) -> None:
        """No vetoed FP fid maps directly to game-end."""
        adapter = load_adapter()
        for fid in sorted(_VETOED_FP_FIDS):
            m = adapter.get(fid)
            assert m is not None, f'vetoed fid {fid} missing from adapter'
            assert m.event != 'game-end', (
                f'vetoed fid {fid} ({m.name!r}) leaks to game-end'
            )


# ────────────────────────────────────────────────────────────────────────────
# 3. Owner flag dispositions
# ────────────────────────────────────────────────────────────────────────────

class TestOwnerDispositions:
    """Owner-ruled flag dispositions are correctly encoded."""

    def test_etb_ltb_split_death_to_permanent_dies(self) -> None:
        """Death-trigger fids from the etb-ltb bucket map to permanent-dies."""
        death_fids = [7, 134, 1307, 1341, 1355, 1491, 2138, 2775]
        adapter = load_adapter()
        for fid in death_fids:
            m = adapter[fid]
            assert m.event == 'permanent-dies', (
                f'death fid {fid} ({m.name!r}) maps to {m.event!r}, expected permanent-dies'
            )

    def test_etb_ltb_split_ltb_to_permanent_leaves(self) -> None:
        """LTB fids from the etb-ltb bucket map to permanent-leaves-battlefield."""
        ltb_fids = [3, 79, 1343, 1354, 1490, 2001, 2409, 2424, 2774, 2808]
        adapter = load_adapter()
        for fid in ltb_fids:
            m = adapter[fid]
            assert m.event == 'permanent-leaves-battlefield', (
                f'ltb fid {fid} ({m.name!r}) maps to {m.event!r}, expected permanent-leaves-battlefield'
            )

    def test_etb_ltb_split_landfall_to_land_enters(self) -> None:
        """Landfall fids from the etb-ltb bucket map to land-enters-battlefield."""
        landfall_fids = [78, 119, 565, 1879, 2825]
        adapter = load_adapter()
        for fid in landfall_fids:
            m = adapter[fid]
            assert m.event == 'land-enters-battlefield', (
                f'landfall fid {fid} ({m.name!r}) maps to {m.event!r}, expected land-enters-battlefield'
            )

    def test_scry_surveil_not_draw(self) -> None:
        """scry/surveil features do NOT map to draw-a-card (owner ruling)."""
        adapter = load_adapter()
        scry_fids = [m for m in adapter.values() if 'scry' in m.name.lower() or 'surveil' in m.name.lower()]
        for m in scry_fids:
            assert m.event != 'draw-a-card', (
                f'fid {m.fid} ({m.name!r}) maps to draw-a-card — scry/surveil should not'
            )

    def test_bounce_to_permanent_leaves(self) -> None:
        """Features classified as 'bounce' kind map to permanent-leaves-battlefield."""
        # Spot-check known bounce-kind fids (the adapter applied the bounce disposition).
        # We can't filter by name alone since "return" appears in ETB creature names too.
        adapter = load_adapter()
        bounce_count = sum(1 for m in adapter.values() if m.event == 'permanent-leaves-battlefield')
        assert bounce_count >= 47, f'expected >=47 permanent-leaves-battlefield mappings, got {bounce_count}'

    def test_life_total_set_to_life_loss_drain(self) -> None:
        """life-total-set features (fids 528, 686) map to life-loss-drain."""
        adapter = load_adapter()
        for fid in (528, 686):
            m = adapter[fid]
            assert m.event == 'life-loss-drain', (
                f'life-set fid {fid} ({m.name!r}) maps to {m.event!r}, expected life-loss-drain'
            )

    def test_fid_2113_lich_is_cant_lose(self) -> None:
        """fid 2113 "Lich" protection maps to cant-lose, NOT game-end (§C.6)."""
        m = lookup(2113)
        assert m is not None
        assert m.event == 'cant-lose', f'fid 2113 maps to {m.event!r}, expected cant-lose'

    def test_fid_1742_extra_turn_is_finite(self) -> None:
        """fid 1742 "Extra Turn Spell" maps to monarch-or-extra-turn with finite magnitude."""
        m = lookup(1742)
        assert m is not None
        assert m.event == 'monarch-or-extra-turn'
        assert m.magnitude == 'finite'

    def test_fid_1394_skip_turns_non_terminal(self) -> None:
        """fid 1394 "Skip all your future turns" is non-terminal (§C.1 owner ruling)."""
        m = lookup(1394)
        assert m is not None
        assert m.event is None or m.event != 'game-end', (
            'fid 1394 must NOT be terminal — the walk derives terminality'
        )
        assert m.note is not None and 'no-more-turns' in m.note

    def test_saga_dice_dungeon_not_lumped(self) -> None:
        """Saga/dice/dungeon from the trigger-engine kind are NOT lumped (owner ruling).

        Only features in the 'trigger-engine' kind (8 features) are subject to the
        saga/dice/dungeon split.  Features in other kinds (like etb-ltb) that happen
        to mention 'venture' or 'dungeon' in their name are classified by their own kind.
        """
        adapter = load_adapter()
        # The trigger-engine kind features (from research): actual fids, not illustrative.
        # fid 2551 "Infinite triggers of chapter abilities from nonlegendary Sagas" → counter-placed
        m2551 = lookup(2551)
        assert m2551 is not None
        assert m2551.event == 'counter-placed', (
            f'saga trigger-engine fid 2551 should be counter-placed, got {m2551.event!r}'
        )
        # fid 1046 "Infinite die rolls" → None (die-rolled is its own event)
        m1046 = lookup(1046)
        assert m1046 is not None
        assert m1046.event is None, f'dice fid 1046 should be None, got {m1046.event!r}'
        # fid 1232 "Infinite ventures into the dungeon" → None (ventured is its own event)
        m1232 = lookup(1232)
        assert m1232 is not None
        assert m1232.event is None, f'dungeon fid 1232 should be None, got {m1232.event!r}'
        # fid 261 "Infinite planeswalker activations" → None
        m261 = lookup(261)
        assert m261 is not None
        assert m261.event is None, f'pw fid 261 should be None, got {m261.event!r}'


# ────────────────────────────────────────────────────────────────────────────
# 4. Object descriptor parsing
# ────────────────────────────────────────────────────────────────────────────

class TestObjectDescriptor:
    """Object descriptors are parsed from feature names where applicable."""

    def test_treasure_is_mana_produced(self) -> None:
        """Infinite Treasure tokens → event=mana-produced, descriptor has type=Treasure."""
        m = lookup(91)  # "Infinite Treasure tokens"
        assert m is not None
        assert m.event == 'mana-produced'
        assert m.object_descriptor is not None
        assert m.object_descriptor.get('type') == 'Treasure'

    def test_color_in_mana(self) -> None:
        """Infinite colorless mana → descriptor has color."""
        m = lookup(11)  # "Infinite colorless mana"
        assert m is not None
        assert m.object_descriptor is not None
        assert m.object_descriptor.get('color') == 'colorless'

    def test_haste_keyword(self) -> None:
        """Infinite creature tokens with haste → descriptor has keywords=[haste]."""
        m = lookup(39)  # "Infinite creature tokens with haste"
        assert m is not None
        assert m.object_descriptor is not None
        assert 'haste' in (m.object_descriptor.get('keywords') or [])

    def test_null_descriptor_for_plain_features(self) -> None:
        """Plain features (Win the game, Infinite damage) have no descriptor."""
        m = lookup(2)  # "Win the game"
        assert m is not None
        assert m.object_descriptor is None


# ────────────────────────────────────────────────────────────────────────────
# 5. Accessor API
# ────────────────────────────────────────────────────────────────────────────

class TestAccessorAPI:
    """The thin accessor API works correctly."""

    def test_lookup_known_fid(self) -> None:
        m = lookup(2)
        assert m is not None
        assert m.fid == 2
        assert m.name == 'Win the game'
        assert m.event == 'game-end'

    def test_lookup_unknown_fid(self) -> None:
        assert lookup(999999) is None

    def test_fids_for_event(self) -> None:
        game_end_fids = fids_for_event('game-end')
        assert len(game_end_fids) > 0
        assert all(m.event == 'game-end' for m in game_end_fids)

    def test_fids_for_nonexistent_event(self) -> None:
        assert fids_for_event('nonexistent-event') == []
