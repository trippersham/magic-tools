"""Terminal-cause validity gate — the classifier that replaces the ms<2000 wall-clock proxy.

Time is triage, not correctness (the three-model bailout synthesis): all 76 sub-2s games in the
endurance corpus had assignable terminal causes — real fast kills + unpaid-Pact rule losses that
the ms floor wrongly excluded, and synthetic macro wins that a legal cause would reject. The
classifier buckets a :class:`GameResult` by its ``end_cause`` (emitted by the worker), falling back
to the OLD ms-floor heuristic ONLY for legacy rows that predate the ``end_cause`` jar.
"""

from __future__ import annotations

import pytest

from pipeline.sim.aggregate import (
    BAILOUT_HARD_FLOOR_MS,
    Validity,
    classify_validity,
    is_concede,
    is_fast_game,
)
from pipeline.sim.game_protocol import GameResult, parse_line


def _r(
    *,
    winner: str = 'A',
    ms: int = 60000,
    end_cause: str | None = None,
    reason: str | None = None,
    markers: list[str] | None = None,
) -> GameResult:
    return GameResult(
        task_id='S|O|driven|0',
        winner=winner,
        kill_turn=8,
        ms=ms,
        markers=markers or [],
        log_path=None,
        reason=reason,
        end_cause=end_cause,
    )


# --------------------------------------------------------------------------- #
# The cause → bucket table.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    'cause',
    ['lethal_damage', 'commander_damage', 'draw_empty_library', 'rule_loss', 'state_loss', 'poison'],
)
def test_decisive_causes_count(cause: str) -> None:
    # A real decided outcome is DECISIVE regardless of wall-clock — even a 22ms lethal kill.
    assert classify_validity(_r(end_cause=cause, ms=746)) is Validity.DECISIVE


def test_draw_game_is_decisive_fill() -> None:
    # An actual tie is a valid cell fill (no top-up); the winner='DRAW' credits neither seat.
    assert classify_validity(_r(winner='DRAW', end_cause='draw_game')) is Validity.DECISIVE


def test_timeout_cause_nondecisive() -> None:
    assert classify_validity(_r(winner='none', end_cause='timeout', reason='timeout')) is Validity.NONDECISIVE


def test_driver_rejected_reason_nondecisive() -> None:
    # driver-rejected returns before a game runs → no end_cause, but reason drives exclusion.
    assert classify_validity(_r(winner='none', reason='driver-rejected', ms=0)) is Validity.NONDECISIVE


def test_concede_is_decisive() -> None:
    # A concession is a rules-legal loss (CR 104.3a) — a decided outcome, NOT invalid data. XMage's
    # CP7 AI concedes hopeless positions on ordinary games, so it must fill its cell as decisive.
    assert classify_validity(_r(winner='A', end_cause='concede')) is Validity.DECISIVE


def test_concede_flag() -> None:
    # is_concede surfaces concessions in coverage (a data-quality signal), independent of decisiveness.
    assert is_concede(_r(winner='A', end_cause='concede')) is True
    assert is_concede(_r(winner='A', end_cause='CONCEDE')) is True
    assert is_concede(_r(winner='A', end_cause='lethal_damage')) is False
    assert is_concede(_r(winner='A', end_cause=None)) is False


# --------------------------------------------------------------------------- #
# Cross-field (cause <-> winner <-> reason) consistency — a mismatch is INVALID,
# never silently counted. Applies to the NEW end_cause path (legacy rows keep the ms-floor).
# --------------------------------------------------------------------------- #


def test_decisive_cause_without_credited_winner_is_invalid() -> None:
    # end_cause=lethal_damage but winner=none → a silent draw the classifier would have credited to
    # neither seat; a decisive cause REQUIRES a credited winner → INVALID.
    assert classify_validity(_r(winner='none', end_cause='lethal_damage')) is Validity.INVALID


def test_draw_game_with_credited_winner_is_invalid() -> None:
    # end_cause=draw_game but winner=A → a draw becoming a win → INVALID.
    assert classify_validity(_r(winner='A', end_cause='draw_game')) is Validity.INVALID


