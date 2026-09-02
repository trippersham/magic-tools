"""Phase 1.2 — the newline-delimited wire protocol codec (pure, no JVM).

Governor→worker is a single line: ``TASK {json}``. The JSON keeps short, stable keys
(``id``, ``fmt``, ``a``, ``b``, ``deck``, ``driver``, ``cp``, ``fqcn``) with nested seat
objects and a nullable nested driver, so a Gson record on the Java side round-trips
against it (design §4).

Worker→governor lines parse into small typed messages:

* ``READY``                          → :class:`Ready` (idle, wants a task — backpressure)
* ``GAME <id> turn=N ms=…``          → :class:`Heartbeat` (stall-watchdog signal)
* ``RESULT {json}``                  → :class:`GameResult`
* ``ERROR {json}``                   → :class:`GameError`

A line that does not parse raises :class:`ProtocolError` — never a silent ``None``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pipeline.sim.game_tasks import GameTask, SeatSpec

__all__ = (
    'GameError',
    'GameResult',
    'Heartbeat',
    'ProtocolError',
    'ProtocolMsg',
    'Ready',
    'encode_task',
    'parse_line',
)


class ProtocolError(ValueError):
    """A worker→governor line (or a field within it) could not be parsed."""


#: The winner tokens the aggregation understands (case-insensitive), mirroring
#: :func:`~pipeline.sim.game_queue._winner_bucket`. ``'none'`` is the non-decisive sentinel a
#: ``reason``-set game reports. Any OTHER value is a VALIDATION FAILURE — never a silent draw.
_VALID_WINNERS = frozenset(
    {'a', 'subject', 'player_a', 'playera', 'b', 'opponent', 'player_b', 'playerb', 'draw', 'none'}
)


#: The non-decisive terminal ``reason`` tokens the pipeline understands. ``'timeout'`` /
#: ``'driver-rejected'`` are emitted by the Java worker; ``'bailout'`` is the sub-floor /
#: deadline stand-in. ``None`` (absent) is an ordinary decided game. Any OTHER token is a
#: VALIDATION FAILURE — a codec drift must never smuggle an unrecognised terminal reason
#: (which the aggregator would treat as non-decisive) past the wire.
_VALID_REASONS = frozenset({'timeout', 'driver-rejected', 'bailout'})


def _require_ms(raw: object, line: str) -> int:
    """Return the typed non-negative ``ms``, or raise :class:`ProtocolError`.

    ``ms`` is REQUIRED and must be a non-negative integer — the old lenient
    ``int(body.get('ms', 0))`` silently invented a duration for a malformed/missing field,
    which the plausibility floor then read as a real (implausibly short) game."""
    if raw is None:
        msg = f'missing ms in {line!r}'
        raise ProtocolError(msg)
    # bool is an int subclass — reject it explicitly; only real ints are a duration.
    if isinstance(raw, bool) or not isinstance(raw, int):
        msg = f'non-integer ms {raw!r} in {line!r}'
        raise ProtocolError(msg)
    if raw < 0:
        msg = f'negative ms {raw!r} in {line!r}'
        raise ProtocolError(msg)
    return raw


def _validate_reason(raw: object, line: str) -> str | None:
    """Return the ``reason`` string (or ``None`` when absent), or raise for an unknown token."""
    if raw is None:
        return None
    reason = str(raw)
    if reason not in _VALID_REASONS:
        msg = f'unknown reason {reason!r} in {line!r} (expected one of {sorted(_VALID_REASONS)})'
        raise ProtocolError(msg)
    return reason


def _validate_markers(raw: object, line: str) -> list[str]:
    """Return the ``markers`` list, or raise for a non-list value.

    A bare STRING markers value is the pathology this guards: ``list("abc")`` would silently
    iterate it into ``['a', 'b', 'c']`` and corrupt ``end_cause=`` extraction. Only an actual
    JSON array is accepted; an absent markers is an empty list."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        msg = f'markers must be a JSON array, got {type(raw).__name__} in {line!r}'
        raise ProtocolError(msg)
    return [str(m) for m in raw]


def _validate_winner(raw: object, line: str) -> str:
    """Return the ``winner`` string, or raise :class:`ProtocolError` for a missing/unknown token.

    Replaces the old silent ``str(body.get('winner', ''))`` default (which folded every
    malformed/unknown winner into a draw): an unrecognised outcome is a hard parse failure so a
    codec drift can never masquerade as a no-credit draw."""
    if raw is None:
        msg = f'missing winner in {line!r}'
        raise ProtocolError(msg)
    winner = str(raw)
    if winner.strip().lower() not in _VALID_WINNERS:
        msg = f'unknown winner {winner!r} in {line!r} (expected one of A/B/DRAW/none)'
        raise ProtocolError(msg)
    return winner


@dataclass(frozen=True)
class Ready:
    """The worker is idle and pulling for the next task (backpressure signal)."""


@dataclass(frozen=True)
class Heartbeat:
    """Coarse per-game progress: resets the worker's stall clock."""

    task_id: str
    turn: int
    ms: int


