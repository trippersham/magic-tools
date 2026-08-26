"""Phase 4 guardrail tests (AC8) — the do-not-own discipline on the QUAD surface.

The external ``ComputerPlayer7``-subclass path is retired, so the old ``copy()``-override /
``selectAttackers`` test re-scopes to the quad's real-seat slots. A guardrail violation is a
rendered quad whose owned code reaches for something the design §7.1 do-not-own list forbids:

  * the macro's ``apply`` calls ``priority()`` or ``copy()`` on the handed sim game (it must
    drive the outcome with bounded explicit state moves, never re-enter the turn loop / copy);
  * any slot owns combat / land drops / attacker selection (``selectAttackers`` /
    ``selectBlockers`` / a forced ``declareAttacker``);
  * the S steer proxies a whole category by a SINGLE card name (category-altitude rule).

:func:`pipeline.sim.driver_authoring.check_quad_guardrails` enforces these statically over a
rendered quad; the emitter's own worked specs must pass, and deliberately-violating specs must
be rejected.
"""

from __future__ import annotations

import pytest

from pipeline.contracts.models import Deck, DeckCard
from pipeline.sim import driver_authoring as da


def _deck() -> Deck:
    return Deck(name='Guardrail Deck', cards=[DeckCard(name='Forest', quantity=1)])


def test_worked_specs_pass_the_guardrails() -> None:
    """The emitter's own positive controls (proactive Jeleva + Φ-only reactive) are clean."""
    for spec in (da.JELEVA_QUAD_SPEC, da.SHORIKAI_REACTIVE_QUAD_SPEC):
        src = da.render_quad_driver(_deck(), spec)
        da.check_quad_guardrails(src)  # raises on a violation


def test_macro_apply_calling_priority_is_rejected() -> None:
    """A macro whose ``apply`` re-enters the turn loop (``priority()`` on the handed game)
    corrupts the search — rejected (the retired ``copy()``-override test, re-scoped to the
    macro real-fire slot)."""
    bad = da.QuadSpec(
        name='bad-macro-priority',
        archetype='proactive',
        phi_body='return 0;',
        macro=da.MacroSpec(
            applicable_body='return true;',
            apply_body='Player me = game.getPlayer(pid);\nme.priority(game);',
        ),
    )
    src = da.render_quad_driver(_deck(), bad)
    with pytest.raises(da.GuardrailViolation, match='priority'):
        da.check_quad_guardrails(src)


def test_macro_apply_calling_copy_is_rejected() -> None:
    """A macro that copies the handed game (``game.copy()``) leaks driver logic into nested
    search copies — rejected."""
    bad = da.QuadSpec(
        name='bad-macro-copy',
        archetype='proactive',
        phi_body='return 0;',
        macro=da.MacroSpec(
            applicable_body='return true;',
            apply_body='Game g2 = game.copy();',
        ),
    )
    src = da.render_quad_driver(_deck(), bad)
    with pytest.raises(da.GuardrailViolation, match='copy'):
        da.check_quad_guardrails(src)


def test_owning_combat_is_rejected() -> None:
    """A quad that owns combat / attacker selection regresses CP7's competent combat — the
    force-attack HARD RULE (design §7.1 do-not-own)."""
    bad = da.QuadSpec(
        name='bad-owns-combat',
        archetype='proactive',
        phi_body='return 0;',
        helpers='private static void selectAttackers(Game game, UUID pid) {\n}',
    )
    src = da.render_quad_driver(_deck(), bad)
    with pytest.raises(da.GuardrailViolation, match=r'combat|attack'):
        da.check_quad_guardrails(src)


def test_land_drop_ownership_is_rejected() -> None:
    """Owning land drops / curve sequencing regresses CP7 (design §7.1 do-not-own)."""
    bad = da.QuadSpec(
        name='bad-owns-lands',
        archetype='proactive',
        phi_body='playLand(game, pid);\nreturn 0;',
    )
    src = da.render_quad_driver(_deck(), bad)
    with pytest.raises(da.GuardrailViolation, match='land'):
        da.check_quad_guardrails(src)
