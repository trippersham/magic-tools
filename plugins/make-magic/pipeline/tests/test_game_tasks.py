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
    starter_for_index,
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


def test_baseline_only_subject_emits_single_arm() -> None:
    """A baseline-only SUBJECT enumerates ONLY the baseline arm (one piloting x games per opponent),
    with task ids keeping the ``subject|opponent|baseline|index`` shape."""
    field = [_subject('o1'), _subject('o2')]
    games = 3
    tasks = build_game_tasks([], field, games, baseline_only_subjects=[_subject('s1', driven=False)])

    cells: dict[tuple[str, str, str], int] = {}
    for t in tasks:
        cells[cell_key(t)] = cells.get(cell_key(t), 0) + 1
    # 1 subject * 2 opponents * ONE (baseline) arm = 2 cells, each with `games` tasks.
    assert len(cells) == 2
    assert {key[2] for key in cells} == {'baseline'}
    assert all(count == games for count in cells.values())
    assert len(tasks) == 2 * games
    assert all(t.task_id.split('|')[2] == 'baseline' for t in tasks)
    # the subject seat is bare in every task; the opponent still carries its own driver.
    assert all(t.seat_a.driver is None for t in tasks)


def test_mixed_roster_two_arm_and_baseline_only() -> None:
    """A roster mixing a driven subject (two arms) and a driverless subject (baseline only): each
    subject enumerates its OWN arm set — the two-arm subject is unaffected by the single-arm one."""
    field = [_subject('o1')]
    tasks = build_game_tasks(
        [_subject('driven1', driven=True)], field, 2,
        baseline_only_subjects=[_subject('thin1', driven=False)],
    )
    arms_by_subject: dict[str, set[str]] = {}
    for t in tasks:
        subj, _opp, pil = cell_key(t)
        arms_by_subject.setdefault(subj, set()).add(pil)
    driven_subj = next(s for s in arms_by_subject if 'driven1' in s)
    thin_subj = next(s for s in arms_by_subject if 'thin1' in s)
    assert arms_by_subject[driven_subj] == {'driven', 'baseline'}
    assert arms_by_subject[thin_subj] == {'baseline'}
    # task-count math: 2-arm subject = 1 opp * 2 arms * 2 games = 4; 1-arm subject = 1*1*2 = 2.
    assert len(tasks) == 4 + 2


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


def test_starter_for_index_alternates_by_parity() -> None:
    assert [starter_for_index(i) for i in range(4)] == ['A', 'B', 'A', 'B']


def test_starter_split_is_6_6_per_12_game_cell() -> None:
    """A 12-game cell alternates the first-turn seat 6/6 by game index (removes the seat confound)."""
    tasks = build_game_tasks([_subject('s1')], [_subject('o1')], 12)
    by_cell: dict[tuple[str, str, str], list[str]] = {}
    for t in tasks:
        by_cell.setdefault(cell_key(t), []).append(t.starter)
    for starters in by_cell.values():
        assert starters.count('A') == 6
        assert starters.count('B') == 6


def test_driven_and_baseline_arms_share_identical_starter_schedule() -> None:
    """The delta's internal validity requires the two arms to be index-matched: game i has the
    SAME starter in driven and baseline, so the first-player effect cancels within the delta."""
    tasks = build_game_tasks([_subject('s1')], [_subject('o1')], 12)
    driven = {t.task_id.rsplit('|', 1)[1]: t.starter for t in tasks if cell_key(t)[2] == 'driven'}
    baseline = {t.task_id.rsplit('|', 1)[1]: t.starter for t in tasks if cell_key(t)[2] == 'baseline'}
    assert driven == baseline
    # and it actually alternates (not a degenerate all-A schedule).
    assert set(driven.values()) == {'A', 'B'}


def test_starter_default_is_a() -> None:
    """A GameTask constructed without an explicit starter defaults to legacy 'A' (subject on play)."""
    tasks = build_game_tasks([_subject('s1')], [_subject('o1')], 1)
    assert all(t.starter == 'A' for t in tasks)  # game index 0 → A in both arms


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
