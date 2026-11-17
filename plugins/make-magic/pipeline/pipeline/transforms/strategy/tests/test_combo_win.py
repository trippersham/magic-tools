"""Phase 2B step 5b-ii: closure-mechanism win-walk tests (ADR 0016).

Tests for the AUTHORITATIVE combo win-detection closure walk that replaces
the regex path. The walk is the source of truth for ``resolve()`` / combo
flags / DRIVE. It is imported by strategy_graph.py and determines every combo's win
flag.

TDD: tests from 5b-i updated for the flip — shadow discipline tests replaced
by authoritative-wiring tests.
"""

from __future__ import annotations

import pytest

from pipeline.transforms.strategy.fid_adapter import load_adapter

# ────────────────────────────────────────────────────────────────────────────
# 1. The outcome classifier: fid → route record
# ────────────────────────────────────────────────────────────────────────────


class TestFidToRoute:
    """Each fid's adapter mapping classifies into a walk route."""

    def test_game_end_fid_is_terminal_win(self) -> None:
        """fid 2 'Win the game' → route=terminal-win, timing=immediate."""
        from pipeline.transforms.strategy.combo_win import fid_route

        r = fid_route(2)
        assert r is not None
        assert r['route'] == 'terminal-win'
        assert r['timing'] == 'immediate'

    def test_sba_drain_fid_is_rule_closed(self) -> None:
        """fid 1 'Near-infinite damage' → route=sba-drain (rule-edge to game-end)."""
        from pipeline.transforms.strategy.combo_win import fid_route

        r = fid_route(1)
        assert r is not None
        assert r['route'] == 'sba-drain'

    def test_infinite_mana_is_intermediate(self) -> None:
        """fid 11 'Infinite colorless mana' → route=intermediate, event=mana-produced."""
        from pipeline.transforms.strategy.combo_win import fid_route

        r = fid_route(11)
        assert r is not None
        assert r['route'] == 'intermediate'
        assert r['event'] == 'mana-produced'

    def test_infinite_tokens_sans_haste_is_combat_deferred(self) -> None:
        """fid 21 'Infinite creature tokens' (no haste) → route=combat, timing=deferred."""
        from pipeline.transforms.strategy.combo_win import fid_route

        r = fid_route(21)
        assert r is not None
        assert r['route'] == 'combat'
        assert r['timing'] == 'deferred'

    def test_infinite_tokens_with_haste_is_combat_immediate(self) -> None:
        """fid 39 'Infinite creature tokens with haste' → route=combat, timing=immediate."""
        from pipeline.transforms.strategy.combo_win import fid_route

        r = fid_route(39)
        assert r is not None
        assert r['route'] == 'combat'
        assert r['timing'] == 'immediate'

    def test_finite_combat_prevention_is_oneshot_not_win(self) -> None:
        """Finite combat fids (defensive "can't attack" locks) → route=oneshot, NOT combat.

        ADR 0016 / win-walk Item 1: the two direct-combat branches in ``fid_route``
        are magnitude-guarded like every other route.  fids 48 ("Opponents can't
        attack…"), 213 ("You can't be attacked"), 197 ("Creatures can't attack")
        are finite defensive prevention — they must NOT register as combat wins.
        """
        from pipeline.transforms.strategy.combo_win import fid_route

        for fid in (48, 213, 197):
            r = fid_route(fid)
            assert r is not None
            assert r['magnitude'] == 'finite'
            assert r['route'] == 'oneshot', f'finite combat fid {fid} must be oneshot, got {r["route"]}'

    def test_infinite_combat_damage_is_combat_win(self) -> None:
        """Infinite combat fids still route to combat (guard retains genuine wins).

        fid 205 ('Infinite combat damage', combat-damage-dealt) and fid 2800
        ('Infinitely powerful attacking creatures', creature-attacks) are unbounded
        → route=combat/immediate (the 581 genuine combat wins are preserved).
        """
        from pipeline.transforms.strategy.combo_win import fid_route

        for fid in (205, 2800):
            r = fid_route(fid)
            assert r is not None
            assert r['route'] == 'combat'
            assert r['timing'] == 'immediate'

    def test_library_depleted_opp_is_deckout(self) -> None:
        """fid 26 'Infinite lifeloss' → route=sba-drain."""
        from pipeline.transforms.strategy.combo_win import fid_route

        r = fid_route(26)
        assert r is not None
        assert r['route'] == 'sba-drain'

    def test_lock_state_is_state(self) -> None:
        """A lock event fid → route=state."""
        from pipeline.transforms.strategy.combo_win import fid_route

        adapter = load_adapter()
        lock_fids = [fid for fid, m in adapter.items() if m.event == 'lock']
        assert len(lock_fids) > 0
        r = fid_route(lock_fids[0])
        assert r is not None
        assert r['route'] == 'state'

    def test_null_event_fid_is_inert(self) -> None:
        """A fid with event=None → route=inert (no walk participation)."""
        from pipeline.transforms.strategy.combo_win import fid_route

        adapter = load_adapter()
        null_fids = [fid for fid, m in adapter.items() if m.event is None]
        assert len(null_fids) > 0
        r = fid_route(null_fids[0])
        assert r is not None
        assert r['route'] in ('inert', 'oneshot', 'meta')

    def test_deckout_opp_fids(self) -> None:
        """Mill/library-depletion fids scoped at opponents → deckout-opp."""
        from pipeline.transforms.strategy.combo_win import fid_route

        r = fid_route(9)
        assert r is not None
        assert r['route'] == 'deckout-opp'

    def test_self_draw_is_intermediate_with_selfdep_risk(self) -> None:
        """fid 61 'Infinite draw triggers' → intermediate with self-deplete risk."""
        from pipeline.transforms.strategy.combo_win import fid_route

        r = fid_route(61)
        assert r is not None
        assert r['event'] == 'draw-a-card'
        assert r.get('risk') == 'self-deplete' or r['route'] == 'intermediate'