@dataclass(frozen=True)
class GameResult:
    """One completed game, keyed by ``task_id`` (the dedup key for the governor)."""

    task_id: str
    winner: str
    kill_turn: int | None
    ms: int
    markers: list[str]
    log_path: str | None
    #: Non-decisive terminal reason, or ``None`` for an ordinary decided/undecided game.
    #: ``'timeout'`` marks an engine-defective wall-clock livelock (base CP7 AI re-activating an
    #: optional mana ability — a loop the Comprehensive Rules forbid, CR 104.4b / 720). Such a game
    #: is TERMINAL (dedup / cell-done / no requeue) but EXCLUDED from W/L/D stats: its ``winner`` is
    #: ``'none'`` so :func:`~pipeline.sim.game_queue._winner_bucket` credits neither seat.
    reason: str | None = None
    #: The TERMINAL CAUSE the worker derived from the engine end-state (``lethal_damage`` /
    #: ``commander_damage`` / ``draw_empty_library`` / ``poison`` / ``state_loss`` / ``draw_game`` /
    #: ``timeout`` / ``unknown``). The correctness predicate :func:`~pipeline.sim.aggregate.
    #: classify_validity` buckets on this instead of wall-clock; ``None`` marks a LEGACY row that
    #: predates the terminal-cause jar (classifier falls back to the old ms-floor heuristic).
    end_cause: str | None = None

    @property
    def decisive(self) -> bool:
        """False for a non-decisive terminal game (``reason`` set) — filter these from aggregation."""
        return self.reason is None


@dataclass(frozen=True)
class GameError:
    """A game threw inside the worker; ``exc`` is the captured description."""

    task_id: str
    exc: str


#: Any worker→governor message.
ProtocolMsg = Ready | Heartbeat | GameResult | GameError


def _seat_json(seat: SeatSpec) -> dict[str, Any]:
    driver = None if seat.driver is None else {'cp': seat.driver.classpath, 'fqcn': seat.driver.fqcn}
    return {'deck': seat.deck_path, 'driver': driver}


def encode_task(task: GameTask) -> str:
    """Serialize a :class:`GameTask` to a ``TASK {json}`` wire line (no trailing newline)."""
    payload = {
        'id': task.task_id,
        'fmt': task.fmt,
        'a': _seat_json(task.seat_a),
        'b': _seat_json(task.seat_b),
    }
    return 'TASK ' + json.dumps(payload, separators=(',', ':'))


def _json_body(line: str, prefix: str) -> dict[str, Any]:
    try:
        body = json.loads(line[len(prefix) :])
    except json.JSONDecodeError as exc:
        msg = f'malformed {prefix.strip()} json: {line!r}'
        raise ProtocolError(msg) from exc
    if not isinstance(body, dict):
        msg = f'{prefix.strip()} payload must be an object: {line!r}'
        raise ProtocolError(msg)
    return body


_END_CAUSE_PREFIX = 'end_cause='


def _end_cause_from_markers(markers: list[str]) -> str | None:
    """Extract the ``end_cause=<value>`` terminal-cause marker (the worker emits it in the marker
    list, same pattern as ``reason=``/``decisive=``), or ``None`` when absent (a legacy row)."""
    for m in markers:
        if isinstance(m, str) and m.startswith(_END_CAUSE_PREFIX):
            return m[len(_END_CAUSE_PREFIX) :]
    return None


def _require(body: dict[str, Any], key: str, line: str) -> Any:
    if key not in body:
        msg = f'missing {key!r} in {line!r}'
        raise ProtocolError(msg)
    return body[key]


def parse_line(line: str) -> ProtocolMsg:
    """Parse one worker→governor line into a typed message, or raise :class:`ProtocolError`."""
    stripped = line.strip()
    if not stripped:
        raise ProtocolError('empty line')

    if stripped == 'READY':
        return Ready()

    if stripped.startswith('GAME '):
        return _parse_heartbeat(stripped)

    if stripped.startswith('RESULT '):
        body = _json_body(stripped, 'RESULT ')
        markers = _validate_markers(body.get('markers'), line)
        return GameResult(
            task_id=str(_require(body, 'id', line)),
            winner=_validate_winner(body.get('winner'), line),
            kill_turn=body.get('kill_turn'),
            ms=_require_ms(body.get('ms'), line),
            markers=markers,
            log_path=body.get('log'),
            reason=_validate_reason(body.get('reason'), line),
            end_cause=_end_cause_from_markers(markers),
        )

    if stripped.startswith('ERROR '):
        body = _json_body(stripped, 'ERROR ')
        return GameError(task_id=str(_require(body, 'id', line)), exc=str(body.get('exc', '')))

    msg = f'unrecognized protocol line: {line!r}'
    raise ProtocolError(msg)


def _parse_heartbeat(stripped: str) -> Heartbeat:
    # Shape: ``GAME <id> turn=N ms=M`` — id may itself contain no whitespace.
    parts = stripped.split()
    if len(parts) != 4 or not parts[2].startswith('turn=') or not parts[3].startswith('ms='):
        msg = f'malformed GAME heartbeat: {stripped!r}'
        raise ProtocolError(msg)
    try:
        turn = int(parts[2][len('turn=') :])
        ms = int(parts[3][len('ms=') :])
    except ValueError as exc:
        msg = f'non-integer field in GAME heartbeat: {stripped!r}'
        raise ProtocolError(msg) from exc
    if ms < 0:
        msg = f'negative ms in GAME heartbeat: {stripped!r}'
        raise ProtocolError(msg)
    return Heartbeat(task_id=parts[1], turn=turn, ms=ms)
