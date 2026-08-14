"""TDD tests for the CRISPI edge contracts (``CrispiAxis`` / ``CrispiBracket`` /
``CrispiResult``).

Covers: a good example validates; range constraints reject bad values (axis
value 1..10, bracket 1..5); optional/nullable bracket; ``model_json_schema()``
works; a round-trip preserves the shape. Mirrors ``test_contracts.py`` style.

No network. Static contract only — the Phase 0 output shape.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pipeline.contracts import CrispiAxis, CrispiBracket, CrispiResult

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _axis(value: float = 6.0) -> CrispiAxis:
    return CrispiAxis(
        value=value,
        rationale='Deterministic count over the resolved list.',
        cited_cards=['Demonic Tutor', 'Rhystic Study'],
    )


def _result(**overrides: object) -> CrispiResult:
    base: dict[str, object] = {
        'consistency': _axis(6.0),
        'interaction': _axis(7.0),
        'speed': _axis(7.0),
        'resilience': _axis(5.0),
        'performance_index': 6.25,
        'bracket': None,
        'inputs': {'fundamental_turn': 8.5, 'commander_dependence': 'med'},
        'computed_at': '2026-08-10T00:00:00+00:00',
    }
    base.update(overrides)
    return CrispiResult(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# CrispiAxis — value + rationale + cited_cards
# --------------------------------------------------------------------------- #


def test_crispi_axis_good() -> None:
    axis = _axis(6.25)
    assert axis.value == 6.25
    assert axis.rationale
    assert axis.cited_cards == ['Demonic Tutor', 'Rhystic Study']


def test_crispi_axis_cited_cards_default_empty() -> None:
    axis = CrispiAxis(value=1.0, rationale='Nothing to cite.')
    assert axis.cited_cards == []


@pytest.mark.parametrize('value', [1.0, 5.5, 6.25, 10.0])
def test_crispi_axis_accepts_in_range(value: float) -> None:
    assert CrispiAxis(value=value, rationale='ok').value == value


@pytest.mark.parametrize('value', [0.0, 0.99, 10.25, 11.0, -3.0])
def test_crispi_axis_rejects_out_of_range(value: float) -> None:
    with pytest.raises(ValidationError):
        CrispiAxis(value=value, rationale='out of range')


def test_crispi_axis_missing_required_rejected() -> None:
    with pytest.raises(ValidationError):
        CrispiAxis(rationale='no value')  # type: ignore[call-arg]


def test_crispi_axis_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        CrispiAxis(value=5.0, rationale='ok', surprise='no')  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# CrispiBracket — bracket 1..5 + triggers (populated in Phase 6)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize('bracket', [1, 2, 3, 4, 5])
def test_crispi_bracket_accepts_valid(bracket: int) -> None:
    assert CrispiBracket(bracket=bracket).bracket == bracket


@pytest.mark.parametrize('bracket', [0, 6, -1, 10])
def test_crispi_bracket_rejects_out_of_range(bracket: int) -> None:
    with pytest.raises(ValidationError):
        CrispiBracket(bracket=bracket)


def test_crispi_bracket_triggers_default_empty() -> None:
    """Constructible now with no triggers — Phase 6 populates them."""
    b = CrispiBracket(bracket=3)
    assert b.triggers == []


def test_crispi_bracket_with_triggers() -> None:
    b = CrispiBracket(bracket=4, triggers=['Consistency 7.5 + Interaction 7.5 — competitive core'])
    assert b.bracket == 4
    assert b.triggers == ['Consistency 7.5 + Interaction 7.5 — competitive core']


def test_crispi_bracket_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        CrispiBracket(bracket=3, mystery='x')  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# CrispiResult — four axes + PI + bracket + inputs + stamp
# --------------------------------------------------------------------------- #


def test_crispi_result_good() -> None:
    r = _result()
    assert r.consistency.value == 6.0
    assert r.interaction.value == 7.0
    assert r.speed.value == 7.0
    assert r.resilience.value == 5.0
    assert r.performance_index == 6.25
    assert r.bracket is None
    assert r.inputs['fundamental_turn'] == 8.5
    assert r.inputs['commander_dependence'] == 'med'
    assert r.computed_at == '2026-08-10T00:00:00+00:00'


def test_crispi_result_bracket_nullable() -> None:
    """Bracket is None until the Phase 6 layer populates it."""
    assert _result(bracket=None).bracket is None


def test_crispi_result_with_bracket() -> None:
    r = _result(bracket=CrispiBracket(bracket=3, triggers=['CRISPI 6.25']))
    assert r.bracket is not None
    assert r.bracket.bracket == 3


def test_crispi_result_pi_in_range() -> None:
    with pytest.raises(ValidationError):
        _result(performance_index=11.0)
    with pytest.raises(ValidationError):
        _result(performance_index=0.0)


def test_crispi_result_rejects_bad_axis() -> None:
    """An out-of-range nested axis is rejected at the top level."""
    with pytest.raises(ValidationError):
        _result(resilience=CrispiAxis(value=99.0, rationale='bad'))


def test_crispi_result_missing_required_rejected() -> None:
    with pytest.raises(ValidationError):
        CrispiResult(  # type: ignore[call-arg]
            consistency=_axis(),
            interaction=_axis(),
            speed=_axis(),
            # resilience missing
            performance_index=6.0,
            inputs={'fundamental_turn': 7.0, 'commander_dependence': 'low'},
            computed_at='2026-08-10T00:00:00+00:00',
        )


def test_crispi_result_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        _result(surprise='no')


def test_crispi_result_roundtrip_model_dump() -> None:
    r = _result(bracket=CrispiBracket(bracket=3))
    restored = CrispiResult.model_validate(r.model_dump())
    assert restored.performance_index == r.performance_index
    assert restored.bracket is not None
    assert restored.bracket.bracket == 3
    assert restored.consistency.value == r.consistency.value


def test_crispi_result_json_schema() -> None:
    schema = CrispiResult.model_json_schema()
    assert schema['title'] == 'CrispiResult'
    # Nested models surface in $defs.
    assert 'CrispiAxis' in schema['$defs']
    assert 'CrispiBracket' in schema['$defs']


def test_crispi_axis_json_schema() -> None:
    assert CrispiAxis.model_json_schema()['title'] == 'CrispiAxis'


def test_crispi_bracket_json_schema() -> None:
    assert CrispiBracket.model_json_schema()['title'] == 'CrispiBracket'
