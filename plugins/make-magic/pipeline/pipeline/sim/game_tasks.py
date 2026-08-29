"""Phase 1.1 — the game-task model + the completeness-guaranteeing task-list builder.

The persistent-worker queue schedules at **one game** granularity. A compare request
expands to a flat, deterministic ``list[GameTask]``; the atomic unit rides the wire as
JSON (see :mod:`pipeline.sim.game_protocol`) and the worker echoes only ``task_id``.

Seats are **symmetric** — each carries a deck + an optional nested :class:`DriverRef`
(the dual-driver methodology, design §3). The builder enumerates **both** pilotings
(``driven`` / ``baseline``) for every ``(subject, opponent)`` pair, which is the
structural fix for the historical ``cp7 0/0`` bug: a subject is not "done" until every
driven *and* baseline task has drained.

The ``(subject, opponent, piloting)`` cell key lives only in the governor's registry; it
is derivable from ``task_id`` (:func:`cell_key`) so the worker never needs it.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = (
    'PILOTINGS',
    'DriverRef',
    'GameTask',
    'SeatSpec',
    'build_game_tasks',
    'cell_key',
)

#: The two pilotings enumerated per cell. ``driven`` = subject runs its own driver;
#: ``baseline`` = subject runs bare CP7 (``seat_a.driver=None``). The opponent is driven
#: in BOTH arms, so the delta isolates the subject's driver.
PILOTINGS: tuple[str, str] = ('driven', 'baseline')

#: The field separator inside a ``task_id`` (``subject|opponent|piloting|game_index``).
#: Deck identifiers must not contain it.
_SEP = '|'


@dataclass(frozen=True)
class DriverRef:
    """A compiled driver: the classpath dir to load and the fully-qualified class name."""

    classpath: str
    fqcn: str


@dataclass(frozen=True)
class SeatSpec:
    """One seat: a staged deck path + an optional driver (``None`` = thin deck / bare CP7)."""

    deck_path: str
    driver: DriverRef | None


@dataclass(frozen=True)
class GameTask:
    """One game to run: a deterministic id, the format, and the two symmetric seats.

    ``task_id`` is ``f"{subject}|{opponent}|{piloting}|{game_index}"`` — deterministic and
    unique across a run. ``fmt`` is ``"commander"`` or ``"constructed"`` (single field;
    maps to the Java boolean). ``seat_a`` is the subject, ``seat_b`` the opponent.
    """

    task_id: str
    fmt: str
    seat_a: SeatSpec
    seat_b: SeatSpec


def _ident(seat: SeatSpec) -> str:
    """The stable, ``|``-free identifier for a contestant (its deck path)."""
    ident = seat.deck_path
    if _SEP in ident:
        msg = f'deck path must not contain {_SEP!r} (breaks task_id/cell_key): {ident!r}'
        raise ValueError(msg)
    return ident


def build_game_tasks(
    subjects: list[SeatSpec] | tuple[SeatSpec, ...],
    field: list[SeatSpec] | tuple[SeatSpec, ...],
    games: int,
    *,
    fmt: str = 'commander',
) -> list[GameTask]:
    """Expand a compare request into the flat, deterministic task list.

    For each ``subject`` x each ``opponent`` in ``field`` x each ``piloting`` x each
    ``game_index in range(games)`` emit one :class:`GameTask`. ``driven`` puts the
    subject's driver on ``seat_a``; ``baseline`` clears it (``seat_a.driver=None``).
    ``seat_b`` **always** carries the opponent's driver (opponent driven in both arms).
    A subject/opponent with ``driver=None`` (thin deck) yields driverless seats.

    The iteration order is subject-major → opponent → piloting → game_index, giving a
    stable, unique task_id per game.
    """
    if games <= 0:
        msg = f'games must be positive, got {games}'
        raise ValueError(msg)

    tasks: list[GameTask] = []
    for subject in subjects:
        s_id = _ident(subject)
        for opponent in field:
            o_id = _ident(opponent)
            for piloting in PILOTINGS:
                seat_a_driver = subject.driver if piloting == 'driven' else None
                seat_a = SeatSpec(deck_path=subject.deck_path, driver=seat_a_driver)
                seat_b = SeatSpec(deck_path=opponent.deck_path, driver=opponent.driver)
                for game_index in range(games):
                    task_id = _SEP.join((s_id, o_id, piloting, str(game_index)))
                    tasks.append(GameTask(task_id=task_id, fmt=fmt, seat_a=seat_a, seat_b=seat_b))

    # Completeness contract: task_ids must be unique, else a game silently overwrites
    # another's tally. Two subjects (or a subject/opponent) sharing a deck_path collide
    # here — reject at construction rather than under-counting a cell at aggregation.
    seen: set[str] = set()
    for t in tasks:
        if t.task_id in seen:
            msg = (
                f'duplicate task_id {t.task_id!r} — a shared deck_path collapses distinct '
                'contestants into one id (completeness violation); deck paths must be unique'
            )
            raise ValueError(msg)
        seen.add(t.task_id)
    return tasks


def cell_key(task: GameTask) -> tuple[str, str, str]:
    """The ``(subject, opponent, piloting)`` aggregation key, derived from ``task_id``.

    The governor tallies per cell; the worker never needs this — it just echoes ``task_id``.
    """
    parts = task.task_id.split(_SEP)
    if len(parts) != 4:
        msg = f'malformed task_id (expected 4 {_SEP!r}-fields): {task.task_id!r}'
        raise ValueError(msg)
    subject, opponent, piloting, _game_index = parts
    return subject, opponent, piloting
