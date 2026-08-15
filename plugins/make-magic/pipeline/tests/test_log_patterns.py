"""R3-3: the game-terminator regex triplet is ONE shared definition.

``runner`` and ``telemetry`` must import the SAME compiled objects from
``pipeline.sim._log_patterns`` so tally and telemetry segmentation can never
silently desync (a future edit to one no longer diverges from the other).
"""

from __future__ import annotations

from pipeline.sim import _log_patterns, runner, telemetry


def test_runner_and_telemetry_share_the_same_pattern_objects() -> None:
    # Identity, not just equality: both modules bind the SAME compiled object.
    assert runner._RESULT_RE is _log_patterns.RESULT_RE
    assert runner._DRAW_RESULT_RE is _log_patterns.DRAW_RESULT_RE
    assert runner._WINNER_RE is _log_patterns.WINNER_RE
    assert telemetry._RESULT_RE is _log_patterns.RESULT_RE
    assert telemetry._DRAW_RESULT_RE is _log_patterns.DRAW_RESULT_RE
    assert telemetry._WINNER_RE is _log_patterns.WINNER_RE
    # And therefore identical across the two consumers.
    assert runner._RESULT_RE is telemetry._RESULT_RE
    assert runner._DRAW_RESULT_RE is telemetry._DRAW_RESULT_RE
    assert runner._WINNER_RE is telemetry._WINNER_RE


def test_shared_patterns_still_match_the_real_grammar() -> None:
    result = 'Game Result: Game 1 ended in 5321 ms. Ai(2)-U/R Izzet (Chaos) has won!'
    m = _log_patterns.RESULT_RE.match(result)
    assert m is not None and m.group(1) == '5321'
    w = _log_patterns.WINNER_RE.search(m.group(2))
    assert w is not None and w.group(1) == '2'  # slot -> deck_b

    draw = 'Game Result: Game 3 ended in a Draw! Took 90000 ms.'
    assert _log_patterns.RESULT_RE.match(draw) is None  # draw wording is NOT the normal form
    dm = _log_patterns.DRAW_RESULT_RE.match(draw)
    assert dm is not None and dm.group(1) == '90000'
