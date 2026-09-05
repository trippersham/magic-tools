"""Batch re-gate — the post-jar-cut chore as one command (issue #52, release-blocking).

A jar cut bumps ``harness_version``, which turns EVERY gated per-deck driver ``'stale'``
(:func:`pipeline.sim.drivers.driver_state`) and quietly reverts Tier-2 Speed to Tier-1. This
walks the driver-batch ledger (:mod:`pipeline.sim.driver_batch`), classifies every driver's
state, and — outside dry-run — RE-GATES each stale driver against the current harness
(:func:`pipeline.sim.driver_gate.regate_driver`), so the chore is a single invocation:

    python -m pipeline.sim.regate_batch            # re-gate every stale driver
    python -m pipeline.sim.regate_batch --dry-run  # just LIST states, change nothing

Reports a per-driver line (``valid`` / ``stale`` / ``regated`` / ``failed`` / ``absent`` /
``broken`` / ``environment``) and a summary. Exits NONZERO if ANY driver FAILED its re-gate (a
compile/gate rejection) — a broken driver post-cut is a release signal, not a silent revert.
The dry-run path never re-gates and never exits nonzero on stale alone.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.sim import driver_batch, drivers

if TYPE_CHECKING:
    from collections.abc import Callable

    from pipeline.contracts.models import Deck

log = logging.getLogger('make_magic.sim.regate_batch')

#: Per-driver report outcomes. ``valid`` needed nothing; ``regated`` was stale and re-gated
#: clean; ``failed`` was stale and its recompile/gate REJECTED it; ``environment`` was stale but
#: the toolchain could not run here; ``broken`` was already gates_passed=false; ``absent`` had no
#: driver on disk (never authored — nothing to re-gate).
_OUTCOMES = ('valid', 'regated', 'failed', 'environment', 'broken', 'absent')


@dataclass(frozen=True)
class RegateReport:
    """One driver's row in the batch report."""

    deck_id: str
    name: str
    state: str  #: the pre-run driver_state
    outcome: str  #: one of _OUTCOMES
    reason: str = ''


def _driver_rows(ledger: driver_batch.Ledger) -> list[dict[str, Any]]:
    """The ledger rows that carry a compiled per-deck driver (DRIVE + at/after ``compiled``).

    THIN rows run bare CP7 (no driver) and rows short of ``compiled`` never produced bytecode —
    neither has a stamp to re-gate, so both are skipped from the driver walk.
    """
    compiled_rank = driver_batch.stage_rank('compiled')
    rows: list[dict[str, Any]] = []
    for row in ledger.rows():
        if not row.get('drive'):
            continue
        stage = row.get('stage')
        if stage is None or driver_batch.stage_rank(stage) < compiled_rank:
            continue
        rows.append(row)
    return rows


def _default_deck_ref(deck: Deck, row: dict[str, Any]) -> tuple[str, str]:
    """Render the ``(name, dck_text)`` the gate's goldfish consumes for a ledger row.

    Gauntlet rows carry their ``.dck`` on disk under the packaged corpus; inventory rows export
    via the Forge exporter. Best-effort — a resolution failure surfaces as an environment outcome
    when the gate then cannot run.
    """
    from pipeline.destinations.deck_export import get_exporter

    return (deck.name, get_exporter('forge_dck').export(deck))


def run_regate_batch(
    *,
    ledger_path: str | os.PathLike[str],
    data_dir: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
    install: object | None = None,
    games: int = 20,
    regate_fn: Callable[..., Any] | None = None,
    deck_ref_fn: Callable[[Deck, dict[str, Any]], tuple[str, str]] | None = None,
) -> list[RegateReport]:
    """Walk the driver ledger, classify each driver, and (unless ``dry_run``) re-gate the stale.

    Returns one :class:`RegateReport` per driver. ``regate_fn`` / ``deck_ref_fn`` are injectable
    seams so the state machine is unit-testable with no real JVM/ECJ. In production ``regate_fn``
    defaults to :func:`pipeline.sim.driver_gate.regate_driver` and needs a live ``install``.
    """
    ledger = driver_batch.Ledger(Path(ledger_path))
    reports: list[RegateReport] = []
    resolve_ref = deck_ref_fn if deck_ref_fn is not None else _default_deck_ref

    for row in _driver_rows(ledger):
        deck = driver_batch._deck_for_row(row)
        deck_id = str(row.get('deck_id', deck.uuid))
        state = drivers.driver_state(deck, data_dir=data_dir)

        if dry_run or state in ('valid', 'absent', 'broken'):
            # Dry-run reports the raw state as the outcome; a live run has nothing to DO for a
            # valid/absent/broken driver (only STALE is auto-recoverable here).
            reports.append(RegateReport(deck_id=deck_id, name=deck.name, state=state, outcome=state))
            continue

        # state == 'stale' on a live run → re-gate.
        regate = regate_fn if regate_fn is not None else _default_regate
        ref = resolve_ref(deck, row)
        rg = regate(deck, ref, install=install, games=games, data_dir=data_dir)
        if rg.ok:
            outcome = 'regated'
        elif rg.outcome == 'environment':
            outcome = 'environment'
        else:
            outcome = 'failed'
        reports.append(
            RegateReport(deck_id=deck_id, name=deck.name, state=state, outcome=outcome, reason=rg.reason)
        )
    return reports


def _default_regate(deck, deck_ref, **kwargs):  # type: ignore[no-untyped-def]
    from pipeline.sim.driver_gate import regate_driver

    return regate_driver(deck, deck_ref, **kwargs)


def run(argv: list[str] | None = None) -> int:
    """CLI entry: walk the ledger, (re-)gate stale drivers, print the report. Returns exit code.

    Exit is NONZERO iff at least one driver FAILED its re-gate (a compile/gate rejection).
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog='regate-drivers',
        description='Batch re-gate every stale per-deck driver against the current harness.',
    )
    parser.add_argument('--ledger', default=None, help='Ledger JSONL path (default: the data-dir ledger).')
    parser.add_argument('--dry-run', action='store_true', help='List driver states only; re-gate nothing.')
    parser.add_argument('--games', type=int, default=20, help='Games per re-gate goldfish (default 20).')
    args = parser.parse_args(argv)  # argv=None → argparse reads sys.argv (the real CLI path).

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

    ledger_path = Path(args.ledger) if args.ledger else driver_batch.default_ledger_path()
    install = None
    if not args.dry_run:
        try:
            from pipeline.sim import xmage_runtime as xr

            install = xr.resolve()
        except Exception as exc:  # no reachable XMage install — re-gate cannot run.
            log.error('cannot resolve an XMage install to re-gate against: %s', exc)
            return 2

    reports = run_regate_batch(
        ledger_path=ledger_path, dry_run=args.dry_run, install=install, games=args.games,
    )

    for r in reports:
        line = f'{r.outcome:<12} {r.state:<8} {r.deck_id}  ({r.name})'
        if r.reason:
            line += f'  — {r.reason}'
        print(line)

    summary: dict[str, int] = {}
    for r in reports:
        summary[r.outcome] = summary.get(r.outcome, 0) + 1
    print('\n' + json.dumps({'total': len(reports), 'by_outcome': dict(sorted(summary.items())),
                             'dry_run': bool(args.dry_run), 'ledger': str(ledger_path)}, indent=2))

    failed = summary.get('failed', 0)
    if failed:
        log.error('%d driver(s) FAILED re-gate — see the report above.', failed)
        return 1
    return 0


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run(argv))


if __name__ == '__main__':
    main()