# ────────────────────────────────────────────────────────────────────────────
# 2. The closure walk: combo + deck → verdict
# ────────────────────────────────────────────────────────────────────────────


class TestClosureWalk:
    """The closure walk computes a verdict from a combo's produced events + deck cards."""

    def test_rule_closed_immediate(self) -> None:
        """A combo producing 'Win the game' (fid 2) is rule-closed immediate."""
        from pipeline.transforms.strategy.combo_win import combo_win_verdict

        result = combo_win_verdict(fids=[2], deck_card_names=set())
        assert result['kind'] == 'rule'
        assert result['polarity'] == 'win'
        assert result['timing'] == 'immediate'

    def test_sba_drain_is_rule_closed(self) -> None:
        """A combo producing infinite damage (fid 1) is rule-closed via SBA."""
        from pipeline.transforms.strategy.combo_win import combo_win_verdict

        result = combo_win_verdict(fids=[1], deck_card_names=set())
        assert result['kind'] == 'rule'
        assert result['polarity'] == 'win'

    def test_infinite_mana_without_sink_is_unclosable(self) -> None:
        """Infinite mana (fid 11) with no sink in deck → unclosable."""
        from pipeline.transforms.strategy.combo_win import combo_win_verdict

        result = combo_win_verdict(fids=[11], deck_card_names={'sol ring', 'island'})
        assert result['kind'] in ('card-needed', 'unclosable')

    def test_infinite_mana_with_walking_ballista_is_card_closed(self) -> None:
        """Infinite mana + Walking Ballista in deck → card-closed win."""
        from pipeline.transforms.strategy.combo_win import combo_win_verdict

        result = combo_win_verdict(
            fids=[11],
            deck_card_names={'walking ballista', 'sol ring'},
        )
        assert result['kind'] == 'card'
        assert result['polarity'] == 'win'

    def test_infinite_mana_with_exsanguinate_is_card_closed(self) -> None:
        """Infinite mana + Exsanguinate in deck → card-closed win."""
        from pipeline.transforms.strategy.combo_win import combo_win_verdict

        result = combo_win_verdict(
            fids=[11],
            deck_card_names={'exsanguinate', 'swamp'},
        )
        assert result['kind'] == 'card'
        assert result['polarity'] == 'win'

    def test_combat_deferred_is_action_closed(self) -> None:
        """Infinite creature tokens (no haste, fid 21) → action-closed deferred."""
        from pipeline.transforms.strategy.combo_win import combo_win_verdict

        result = combo_win_verdict(fids=[21], deck_card_names=set())
        assert result['kind'] == 'action'
        assert result['timing'] == 'deferred'

    def test_combat_immediate_is_action_closed(self) -> None:
        """Infinite creature tokens with haste → action-closed immediate."""
        from pipeline.transforms.strategy.combo_win import combo_win_verdict

        result = combo_win_verdict(fids=[39], deck_card_names=set())
        assert result['kind'] == 'action'
        assert result['timing'] == 'immediate'


# ────────────────────────────────────────────────────────────────────────────
# 3. Guard evaluation: scope + characteristic
# ────────────────────────────────────────────────────────────────────────────


