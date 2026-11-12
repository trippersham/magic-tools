"""Scryfall (RE2) pasteability guard for `oracle:/.../` / `type:/.../` patterns.

The invariant: a regex pattern that compiles through :func:`compile_query`
pastes natively into Scryfall's ``oracle:/.../`` operator. The guard rejects the
constructs that provably break that — a bare space (Scryfall's term tokenizer
splits on it), a backreference, lookaround, or a conditional (none in RE2) — so
"compiles locally => pastes to Scryfall" holds.
"""

from __future__ import annotations

import pytest

from pipeline.transforms.strategy.query import CardView, compile_query


def test_bare_space_pattern_is_rejected() -> None:
    """A literal space tears the pattern across Scryfall's term split -> reject."""
    with pytest.raises(ValueError, match='bare space'):
        compile_query('oracle:/copy of/')


def test_lookahead_pattern_is_rejected() -> None:
    """RE2 has no lookaround -> reject a lookahead pattern."""
    with pytest.raises(ValueError, match='lookaround'):
        compile_query('oracle:/draw(?=.*lose)/')


def test_lookbehind_and_negative_forms_are_rejected() -> None:
    for pat in ('oracle:/(?<=draw)card/', 'oracle:/draw(?!s)/', 'oracle:/x(?<!y)/'):
        with pytest.raises(ValueError, match='lookaround'):
            compile_query(pat)


def test_backreference_is_rejected() -> None:
    with pytest.raises(ValueError, match='backreference'):
        compile_query('oracle:/(draw).*\\1/')


def test_conditional_is_rejected() -> None:
    with pytest.raises(ValueError, match='conditional'):
        compile_query('oracle:/(a)(?(1)b)/')


def test_clean_proximity_pattern_compiles_and_matches() -> None:
    """A clean `[^.]{0,N}` proximity pattern compiles and matches expected text."""
    pred = compile_query('oracle:/draw[^.]{0,30}lose/')
    hit = CardView.from_card(
        {'oracle_text': 'You draw two cards, then you lose 2 life.'})
    miss = CardView.from_card(
        {'oracle_text': 'You draw two cards. You gain 2 life.'})
    assert pred(hit) is True
    assert pred(miss) is False


def test_escaped_space_and_whitespace_class_are_allowed() -> None:
    """`\\s` and an escaped space are pasteable (only a BARE space breaks)."""
    # Both compile without raising.
    compile_query(r'oracle:/copy\sof/')
    compile_query(r'oracle:/copy\ of/')


def test_bounded_repeat_over_budget_is_rejected() -> None:
    """Scryfall/RE2 caps summed bounded-repeat expansion at 50 (pinned live)."""
    with pytest.raises(ValueError, match='bounded-repeat budget'):
        compile_query('oracle:/dies[^.]{0,70}copy/')          # single 70 > 50
    with pytest.raises(ValueError, match='bounded-repeat budget'):
        compile_query('oracle:/gain[^.]{0,40}life[^.]{0,40}lose/')  # 40+40=80 > 50


def test_bounded_repeat_at_and_under_budget_compiles() -> None:
    """Exactly 50 (single or split) pastes; unbounded forms never count."""
    compile_query('oracle:/dies[^.]{0,50}copy.of/')           # 50 == budget
    compile_query('oracle:/gain[^.]{0,25}life[^.]{0,25}lose/')  # 25+25 == 50
    compile_query('oracle:/dies[^.]*copy.of/')                # unbounded -> 0
    compile_query('oracle:/dies[^.]{3,}copy.of/')             # {m,} -> 0


def test_type_regex_form_is_guarded_too() -> None:
    with pytest.raises(ValueError, match='bare space'):
        compile_query('type:/Legendary Creature/')
    pred = compile_query('type:/Legendary.Creature/')
    assert pred(CardView.from_card({'type_line': 'Legendary Creature — God'})) is True