def test_nondecisive_reason_with_credited_winner_is_invalid() -> None:
    # A non-decisive reason (timeout) must credit no winner; winner=A with reason=timeout → INVALID.
    assert classify_validity(_r(winner='A', end_cause='timeout', reason='timeout')) is Validity.INVALID


def test_timeout_cause_with_credited_winner_is_invalid() -> None:
    # end_cause=timeout (non-decisive) paired with a credited winner → INVALID.
    assert classify_validity(_r(winner='B', end_cause='timeout')) is Validity.INVALID


def test_concede_without_credited_winner_is_invalid() -> None:
    # concede is decisive; the conceder is the loser, so a credited winner is required. winner=none
    # with end_cause=concede is inconsistent → INVALID.
    assert classify_validity(_r(winner='none', end_cause='concede')) is Validity.INVALID


def test_unknown_cause_on_decisive_claim_is_invalid() -> None:
    assert classify_validity(_r(winner='A', end_cause='unknown')) is Validity.INVALID


def test_macro_game_over_without_legal_cause_is_invalid() -> None:
    assert classify_validity(_r(winner='A', end_cause='macro_game_over')) is Validity.INVALID


# --------------------------------------------------------------------------- #
# Backward compat — a legacy row (no end_cause) falls back to the ms-floor heuristic.
# --------------------------------------------------------------------------- #


def test_legacy_row_fast_falls_back_to_ms_floor() -> None:
    # No end_cause + below the floor → the old bailout heuristic still excludes it.
    assert classify_validity(_r(end_cause=None, ms=800), hard_floor_ms=BAILOUT_HARD_FLOOR_MS) is Validity.NONDECISIVE


def test_legacy_row_slow_is_decisive() -> None:
    assert classify_validity(_r(end_cause=None, ms=60000), hard_floor_ms=BAILOUT_HARD_FLOOR_MS) is Validity.DECISIVE


def test_legacy_row_floor_disarmed_is_decisive() -> None:
    # floor=0 (the delta-replay path) keeps every legacy game — the old disarmed behavior.
    assert classify_validity(_r(end_cause=None, ms=50), hard_floor_ms=0) is Validity.DECISIVE


def test_legacy_marker_bailout_still_excluded() -> None:
    r = _r(end_cause=None, ms=60000, markers=['concede'])
    assert classify_validity(r, hard_floor_ms=BAILOUT_HARD_FLOOR_MS) is Validity.NONDECISIVE


def test_new_fast_decisive_kill_comes_back() -> None:
    # The headline fix: a 746ms lethal kill WITH a cause is decisive even armed at the 2s floor.
    r = _r(winner='B', end_cause='lethal_damage', ms=746)
    assert classify_validity(r, hard_floor_ms=BAILOUT_HARD_FLOOR_MS) is Validity.DECISIVE


# --------------------------------------------------------------------------- #
# fast_game triage flag — informational only.
# --------------------------------------------------------------------------- #


def test_fast_game_flag() -> None:
    assert is_fast_game(_r(ms=1500)) is True
    assert is_fast_game(_r(ms=2000)) is False
    assert is_fast_game(_r(ms=60000)) is False


# --------------------------------------------------------------------------- #
# Wire-through — end_cause parses off the RESULT markers.
# --------------------------------------------------------------------------- #


def test_end_cause_parsed_off_markers() -> None:
    line = (
        'RESULT {"id": "S|O|driven|0", "winner": "A", "ms": 746, "markers": ["seatA=cp7", "end_cause=lethal_damage"]}'
    )
    msg = parse_line(line)
    assert isinstance(msg, GameResult)
    assert msg.end_cause == 'lethal_damage'


def test_end_cause_default_none_when_absent() -> None:
    line = 'RESULT {"id": "S|O|driven|0", "winner": "A", "ms": 60000, "markers": ["seatA=cp7"]}'
    msg = parse_line(line)
    assert isinstance(msg, GameResult)
    assert msg.end_cause is None