class TestGuardEvaluation:
    """Guards prevent false promotions (Sheoldred on self-draw, etc.)."""

    def test_sheoldred_does_not_promote_on_self_draw(self) -> None:
        """Sheoldred's draw-to-drain edge fires on OPPONENT draw only."""
        from pipeline.transforms.strategy.combo_win import combo_win_verdict

        result = combo_win_verdict(
            fids=[49],
            deck_card_names={'sheoldred, the apocalypse'},
        )
        assert (
            result['polarity'] != 'win'
            or result['kind'] != 'card'
            or (result.get('detail') != 'sheoldred, the apocalypse')
        ), 'Sheoldred must NOT promote self-draw to a win'

    def test_aragorn_does_not_promote_on_wrong_color(self) -> None:
        """Aragorn the Uniter's damage trigger fires on RED casts only."""
        from pipeline.transforms.strategy.combo_win import combo_win_verdict

        adapter = load_adapter()
        cast_fids = [fid for fid, m in adapter.items() if m.event == 'spell-cast' and m.magnitude == 'infinite']
        assert len(cast_fids) > 0, 'Need an infinite spell-cast fid'
        result = combo_win_verdict(
            fids=[cast_fids[0]],
            deck_card_names={'aragorn, the uniter'},
        )
        if result['kind'] == 'card' and result.get('detail') == 'aragorn, the uniter':
            pytest.fail('Aragorn must NOT promote non-red spell casts to a win')

    def test_infinity_dominance_satisfies_ordinal_gate(self) -> None:
        """Infinite mana + Helix Pinnacle → card-closed win."""
        from pipeline.transforms.strategy.combo_win import combo_win_verdict

        result = combo_win_verdict(
            fids=[11],
            deck_card_names={'helix pinnacle'},
        )
        assert result['kind'] == 'card'
        assert result['polarity'] == 'win'

    def test_self_loss_not_called_win(self) -> None:
        """Self-mill without an outlet is NOT a win."""
        from pipeline.transforms.strategy.combo_win import combo_win_verdict

        adapter = load_adapter()
        self_mill_fids = [
            fid
            for fid, m in adapter.items()
            if m.event == 'card-enters-graveyard' and 'self' in (m.name or '').lower() and m.magnitude == 'infinite'
        ]
        if not self_mill_fids:
            self_mill_fids = [
                fid
                for fid, m in adapter.items()
                if m.event == 'library-depleted'
                and 'your' in (m.name or '').lower()
                and m.magnitude in ('infinite', 'near-infinite')
            ]
        assert len(self_mill_fids) > 0, 'Need a self-deplete fid'
        result = combo_win_verdict(
            fids=[self_mill_fids[0]],
            deck_card_names={'swamp', 'island'},
        )
        assert result['polarity'] != 'win', 'Self-loss must not be called a win'


# ────────────────────────────────────────────────────────────────────────────
# 4. Sink dissolution: mana→drain via card-edge, not frozen list
# ────────────────────────────────────────────────────────────────────────────


class TestSinkDissolution:
    """The lethal mana sink is computed from EventEffects, not the frozen list."""

    def test_fireball_is_a_mana_sink(self) -> None:
        """Fireball converts infinite mana to life-loss-drain → game-end."""
        from pipeline.transforms.strategy.combo_win import combo_win_verdict

        result = combo_win_verdict(
            fids=[11],
            deck_card_names={'fireball'},
        )
        assert result['kind'] == 'card'
        assert result['polarity'] == 'win'

    def test_aetherflux_reservoir_via_spell_cast(self) -> None:
        """Aetherflux Reservoir: spell-cast → drain (cast_drain edge class)."""
        from pipeline.transforms.strategy.combo_win import combo_win_verdict

        adapter = load_adapter()
        cast_fids = [fid for fid, m in adapter.items() if m.event == 'spell-cast' and m.magnitude == 'infinite']
        assert len(cast_fids) > 0
        result = combo_win_verdict(
            fids=[cast_fids[0]],
            deck_card_names={'aetherflux reservoir'},
        )
        assert result['kind'] == 'card'
        assert result['polarity'] == 'win'

    def test_impact_tremors_via_etb(self) -> None:
        """Impact Tremors: creature-ETB → life-loss-drain (etb_drain edge class)."""
        from pipeline.transforms.strategy.combo_win import combo_win_verdict

        result = combo_win_verdict(
            fids=[4],
            deck_card_names={'impact tremors'},
        )
        assert result['kind'] == 'card'
        assert result['polarity'] == 'win'

    def test_blood_artist_via_death(self) -> None:
        """Blood Artist: permanent-dies → life-loss-drain (death_drain edge class)."""
        from pipeline.transforms.strategy.combo_win import combo_win_verdict

        adapter = load_adapter()
        death_fids = [fid for fid, m in adapter.items() if m.event == 'permanent-dies' and m.magnitude == 'infinite']
        assert len(death_fids) > 0
        result = combo_win_verdict(
            fids=[death_fids[0]],
            deck_card_names={'blood artist'},
        )
        assert result['kind'] == 'card'
        assert result['polarity'] == 'win'


