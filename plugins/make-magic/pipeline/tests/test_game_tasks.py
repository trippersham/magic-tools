"""Phase 1.1 — task model + task-list builder (pure, no JVM).

The completeness guarantee (both pilotings per cell) is the structural fix for the
historical ``cp7 0/0`` bug — a subject was never "done" because its baseline arm had
zero enumerated tasks. These tests pin the enumeration, determinism, and seat wiring.
"""

from __future__ import annotations

import pytest

from pipeline.sim.game_tasks import (
    DriverRef,
    SeatSpec,
    build_game_tasks,
    cell_key,
)


def _subject(name: str, *, driven: bool = True) -> SeatSpec:
    drv = DriverRef(classpath=f'/cp/{name}', fqcn=f'com.x.{name}Driver') if driven else None
    return SeatSpec(deck_path=f'/decks/{name}.dck', driver=drv)


def test_every_cell_has_both_pilotings_and_games_tasks() -> None:
    """Regression for cp7 0/0: each (subject,opp,piloting) cell gets exactly ``games`` tasks,
    and BOTH pilotings are enumerated for every subject/opponent pair."""
    subjects = [_subject('s1'), _subject('s2')]
    field = [_subject('o1'), _subject('o2')]
    games = 3

    tasks = build_game_tasks(subjects, field, games)

    cells: dict[tuple[str, str, str], int] = {}
    for t in tasks:
        cells[cell_key(t)] = cells.get(cell_key(t), 0) + 1

    # 2 subjects * 2 opponents * 2 pilotings = 8 cells, each with `games` tasks.
    assert len(cells) == 8
    assert all(count == games for count in cells.values())
    pilotings = {key[2] for key in cells}
    assert pilotings == {'driven', 'baseline'}
    assert len(tasks) == 8 * games


def test_task_ids_unique_and_deterministic() -> None:
    subjects = [_subject('s1'), _subject('s2')]
    field = [_subject('o1')]
    first = build_game_tasks(subjects, field, 2)
    second = build_game_tasks(subjects, field, 2)

    ids = [t.task_id for t in first]
    assert len(ids) == len(set(ids))  # unique
    assert [t.task_id for t in second] == ids  # deterministic (same inputs -> same list)


def test_baseline_drops_subject_driver_keeps_opponent_driver() -> None:
    subjects = [_subject('s1')]
    field = [_subject('o1')]
    tasks = build_game_tasks(subjects, field, 1)

    by_piloting = {cell_key(t)[2]: t for t in tasks}
    driven = by_piloting['driven']
    baseline = by_piloting['baseline']

    # driven arm: subject carries its driver.
    assert driven.seat_a.driver is not None
    # baseline arm: subject seat is bare (driver=None) ...
    assert baseline.seat_a.driver is None
    # ... but the opponent is driven in BOTH arms (dual-driver methodology).
    assert driven.seat_b.driver is not None
    assert baseline.seat_b.driver is not None


def test_thin_decks_yield_driverless_seats() -> None:
    subjects = [_subject('s1', driven=False)]
    field = [_subject('o1', driven=False)]
    tasks = build_game_tasks(subjects, field, 1)

    for t in tasks:
        assert t.seat_a.driver is None
        assert t.seat_b.driver is None


def test_cell_key_derives_from_task_id() -> None:
    subjects = [_subject('s1')]
    field = [_subject('o1')]
    (t,) = [x for x in build_game_tasks(subjects, field, 1) if cell_key(x)[2] == 'driven']
    subject, opponent, piloting = cell_key(t)
    assert piloting == 'driven'
    # task_id encodes the cell + game index deterministically.
    assert t.task_id == f'{subject}|{opponent}|{piloting}|0'


def test_fmt_field_flows_onto_tasks() -> None:
    tasks = build_game_tasks([_subject('s1')], [_subject('o1')], 1, fmt='constructed')
    assert all(t.fmt == 'constructed' for t in tasks)


def test_colliding_deck_path_raises_completeness_error() -> None:
    """Two subjects sharing a deck_path collide on task_id — a silent completeness
    violation. The builder must reject it at construction (MINOR-3)."""
    subjects = [
        SeatSpec(deck_path='/decks/dup.dck', driver=None),
        SeatSpec(deck_path='/decks/dup.dck', driver=None),  # same path → same s_id.
    ]
    field = [_subject('o1')]
    with pytest.raises(ValueError, match='duplicate task_id'):
        build_game_tasks(subjects, field, 1)
