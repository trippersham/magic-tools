"""M1 regression: fresh-path pooling excludes clockout games (fresh == cached).

The cache stores per-game logs as ``split_games(raw_log)`` with clockout segments
excluded (``store.py``), but the FRESH (``--force``) path used to pool the whole
``raw_log`` verbatim — including clockout games, each of which still emits a
``gameend`` HANDLOG marker → an extra, fabricated game in the piloting
denominator. So a ``--force`` run and a cached rerun of the same matchup reported
DIFFERENT piloting numbers. The fix applies the identical decided-games filter on
the fresh path.

Pure parse of synthetic logs (no JVM, no store).
"""

from __future__ import annotations

from pipeline.sim.core import MatchOutcome, _decided_game_segments, _pool_candidate_logs
from pipeline.sim.telemetry import extract_piloting

_REMOVAL = {'Doom Blade'}
_KW = {
    'counters': set(),
    'removal': _REMOVAL,
    'interaction': set(_REMOVAL),
    'costs': {'Doom Blade': 2},
    'lands': {'Swamp'},
}

# Game 1 — DECIDED: candidate (Ai(1)) holds an affordable Doom Blade on its own
# turn with a target, and casts it (1 opportunity, 1 conversion).
_DECIDED = (
    'Turn: Turn 1 (Ai(1)-Test)\n'
    'HANDLOG turn=1 event=turnstart active=Ai(1)-Test '
    'UR_hand=[Doom Blade;Swamp] UR_untapped_lands=0 UR_total_lands=3 '
    'opp_creatures=1 UR_life=20 opp_life=20\n'
    'HANDLOG turn=1 event=cast kind=spell castBy=Ai(1)-Test source=Doom Blade '
    'UR_hand=[Swamp] UR_untapped_lands=1 UR_total_lands=3 '
    'opp_creatures=1 UR_life=20 opp_life=20\n'
    'HANDLOG turn=1 event=gameend '
    'UR_hand=[Swamp] UR_untapped_lands=1 UR_total_lands=3 '
    'opp_creatures=0 UR_life=20 opp_life=20\n'
    '\nGame Result: Game 1 ended in 5000 ms. Ai(1)-Test has won!\n'
)

# Game 2 — CLOCKOUT: the `Stopping slow match as draw` marker + a fabricated
# dual-win terminator. It ALSO holds an affordable Doom Blade on the candidate's
# turn (another opportunity, never cast) — the pollution the filter must drop.
_CLOCKOUT = (
    'Stopping slow match as draw\n'
    'HANDLOG turn=9 event=turnstart active=Ai(1)-Test '
    'UR_hand=[Doom Blade;Swamp] UR_untapped_lands=0 UR_total_lands=4 '
    'opp_creatures=1 UR_life=20 opp_life=20\n'
    'HANDLOG turn=9 event=gameend '
    'UR_hand=[Doom Blade;Swamp] UR_untapped_lands=0 UR_total_lands=4 '
    'opp_creatures=1 UR_life=20 opp_life=20\n'
    'Game Outcome: Ai(1)-Test has won because all opponents have lost\n'
    'Game Outcome: Ai(2)-Foe has won because all opponents have lost\n'
    '\nGame Result: Game 2 ended in 90000 ms. Ai(1)-Test has won!\n'
)

_RAW = _DECIDED + _CLOCKOUT


def test_unfiltered_raw_would_double_count_the_clockout() -> None:
    # Baseline showing the bug: parsing the raw log directly counts BOTH games,
    # so the clockout inflates the opportunity denominator (2 opps, 1 cast).
    p = extract_piloting(_RAW, **_KW)
    assert p.games == 2
    assert p.removal_opps == 2
    assert p.removal_casts == 1


def test_fresh_path_excludes_clockout() -> None:
    # The fix: pooling the fresh raw_log drops the clockout game -> 1 decided
    # game, 1 opportunity, 1 conversion.
    pooled = _pool_candidate_logs(
        [MatchOutcome(opponent='Foe', wins=1, losses=0, draws=0, cached=False, features=[], raw_log=_RAW)],
        data_dir=None,
    )
    p = extract_piloting(pooled, **_KW)
    assert p.games == 1
    assert p.removal_opps == 1
    assert p.removal_casts == 1


def test_fresh_pool_equals_cached_decided_segments() -> None:
    # The store persists exactly `_decided_game_segments(raw_log)` for the cache,
    # so a cached rerun pools those. The fresh path must produce the SAME games.
    fresh = _pool_candidate_logs(
        [MatchOutcome(opponent='Foe', wins=1, losses=0, draws=0, cached=False, features=[], raw_log=_RAW)],
        data_dir=None,
    )
    cached_like = '\n'.join(_decided_game_segments(_RAW))  # what get_game_logs returns
    assert extract_piloting(fresh, **_KW) == extract_piloting(cached_like, **_KW)


def test_decided_segments_drops_only_the_clockout() -> None:
    segs = _decided_game_segments(_RAW)
    assert len(segs) == 1
    assert 'Game 1 ended' in segs[0]
    assert 'Stopping slow match' not in segs[0]