# ────────────────────────────────────────────────────────────────────────────
# 5. Authoritative wiring: combo_win IS the resolve path
# ────────────────────────────────────────────────────────────────────────────


class TestAuthoritativeWiring:
    """The win walk IS wired into the resolve path (replaces shadow discipline)."""

    def test_combo_win_module_imported_by_strategy_graph(self) -> None:
        """combo_win.py IS imported by strategy_graph.py (the authoritative path)."""
        import sys

        mod_name = 'pipeline.transforms.strategy.combo_win'
        prefix = 'pipeline.transforms.strategy'
        # Snapshot so the fresh-import probe below does not leak duplicate class
        # objects (e.g. machine_vocab.Provenance) into later tests' sys.modules.
        saved = {m: sys.modules[m] for m in list(sys.modules) if m.startswith(prefix)}
        try:
            # Clear to test fresh import chain
            for m in saved:
                del sys.modules[m]
            import pipeline.transforms.strategy.strategy_graph  # noqa: F401

            assert mod_name in sys.modules, (
                'combo_win is NOT imported by strategy_graph.py — it MUST be (authoritative path)'
            )
        finally:
            # Restore the original module objects so class identity is preserved.
            for m in list(sys.modules):
                if m.startswith(prefix):
                    del sys.modules[m]
            sys.modules.update(saved)

    def test_regex_functions_removed_from_combo(self) -> None:
        """combo.py no longer exports is_game_win_result or LETHAL_MANA_SINK_NAMES."""
        import pipeline.transforms.strategy.combo as combo_mod

        assert not hasattr(combo_mod, 'is_game_win_result'), 'is_game_win_result still in combo.py — must be deleted'
        assert not hasattr(combo_mod, 'LETHAL_MANA_SINK_NAMES'), (
            'LETHAL_MANA_SINK_NAMES still in combo.py — must be deleted'
        )
        assert not hasattr(combo_mod, 'is_infinite_mana_result'), (
            'is_infinite_mana_result still in combo.py — must be deleted'
        )
        assert not hasattr(combo_mod, 'LETHAL_MANA_SINKS'), 'LETHAL_MANA_SINKS still in combo.py — must be deleted'

    def test_combo_detect_module_untouched(self) -> None:
        """combo_detect.py (sim driver layer) still has its own regex functions."""
        from pipeline.transforms.combo_detect import (
            is_game_win_result,
            is_infinite_mana_result,
        )

        # The sim driver's own copies are untouched
        assert is_game_win_result('Win the game') is True
        assert is_game_win_result('Infinite mana') is False
        assert is_infinite_mana_result('Infinite mana') is True

    def test_resolve_uses_walk_for_win_flag(self) -> None:
        """resolve() computes win_combos via the closure walk, not regex."""
        import json
        from pathlib import Path

        from pipeline.transforms.strategy.resolve import resolve

        fix = Path(__file__).resolve().parent / 'fixtures'
        combos = json.loads((fix / 'combos.json').read_text())

        # Test with a deck that has combos (use any fixture deck)
        deck_files = sorted((fix / 'decks').glob('*.json'))
        assert len(deck_files) > 0

        for df in deck_files[:3]:
            deck = json.loads(df.read_text())
            rec = resolve(deck, list(combos))
            # n_win_combos should be computed from the walk
            assert isinstance(rec['n_win_combos'], int)
            # mana_sinks_present should be empty (frozen list dissolved)
            assert rec['mana_sinks_present'] == [], (
                f'mana_sinks_present should be empty (frozen list dissolved), got {rec["mana_sinks_present"]}'
            )

    def test_old_walk_modules_do_not_exist(self) -> None:
        """The old shadow_walk.py / win_walk.py modules no longer exist (renamed to combo_win.py)."""
        from pathlib import Path

        strategy_dir = Path(__file__).resolve().parent.parent
        assert not (strategy_dir / 'shadow_walk.py').exists(), 'shadow_walk.py still exists — it was renamed'
        assert not (strategy_dir / 'win_walk.py').exists(), (
            'win_walk.py still exists — it should be renamed to combo_win.py'
        )
        assert (strategy_dir / 'combo_win.py').exists(), 'combo_win.py does not exist — win_walk.py should be renamed'
