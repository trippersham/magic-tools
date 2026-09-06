"""Phase 1 combo-litmus tests — the rule-1 drive gate (win-result predicate + win filter).

Pure logic, no JVM. Covers :func:`is_game_win_result` (the rule-1 win-result judgment),
:func:`win_combos_in_deck` (the drive gate = ``combos_in_deck`` filtered to game-wins), and
:func:`ensure_combo_lake` (the missing-parquet guard, exercised on a mocked temp data dir so
the real lake is never touched).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.transforms import combo_detect as cd
from pipeline.transforms.combo_detect import Combo

# --------------------------------------------------------------------------- #
# is_game_win_result — the rule-1 judgment                                    #
# --------------------------------------------------------------------------- #

_WIN_RESULTS = (
    'Win the game',
    'Each opponent loses the game',
    'Infinite mana; Win the game',
    'Deal infinite damage',
    'Infinite damage to each opponent',
    'You win the game',
    'Each opponent loses the game at the beginning of your next upkeep',
    # --- EP1 widened win-classes (mill / drain / poison / turns) --------------
    'Infinite mill for target opponent',
    'Infinite mill',
    'Infinite mill for target opponent; Near-infinite mill',
    'Infinite self-mill; Infinite mill for target opponent',  # opponent-mill clause present
    'Infinite mill; Near-infinite self-mill',
    'Infinite lifeloss',
    'Near-infinite lifeloss',
    'Infinite lifegain triggers; Infinite lifeloss; Infinite lifegain',  # lifeloss clause present
    'Near-infinite lifeloss for target opponent',
    'Infinite poison counters',
    'Infinite turns; Lock',
    'Near-infinite turns; Lock',
    "Exile target opponent's library",
    "Cast any number of spells from opponents' libraries; Exile each opponent's library",
)

_NON_WIN_RESULTS = (
    'Infinite mana',
    'Infinite colorless mana',
    'Infinite colored mana',
    'Infinite green mana',
    'Infinite card draw',
    'Infinite draw',
    'Infinite tokens',
    'Infinite mana; Infinite card draw',
    'Infinite creature tokens',
    'Infinite creature ETB',
    'Infinite loot',
    'Infinite storm count',
    # --- EP1 must-stay-FALSE guards (payoff-less resource loops) --------------
    'Infinite lifegain',
    'Infinite lifegain triggers; Infinite lifegain',
    'Infinite self-mill',
    'Near-infinite self-mill',
    'Infinite self-mill; Near-infinite self-discard triggers',
    'Exile your library; Cast all spells in your library',  # SELF library, not a deck-out
    '',
)


@pytest.mark.parametrize('result', _WIN_RESULTS)
def test_is_game_win_result_positive(result: str) -> None:
    assert cd.is_game_win_result(result) is True


@pytest.mark.parametrize('result', _NON_WIN_RESULTS)
def test_is_game_win_result_negative(result: str) -> None:
    assert cd.is_game_win_result(result) is False


# --------------------------------------------------------------------------- #
# win_combos_in_deck — the rule-1 drive gate                                  #
# --------------------------------------------------------------------------- #


def _thoracle_win() -> Combo:
    return Combo(
        variant_id='win-1',
        card_names=("Thassa's Oracle", 'Demonic Consultation'),
        card_oracle_ids=('', ''),
        result='Each opponent loses the game',
    )


def _mana_only() -> Combo:
    return Combo(
        variant_id='mana-1',
        card_names=('Basalt Monolith', 'Rings of Brighthearth'),
        card_oracle_ids=('', ''),
        result='Infinite colorless mana',
    )


def test_win_combos_in_deck_returns_only_game_wins() -> None:
    combos = [_thoracle_win(), _mana_only()]
    identity = {
        "thassa's oracle",
        'demonic consultation',
        'basalt monolith',
        'rings of brighthearth',
    }
    won = cd.win_combos_in_deck(identity, combos)
    assert [c.variant_id for c in won] == ['win-1']


def test_win_combos_in_deck_empty_when_only_mana_combo() -> None:
    combos = [_mana_only()]
    identity = {'basalt monolith', 'rings of brighthearth'}
    assert cd.win_combos_in_deck(identity, combos) == []


# --------------------------------------------------------------------------- #
# EP1: infinite-mana + in-deck lethal sink promotion (deck-aware)             #
# --------------------------------------------------------------------------- #


def test_is_infinite_mana_result() -> None:
    assert cd.is_infinite_mana_result('Infinite colorless mana') is True
    assert cd.is_infinite_mana_result('Infinite green mana; Infinite untap') is True
    assert cd.is_infinite_mana_result('Infinite mana Myr you control can produce') is True
    assert cd.is_infinite_mana_result('Infinite card draw') is False
    assert cd.is_infinite_mana_result('Win the game') is False


def test_mana_only_combo_promoted_when_deck_has_sink() -> None:
    combos = [_mana_only()]
    identity = {'basalt monolith', 'rings of brighthearth', 'exsanguinate'}
    won = cd.win_combos_in_deck(identity, combos)
    assert [c.variant_id for c in won] == ['mana-1']


def test_mana_only_combo_not_promoted_without_sink_can_be_disabled() -> None:
    combos = [_mana_only()]
    identity = {'basalt monolith', 'rings of brighthearth', 'exsanguinate'}
    # allow_mana_sink=False keeps the pure result-only behavior
    assert cd.win_combos_in_deck(identity, combos, allow_mana_sink=False) == []


def test_deck_mana_sinks_detects_known_finishers() -> None:
    identity = {'exsanguinate', 'sol ring', 'forest', 'walking ballista'}
    sinks = cd.deck_mana_sinks(identity)
    assert set(sinks) == {'Exsanguinate', 'Walking Ballista'}


def test_analyze_flags_mana_combo_without_sink() -> None:
    combos = [_thoracle_win(), _mana_only()]
    identity = {
        "thassa's oracle",
        'demonic consultation',
        'basalt monolith',
        'rings of brighthearth',
    }
    analysis = cd.analyze_deck_win_combos(identity, combos)
    assert [c.variant_id for c in analysis.wins] == ['win-1']
    assert [c.variant_id for c in analysis.predicate_wins] == ['win-1']
    assert analysis.mana_sink_wins == []
    assert [c.variant_id for c in analysis.flagged_mana_combos] == ['mana-1']
    assert analysis.mana_sinks_present == []


def test_analyze_promotes_mana_combo_with_sink() -> None:
    combos = [_mana_only()]
    identity = {'basalt monolith', 'rings of brighthearth', 'walking ballista'}
    analysis = cd.analyze_deck_win_combos(identity, combos)
    assert [c.variant_id for c in analysis.mana_sink_wins] == ['mana-1']
    assert [c.variant_id for c in analysis.wins] == ['mana-1']
    assert analysis.flagged_mana_combos == []
    assert analysis.mana_sinks_present == ['Walking Ballista']


# --------------------------------------------------------------------------- #
# ensure_combo_lake — the missing-parquet guard                              #
# --------------------------------------------------------------------------- #


def test_ensure_combo_lake_noop_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the parquet exists the guard does NOT rebuild (no sync/build calls)."""
    called: list[str] = []
    monkeypatch.setattr(cd, '_combo_parquet_path', lambda: Path(__file__))  # a file that exists
    monkeypatch.setattr(cd, 'build', lambda: called.append('build'))
    import pipeline.sources.spellbook as sb

    monkeypatch.setattr(sb, 'sync', lambda **_: called.append('sync'))
    cd.ensure_combo_lake()
    assert called == []


def test_ensure_combo_lake_rebuilds_when_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When the parquet is MISSING the guard runs spellbook.sync() then build()."""
    called: list[str] = []
    missing = tmp_path / 'normalized' / 'combo.parquet'
    monkeypatch.setattr(cd, '_combo_parquet_path', lambda: missing)
    monkeypatch.setattr(cd, 'build', lambda: called.append('build') or missing)
    import pipeline.sources.spellbook as sb

    monkeypatch.setattr(sb, 'sync', lambda **_: called.append('sync') or tmp_path)
    cd.ensure_combo_lake()
    assert called == ['sync', 'build']
