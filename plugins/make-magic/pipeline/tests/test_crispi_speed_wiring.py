"""Phase 4 — the ``crispi_from_deck`` Speed wiring: --fundamental-turn optional.

Supplied turn OVERRIDES (router never called); omitted AUTO-computes via the router;
an auto Speed-N/A raises ``SpeedNotApplicable`` (the contract can't represent N/A, so
we refuse to fabricate a turn). Offline: the otag/combo lakes and the router are stubbed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pipeline.contracts import DeckCard
from pipeline.contracts.models import Deck
from pipeline.sim.speed import FundamentalTurn


def _df():
    scripts_dir = Path(__file__).resolve().parents[2] / 'scripts'
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import deck_factsheet  # type: ignore[import-not-found]

    return deck_factsheet


def _deck() -> Deck:
    return Deck(
        name='Wiring Deck',
        cards=[DeckCard(name='Lightning Bolt', type_line='Instant', mana_value=1.0, quantity=4)],
    )


@pytest.fixture
def stub_lakes(monkeypatch: pytest.MonkeyPatch):
    df = _df()
    monkeypatch.setattr(df, '_load_card_otag', lambda: {})
    monkeypatch.setattr(df, '_crispi_combos', lambda names: [])
    return df


def test_supplied_turn_overrides_router(stub_lakes, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.sim.speed as speed

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError('router must NOT be called when --fundamental-turn is supplied')

    monkeypatch.setattr(speed, 'fundamental_turn', _boom)
    out = stub_lakes.crispi_from_deck(_deck(), fundamental_turn=6.0, commander_dependence='low')
    assert out['inputs']['fundamental_turn'] == 6.0
    assert 'speed_source' not in out  # manual path carries no provenance.


def test_omitted_turn_auto_computes(stub_lakes, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.sim.engine as engine_mod
    import pipeline.sim.speed as speed

    monkeypatch.setattr(engine_mod, 'get_engine', lambda name: (_ for _ in ()).throw(RuntimeError('no jar')))
    monkeypatch.setattr(
        speed, 'fundamental_turn',
        lambda *a, **k: FundamentalTurn(turn=4.0, confidence='high', tier='tier1', source_rationale='stub'),
    )
    out = stub_lakes.crispi_from_deck(_deck(), commander_dependence='low')
    assert out['inputs']['fundamental_turn'] == 4.0
    assert out['speed_source']['tier'] == 'tier1'


def test_auto_speed_na_raises(stub_lakes, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.sim.engine as engine_mod
    import pipeline.sim.speed as speed

    monkeypatch.setattr(engine_mod, 'get_engine', lambda name: (_ for _ in ()).throw(RuntimeError('no jar')))
    monkeypatch.setattr(
        speed, 'fundamental_turn',
        lambda *a, **k: FundamentalTurn(turn=None, confidence='n/a', tier='na', source_rationale='control'),
    )
    with pytest.raises(stub_lakes.SpeedNotApplicable):
        stub_lakes.crispi_from_deck(_deck(), commander_dependence='low')
