"""Tests for the piloting-profile wiring in :func:`pipeline.sim.core.simulate`.

Part B of task 1.6b: when the engine has hand visibility, ``simulate`` pools the
candidate's per-game logs across all matchups (fresh -> ``MatchResult.raw_log``,
cached -> ``store.get_game_logs``), builds the deck classification from deck_a's
names, and attaches a :class:`~pipeline.sim.telemetry.PilotingProfile` to
``SimResult.piloting``. An engine WITHOUT hand visibility -> ``piloting is None``
(never fabricated). An empty-lake classification -> ``available=False`` (the
honest unavailable marker, not a 0/0).

A FakeEngine returns a canned ``raw_log`` = the captured HANDLOG fixture, and the
deck classification is MONKEYPATCHED so no live otag lake is touched — the wiring
is what's under test, the parser itself is covered by ``test_telemetry_piloting``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import store
from pipeline.sim import core
from pipeline.sim.classify import Classification
from pipeline.sim.engine import EngineCapabilities, EngineInstall
from pipeline.sim.governor import Governor
from pipeline.sim.runner import GameOutcome, MatchResult

FIXTURES = Path(__file__).parent / 'fixtures' / 'forge'


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


def _handlog() -> str:
    return (FIXTURES / 'handlog_azorius.log').read_text(errors='replace')


# The UR classification the fixture pilots (mirrors test_telemetry_piloting): the
# sole true counter is Reasonable Doubt (fires 0/7 -> the sim-AI signature).
_UR_CLASSIFICATION = Classification(
    counters=frozenset({'Reasonable Doubt'}),
    removal=frozenset({'Burst Lightning', 'Sear', 'Bombard', 'Unsubtle Mockery', 'Synchronized Spellcraft'}),
    interaction=frozenset(
        {'Reasonable Doubt', 'Burst Lightning', 'Sear', 'Bombard', 'Unsubtle Mockery', 'Synchronized Spellcraft'}
    ),
    costs={
        'Reasonable Doubt': 2,
        'Burst Lightning': 1,
        'Sear': 2,
        'Bombard': 3,
        'Unsubtle Mockery': 3,
        'Synchronized Spellcraft': 5,
    },
    lands=frozenset({'Island', 'Mountain', 'Spectacle Summit'}),
    available=True,
)

_UNAVAILABLE_CLASSIFICATION = Classification(
    counters=frozenset(),
    removal=frozenset(),
    interaction=frozenset(),
    costs={},
    lands=frozenset(),
    available=False,
    reason='otag lake not populated — run the otag build to enable piloting metrics',
)


def _caps(*, hand: bool) -> EngineCapabilities:
    return EngineCapabilities(
        has_hand_visibility=hand,
        has_counter_metrics=hand,
        expected_nondecisive_rate=0.05,
        reliability_note='fake',
        kill_attribution='named',
    )


class _HandlogEngine:
    """A no-JVM engine whose ``run_matchup`` returns the captured HANDLOG fixture."""

    def __init__(self, *, hand_visibility: bool) -> None:
        self._caps = _caps(hand=hand_visibility)

    name = 'fake-handlog'

    def capabilities(self) -> EngineCapabilities:
        return self._caps

    def resolve(self, *, provision: bool, data_dir: object = None) -> EngineInstall:
        del provision, data_dir
        return EngineInstall(version='test-forge', handle=object())

    def run_matchup(
        self,
        deck_a: tuple[str, str],
        deck_b: tuple[str, str],
        *,
        n: int,
        seed: int,
        fmt: str,
        install: EngineInstall,
        timeout_s: int | None = None,
    ) -> MatchResult:
        del seed, fmt, install, timeout_s
        # The fixture already holds 3 decisive games; the tally is illustrative.
        per_game = tuple(GameOutcome(winner='a', elapsed_ms=1000) for _ in range(n))
        return MatchResult(
            deck_a=deck_a[0],
            deck_b=deck_b[0],
            wins_a=n,
            wins_b=0,
            draws=0,
            per_game=per_game,
            raw_log=_handlog(),
        )

    def replay(self, matchup_key: str, game_idx: int) -> str:
        return f'fake replay {matchup_key}#{game_idx}'


def _patch(monkeypatch: pytest.MonkeyPatch, classification: Classification) -> None:
    from pipeline.sim.gauntlet import GauntletDeck

    monkeypatch.setattr(core, 'resolve_gauntlet', lambda source, fmt, **kw: [GauntletDeck(name='Opp1', dck_text='X')])
    monkeypatch.setattr(Governor, 'stagger_s', 0.0)
    # The classification is what deck_a would resolve to — patch it so no lake is hit.
    monkeypatch.setattr(core, 'classify_deck', lambda names, resolver=None: classification)


def test_piloting_populated_with_hand_visibility(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    _patch(monkeypatch, _UR_CLASSIFICATION)
    result = core.simulate(
        ('Cand', 'Name Cand\n[Main]\n40 Island\n'),
        'curated',
        games=3,
        fmt='constructed',
        seed=1,
        engine=_HandlogEngine(hand_visibility=True),
        data_dir=str(data_dir),
        pool_size=1,
    )
    assert result.piloting is not None
    assert result.piloting.available is True
    assert result.piloting.games == 3
    # The sim-AI signature: holds an affordable counter across 7 opp casts, fires 0.
    assert result.piloting.counter_opps == 7
    assert result.piloting.counter_casts == 0
    assert result.piloting.counter_fire == 0.0


def test_no_hand_visibility_gives_none(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    _patch(monkeypatch, _UR_CLASSIFICATION)
    result = core.simulate(
        ('Cand', 'Name Cand\n[Main]\n40 Island\n'),
        'curated',
        games=3,
        fmt='constructed',
        seed=1,
        engine=_HandlogEngine(hand_visibility=False),
        data_dir=str(data_dir),
        pool_size=1,
    )
    assert result.piloting is None


def test_empty_lake_marks_unavailable(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    _patch(monkeypatch, _UNAVAILABLE_CLASSIFICATION)
    result = core.simulate(
        ('Cand', 'Name Cand\n[Main]\n40 Island\n'),
        'curated',
        games=3,
        fmt='constructed',
        seed=1,
        engine=_HandlogEngine(hand_visibility=True),
        data_dir=str(data_dir),
        pool_size=1,
    )
    assert result.piloting is not None
    assert result.piloting.available is False
    assert result.piloting.reason is not None
    assert 'otag lake' in result.piloting.reason


def test_pools_cached_logs_on_rerun(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    # Second identical run serves from cache (0 fresh matchups) but the piloting
    # profile must STILL be built — pooled from store.get_game_logs, not raw_log.
    _patch(monkeypatch, _UR_CLASSIFICATION)
    kwargs = dict(  # noqa: C408
        games=3,
        fmt='constructed',
        seed=1,
        engine=_HandlogEngine(hand_visibility=True),
        data_dir=str(data_dir),
        pool_size=1,
    )
    first = core.simulate(('Cand', 'Name Cand\n[Main]\n40 Island\n'), 'curated', **kwargs)  # type: ignore[arg-type]
    assert first.fresh_matchups == 1
    second = core.simulate(('Cand', 'Name Cand\n[Main]\n40 Island\n'), 'curated', **kwargs)  # type: ignore[arg-type]
    assert second.cached_matchups == 1 and second.fresh_matchups == 0
    assert second.piloting is not None
    assert second.piloting.available is True
    # Cached-log pooling reproduces the same counter opportunities.
    assert second.piloting.counter_opps == 7
    assert second.piloting.counter_casts == 0
