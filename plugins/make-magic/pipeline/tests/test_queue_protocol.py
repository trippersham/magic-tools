"""Phase 1.2 — wire protocol codec (pure, no JVM).

The JSON shape is Gson-compatible (short, stable keys: id/fmt/a/b/deck/driver/cp/fqcn)
so the Phase-2 Java record round-trips against it. Worker->governor lines parse into
small typed messages; a malformed line raises, never silently returns ``None``.
"""

from __future__ import annotations

import json

import pytest

from pipeline.sim.game_protocol import (
    GameError,
    GameResult,
    Heartbeat,
    ProtocolError,
    Ready,
    encode_task,
    parse_line,
)
from pipeline.sim.game_tasks import DriverRef, GameTask, SeatSpec


def _task(*, b_driven: bool = True) -> GameTask:
    return GameTask(
        task_id='s1|o1|driven|0',
        fmt='commander',
        seat_a=SeatSpec(deck_path='/decks/s1.dck', driver=DriverRef(classpath='/cp/s1', fqcn='com.x.S1')),
        seat_b=SeatSpec(
            deck_path='/decks/o1.dck',
            driver=DriverRef(classpath='/cp/o1', fqcn='com.x.O1') if b_driven else None,
        ),
    )


def test_encode_task_exact_json_shape() -> None:
    line = encode_task(_task())
    assert line.startswith('TASK ')
    payload = json.loads(line[len('TASK ') :])
    assert payload == {
        'id': 's1|o1|driven|0',
        'fmt': 'commander',
        'a': {'deck': '/decks/s1.dck', 'driver': {'cp': '/cp/s1', 'fqcn': 'com.x.S1'}},
        'b': {'deck': '/decks/o1.dck', 'driver': {'cp': '/cp/o1', 'fqcn': 'com.x.O1'}},
    }


def test_encode_task_null_driver_round_trips() -> None:
    line = encode_task(_task(b_driven=False))
    payload = json.loads(line[len('TASK ') :])
    assert payload['b']['driver'] is None
    # The nested-seat shape survives a parse back into the task fields.
    assert payload['a']['driver'] == {'cp': '/cp/s1', 'fqcn': 'com.x.S1'}


def test_parse_ready() -> None:
    assert isinstance(parse_line('READY'), Ready)
    assert isinstance(parse_line('READY\n'), Ready)


def test_parse_result() -> None:
    payload = {
        'id': 's1|o1|driven|0',
        'winner': 'a',
        'kill_turn': 7,
        'ms': 6900,
        'markers': ['DRIVER_FIRE'],
        'log': '/staging/logs/s1|o1|driven|0.log',
    }
    msg = parse_line('RESULT ' + json.dumps(payload))
    assert isinstance(msg, GameResult)
    assert msg.task_id == 's1|o1|driven|0'
    assert msg.winner == 'a'
    assert msg.kill_turn == 7
    assert msg.ms == 6900
    assert msg.markers == ['DRIVER_FIRE']
    assert msg.log_path == '/staging/logs/s1|o1|driven|0.log'


def test_parse_heartbeat() -> None:
    msg = parse_line('GAME s1|o1|driven|0 turn=5 ms=1234')
    assert isinstance(msg, Heartbeat)
    assert msg.task_id == 's1|o1|driven|0'
    assert msg.turn == 5
    assert msg.ms == 1234


def test_parse_error() -> None:
    msg = parse_line('ERROR ' + json.dumps({'id': 's1|o1|driven|0', 'exc': 'NPE at foo'}))
    assert isinstance(msg, GameError)
    assert msg.task_id == 's1|o1|driven|0'
    assert msg.exc == 'NPE at foo'


@pytest.mark.parametrize(
    'line',
    [
        '',
        'GIBBERISH',
        'RESULT not-json',
        'GAME s1 turn=x ms=1',  # non-int turn
        'RESULT {}',  # missing id
    ],
)
def test_malformed_line_raises(line: str) -> None:
    with pytest.raises(ProtocolError):
        parse_line(line)


# --- Sol HIGH 1: strict typed fields --------------------------------------- #


def test_result_missing_ms_raises() -> None:
    """``ms`` is REQUIRED and typed — a missing ms is no longer defaulted to 0."""
    with pytest.raises(ProtocolError, match='ms'):
        parse_line('RESULT {"id": "s|o|driven|0", "winner": "a"}')


@pytest.mark.parametrize('ms', ['"soon"', '-1', '1.5', 'true', 'null'])
def test_result_non_int_or_negative_ms_raises(ms: str) -> None:
    with pytest.raises(ProtocolError, match='ms'):
        parse_line(f'RESULT {{"id": "s|o|driven|0", "winner": "a", "ms": {ms}}}')


def test_heartbeat_negative_ms_raises() -> None:
    with pytest.raises(ProtocolError, match='ms'):
        parse_line('GAME s1|o1|driven|0 turn=5 ms=-3')


@pytest.mark.parametrize('reason', ['bailout', 'timeout', 'driver-rejected'])
def test_result_known_reason_accepted(reason: str) -> None:
    msg = parse_line(f'RESULT {{"id": "s|o|driven|0", "winner": "none", "ms": 60000, "reason": "{reason}"}}')
    assert isinstance(msg, GameResult)
    assert msg.reason == reason


def test_result_unknown_reason_raises() -> None:
    with pytest.raises(ProtocolError, match='reason'):
        parse_line('RESULT {"id": "s|o|driven|0", "winner": "none", "ms": 60000, "reason": "made-up"}')


def test_result_string_markers_is_rejected_not_iterated_as_chars() -> None:
    """A bare-string ``markers`` value must ERROR — never silently become a per-character list."""
    with pytest.raises(ProtocolError, match='markers'):
        parse_line('RESULT {"id": "s|o|driven|0", "winner": "a", "ms": 60000, "markers": "abc"}')


def test_result_list_markers_ok() -> None:
    msg = parse_line('RESULT {"id": "s|o|driven|0", "winner": "a", "ms": 60000, "markers": ["X", "Y"]}')
    assert isinstance(msg, GameResult)
    assert msg.markers == ['X', 'Y']
