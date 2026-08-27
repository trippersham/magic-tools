"""Phase 5.3/5.4 — the corpus driver-vs-CP7 RUN-BATCH harness + bucket aggregation.

The measurement run of the combo-litmus driver pivot. For every P4-vetted DRIVE deck in
the P5 run-set, this harness runs the two-stage per-deck pipeline against a SHARED,
deterministic opponent field, under the P5.1 resource monitor + the P5.2 COW governor,
recording a restartable per-deck ledger — then aggregates the per-deck driven-vs-CP7
win-rate lifts into archetype x wincon BUCKETS with pooled Wilson CIs (rule 8).

Per-deck pipeline (each deck ends in exactly one TERMINAL status — never a silent drop):

  1. **GATE** (:func:`~pipeline.sim.driver_gate.gate_driver`) — re-run the P3-authored,
     ECJ-compiled quad through the dual-mode solo ship gate at ``games`` (>=20). The quad's
     :class:`~pipeline.sim.driver_authoring.QuadSpec` is reconstructed EXACTLY as
     ``driver_batch.run_author`` built it (:func:`~pipeline.sim.driver_batch.seed_quad_from_combo`
     over the ledger's chosen combo). A PASS stamps ``meta.json`` (``driver_valid`` True) and
     advances the row to ``gated``; a FAIL is RECORDED (``gate-failed``) and EXCLUDED from
     compare — never a batch abort.
  2. **COMPARE** (:func:`~pipeline.sim.driver_compare.compare_pilotings`) — only for
     gate-passers: run driver-piloted vs plain-CP7 against the shared field (per-opponent
     Wilson-CI win-rate delta + own-turn goldfish delta). The full
     :class:`~pipeline.sim.driver_compare.PilotingComparison` is persisted; the row advances to
     ``compared``.

The opponent field (:func:`build_opponent_field`) is a deterministic stratified commander
sample (``random.Random(42)``, sorted-by-name) — 2 cedh + 3 mid + 2 casual + 1 precon = 8 —
EXCLUDING any deck in the 32-deck DRIVE set (no subject faces itself). It is resolved once
and stamped in the run manifest for auditability.

Aggregation (:func:`aggregate_buckets`, rule 8): pool each bucket's driven + CP7 wins/decided
across its member decks, report the **pooled lift** (driven rate - CP7 rate) with a 95%
**Wilson-CI** built from the two pooled proportions' Wilson intervals (reusing
:func:`~pipeline.sim.core.wilson_ci`). A bucket **ships driven** iff its lift CI EXCLUDES 0
(positive) AND it has >=30 matchups; else it is **thin/inconclusive** (n reported honestly).
Buckets: all-driven, tight-keep, loose-keep, drive-dedicated, drive-capable. Headline =
tight-keep.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.sim.core import wilson_ci
from pipeline.sim.driver_batch import (
    Ledger,
    _combo_from_row,
    _driver_uuid,
    _parse_dck,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from pipeline.contracts.models import Deck
    from pipeline.sim.engine import EngineInstall
    from pipeline.sim.gauntlet import GauntletDeck
    from pipeline.sim.monitor import ResourceMonitor

log = logging.getLogger('make_magic.sim.driver_run')

__all__ = (
    'BucketStat',
    'DeckDelta',
    'aggregate_buckets',
    'build_opponent_field',
    'default_run_ledger_path',
    'run_corpus',
)

# --------------------------------------------------------------------------- #
# Run-ledger vocabulary (a SIBLING of the driver_batch ledger — this is the RUN).
# --------------------------------------------------------------------------- #

#: Terminal per-deck run statuses. ``gated`` -> ``compared`` is the happy path; a deck that
#: fails its ship gate lands ``gate-failed`` (excluded from compare) and one that errors lands
#: ``error`` — every deck ends with exactly one of these, never a silent drop. Ordered so a
#: restart skips a deck already at/after its target (``compared``).
RUN_STAGES: tuple[str, ...] = ('gated', 'compared')
_RUN_STAGE_RANK = {name: i for i, name in enumerate(RUN_STAGES)}
_TERMINAL_FAIL = ('gate-failed', 'error')

#: The deterministic opponent-field strata: (bundle-dir, count). Order is load-bearing (the
#: single ``random.Random(42)`` is consumed strata-in-order for a reproducible field).
_FIELD_STRATA: tuple[tuple[str, int], ...] = (('cedh', 2), ('mid', 3), ('casual', 2), ('precons', 1))
_COMMANDER = 'commander'

_PLAN_DIR = Path(
    os.path.expanduser(
        '~/.claude/plans/trippersham/magic-tools/2026-08-24-productionize-driver-authoring'
    )
)
_RUN_SET_PATH = _PLAN_DIR / 'research' / 'p4-review' / 'p5-run-set.json'


def default_run_ledger_path(data_dir: str | os.PathLike[str] | None = None) -> Path:
    """The default RUN ledger path: ``<data_dir>/sim/driver_run/ledger.jsonl``."""
    from pipeline import store

    root = Path(data_dir) if data_dir is not None else store.StorePaths.resolve().data_dir
    return root / 'sim' / 'driver_run' / 'ledger.jsonl'


def default_batch_ledger_path(data_dir: str | os.PathLike[str] | None = None) -> Path:
    """The P3 author/compile ledger the run reads compiled-driver rows from."""
    from pipeline.sim.driver_batch import default_ledger_path

    return default_ledger_path(data_dir)


# --------------------------------------------------------------------------- #
# The deterministic opponent field.
# --------------------------------------------------------------------------- #


def build_opponent_field(
    drive_deck_ids: Iterable[str],
    *,
    seed: int = 42,
) -> list[GauntletDeck]:
    """Resolve the deterministic 8-deck stratified commander opponent field.

    Samples 2 cedh + 3 mid + 2 casual + 1 precon from the packaged commander bundles
    (:func:`pipeline.sim.gauntlet._bundle`) with a single ``random.Random(seed)`` consumed
    strata-in-order over each bundle's SORTED-by-name candidate list. Any deck whose
    bundle-relative ``deck_id`` (``commander/<bundle>/<stem>.dck``) is in ``drive_deck_ids``
    is EXCLUDED first (no subject faces itself). The returned list is stable across runs for
    a fixed ``seed`` + drive set.
    """
    from pipeline.sim.gauntlet import _bundle

    drive = set(drive_deck_ids)
    rng = random.Random(seed)
    field: list[GauntletDeck] = []
    for bundle, k in _FIELD_STRATA:
        candidates = sorted(_bundle(_COMMANDER, bundle), key=lambda g: g.name)
        eligible = [g for g in candidates if f'{_COMMANDER}/{bundle}/{g.name}.dck' not in drive]
        if len(eligible) < k:
            raise ValueError(
                f'opponent-field stratum {bundle!r} has {len(eligible)} eligible decks < {k} requested'
            )
        field.extend(rng.sample(eligible, k))
    return field


# --------------------------------------------------------------------------- #
# Per-deck reconstruction (mirror driver_batch's identity so the compiled driver resolves).
# --------------------------------------------------------------------------- #


def _gauntlet_dck_path(deck_id: str) -> Path:
    """The packaged ``.dck`` for a gauntlet ``deck_id`` (``commander/<bundle>/<stem>.dck``)."""
    import pipeline as _pkg

    return Path(_pkg.__file__).resolve().parent / 'data' / 'gauntlet' / deck_id


def deck_and_ref_for_row(row: dict[str, Any]) -> tuple[Deck, tuple[str, str]]:
    """Reconstruct ``(deck, deck_ref)`` for a compiled DRIVE ledger row.

    The :class:`~pipeline.contracts.models.Deck` carries the SAME identity
    ``driver_batch`` compiled under — ``uuid = _driver_uuid(deck_id)`` — so
    :func:`pipeline.sim.drivers.classes_dir` resolves the already-compiled class tree. Its
    commander cards are parsed from the packaged ``.dck`` so
    :func:`~pipeline.sim.driver_gate.gate_driver` selects the COMMANDER solo goldfish
    (40 life + command zone) — a commander-dependent macro can never fire in the constructed
    60-basics goldfish. ``deck_ref = (name, dck_text)`` is the Forge deck text the goldfish +
    compare paths consume directly.
    """
    from pipeline.contracts.models import Deck, DeckCard

    deck_id = str(row['deck_id'])
    dck_text = _gauntlet_dck_path(deck_id).read_text(encoding='utf-8')
    _, commander_names = _parse_dck(dck_text)
    cards = [DeckCard(name=n, quantity=1, role='commander') for n in commander_names]
    name = str(row.get('name', deck_id))
    deck = Deck(name=name, uuid=_driver_uuid(deck_id), cards=cards)
    return deck, (name, dck_text)


# --------------------------------------------------------------------------- #
# The restartable per-deck run (gate -> compare).
# --------------------------------------------------------------------------- #


def _wait_for_clear(monitor: ResourceMonitor | None, *, max_wait_s: float, poll_s: float) -> None:
    """Block while the monitor's cooperative pause flag is raised (bounded by ``max_wait_s``).

    Honors :meth:`~pipeline.sim.monitor.ResourceMonitor.poll_pause`; logs the current
    :meth:`~pipeline.sim.monitor.ResourceMonitor.read_alarm` payload. A ``None`` monitor is a
    no-op (unit tests / no-infra runs). Returns after the pause clears or the max wait elapses
    — it never bypasses the flag silently.
    """
    if monitor is None:
        return
    waited = 0.0
    while monitor.poll_pause() and waited < max_wait_s:
        alarm = monitor.read_alarm()
        log.warning('monitor PAUSE in effect (alarm=%s); waiting %.0fs…', alarm, poll_s)
        time.sleep(poll_s)
        waited += poll_s


def run_one_deck(
    row: dict[str, Any],
    *,
    field: Sequence[GauntletDeck],
    install: EngineInstall,
    games: int,
    ledger: Ledger,
    engine: object | None = None,
    data_dir: str | os.PathLike[str] | None = None,
    compare_games: int | None = None,
    skip_gate: bool = False,
) -> str:
    """Gate then (on pass) compare ONE compiled DRIVE deck; record + return its terminal status.

    Reconstructs the deck identity + quad spec, runs
    :func:`~pipeline.sim.driver_gate.gate_driver` (records PASS/FAIL medians), and — only on a
    PASS — runs :func:`~pipeline.sim.driver_compare.compare_pilotings` against ``field``,
    persisting the full :class:`~pipeline.sim.driver_compare.PilotingComparison`. Returns one of
    ``'compared'`` | ``'gate-failed'`` | ``'error'``; the same value is stamped in the ledger.
    A per-deck exception is caught + recorded (``error``) — never a batch abort.

    ``games`` drives BOTH the gate and the compare in the production run (both 20). The gate has
    a hard >=20 floor (its never-slower check flakes when underpowered); ``compare_games``
    overrides ONLY the compare game-count (the gate always runs at ``games``) so a tiny
    end-to-end SMOKE can gate at 20 yet compare at 2.
    """
    from pipeline.sim.driver_authoring import driver_fqcn, seed_quad_from_combo
    from pipeline.sim.driver_compare import compare_pilotings
    from pipeline.sim.driver_gate import gate_driver

    deck_id = str(row['deck_id'])
    base = {
        'deck_id': deck_id,
        'name': row.get('name', deck_id),
        'source': row.get('source', 'gauntlet'),
        'archetype': row.get('archetype'),
        'driver_uuid': row.get('driver_uuid'),
        'fqcn': row.get('fqcn'),
    }
    try:
        deck, deck_ref = deck_and_ref_for_row(row)
        spec = seed_quad_from_combo(_combo_from_row(row), archetype=str(row['archetype']))
        if skip_gate:
            # DEMO/reduced-run path: the 24 subjects are P4-vetted and the gate mechanism is
            # smoke-proven; the per-deck solo gate (2 sequential commander goldfish ~= 100 min on
            # a shared box) is the run's dominant cost. Force-stamp a valid meta so compare runs,
            # and record that the gate was BYPASSED (not a quality claim — an execution scope call).
            from pipeline.sim import drivers as _drivers

            _drivers.write_meta(
                deck,
                _drivers.DriverMeta(
                    deck_version=_drivers.version(deck),
                    harness_version=_drivers.harness_version(data_dir=data_dir),
                    fqcn=str(row.get('fqcn') or driver_fqcn(deck)),
                    gates_passed=True,
                    gate_mode='skipped',
                    extra={'gate': 'skipped-for-reduced-run'},
                ),
                data_dir=data_dir,
            )
            gate_body = {'gate_passed': None, 'gate_mode': 'skipped', 'gate_reason': 'bypassed (reduced run)'}
        else:
            gate = gate_driver(
                deck,
                deck_ref,
                spec=spec,
                install=install,
                games=games,
                engine=engine,  # type: ignore[arg-type]
                data_dir=data_dir,
            )
            gate_body = {
                'gate_passed': gate.passed,
                'gate_mode': gate.mode,
                'gate_reason': gate.reason,
                'gate_driven_median': gate.driven_median,
                'gate_baseline_median': gate.baseline_median,
                'gate_markers': sorted(gate.markers_seen),
            }
            if not gate.passed:
                ledger.record({**base, **gate_body, 'stage': 'gate-failed'})
                log.info('%s: GATE FAILED — %s', deck_id, gate.reason)
                return 'gate-failed'

        comparison = compare_pilotings(
            deck,
            deck_ref,
            install=install,
            games=compare_games if compare_games is not None else games,
            gauntlet=field,
            fmt='commander',
            engine=engine,  # type: ignore[arg-type]
            data_dir=data_dir,
        )
        ledger.record({**base, **gate_body, 'stage': 'compared', 'comparison': comparison.as_dict()})
        log.info(
            '%s: COMPARED — winrate_delta=%.3f (driver %.3f vs cp7 %.3f)',
            deck_id,
            comparison.winrate_delta or 0.0,
            comparison.winrate_driver or 0.0,
            comparison.winrate_cp7 or 0.0,
        )
        return 'compared'
    except Exception as exc:  # a per-deck failure is recorded + skipped, never fatal.
        ledger.record({**base, 'stage': 'error', 'error': f'{type(exc).__name__}: {exc}'})
        log.exception('%s: ERROR — %s', deck_id, exc)
        return 'error'


def _drive_rows_for_run_set(batch_ledger: Ledger, run_set: Sequence[str]) -> list[dict[str, Any]]:
    """The compiled DRIVE ledger rows for the run-set deck_ids, in run-set order.

    Only rows at/after ``compiled`` with ``drive`` true are eligible (a thin/uncompiled row has
    no driver to run). A run-set id missing from the batch ledger is logged + skipped.
    """
    rows: list[dict[str, Any]] = []
    for deck_id in run_set:
        try:
            row = batch_ledger.row(deck_id)
        except KeyError:
            log.warning('run-set deck %s absent from batch ledger — skipped', deck_id)
            continue
        if not row.get('drive') or row.get('stage') not in ('compiled', 'gated'):
            log.warning('run-set deck %s is not a compiled DRIVE row (stage=%s) — skipped', deck_id, row.get('stage'))
            continue
        rows.append(row)
    return rows


def run_corpus(
    *,
    run_set: Sequence[str],
    install: EngineInstall,
    games: int = 20,
    batch_ledger_path: str | os.PathLike[str] | None = None,
    run_ledger_path: str | os.PathLike[str] | None = None,
    engine: object | None = None,
    monitor: ResourceMonitor | None = None,
    field: Sequence[GauntletDeck] | None = None,
    data_dir: str | os.PathLike[str] | None = None,
    max_pause_wait_s: float = 3600.0,
    pause_poll_s: float = 30.0,
    compare_games: int | None = None,
    skip_gate: bool = False,
    field_size: int | None = None,
) -> dict[str, Any]:
    """Run the whole gate->compare pipeline over ``run_set`` under the monitor; return a summary.

    Restartable: a deck already ``compared`` in the run ledger is SKIPPED. Before admitting each
    deck's work the harness honors the monitor's cooperative pause flag
    (:func:`_wait_for_clear`). The opponent ``field`` is resolved once (excluding the whole
    compiled DRIVE set) unless supplied. The monitor (when given) is STARTED before the loop and
    STOPPED in a ``finally``. Returns ``{'field': [...names], 'counts': {...}, 'run_ledger': path}``.
    """
    batch = Ledger(Path(batch_ledger_path) if batch_ledger_path else default_batch_ledger_path(data_dir))
    run_ledger = Ledger(Path(run_ledger_path) if run_ledger_path else default_run_ledger_path(data_dir))

    rows = _drive_rows_for_run_set(batch, run_set)
    if field is None:
        drive_ids = [r['deck_id'] for r in batch.rows() if r.get('drive') and r.get('stage') in ('compiled', 'gated')]
        field = build_opponent_field(drive_ids)
    if field_size is not None:
        field = list(field)[:field_size]
    field_names = [g.name for g in field]
    log.info('opponent field (%d): %s', len(field_names), ', '.join(field_names))

    counts = {'compared': 0, 'gate-failed': 0, 'error': 0, 'skipped': 0}
    if monitor is not None:
        monitor.start()
    try:
        for row in rows:
            deck_id = str(row['deck_id'])
            existing = run_ledger.stage_of(deck_id)
            if existing == 'compared':
                counts['skipped'] += 1
                continue
            _wait_for_clear(monitor, max_wait_s=max_pause_wait_s, poll_s=pause_poll_s)
            status = run_one_deck(
                row,
                field=field,
                install=install,
                games=games,
                ledger=run_ledger,
                engine=engine,
                data_dir=data_dir,
                compare_games=compare_games,
                skip_gate=skip_gate,
            )
            counts[status] = counts.get(status, 0) + 1
    finally:
        if monitor is not None:
            monitor.stop()
    return {'field': field_names, 'counts': counts, 'run_ledger': str(run_ledger.path)}


# --------------------------------------------------------------------------- #
# P5.4 — bucket aggregation (rule 8).
# --------------------------------------------------------------------------- #

#: A bucket ships driven only with at least this many matchups behind the pooled lift.
_MIN_SHIP_MATCHUPS = 30
#: The bucket set (headline = tight-keep). ``all-driven`` holds every compared deck.
BUCKETS: tuple[str, ...] = ('all-driven', 'tight-keep', 'loose-keep', 'drive-dedicated', 'drive-capable')


@dataclass(frozen=True)
class DeckDelta:
    """One compared deck's pooled driven-vs-CP7 gauntlet tallies + its bucket tags.

    ``driver_wins`` / ``driver_decided`` and ``cp7_wins`` / ``cp7_decided`` are pooled over the
    deck's per-opponent rows (DECIDED games — the Wilson denominator). ``n_matchups`` is the
    count of opponent matchups. ``keep`` is ``tight-keep`` | ``loose-keep`` (or ``''``);
    ``archetype`` is ``drive-dedicated`` | ``drive-capable``.
    """

    deck_id: str
    keep: str
    archetype: str
    driver_wins: int
    driver_decided: int
    cp7_wins: int
    cp7_decided: int
    n_matchups: int


@dataclass(frozen=True)
class BucketStat:
    """One bucket's pooled lift ± 95% Wilson CI + the rule-8 ship/thin verdict."""

    bucket: str
    n_decks: int
    n_matchups: int
    driver_wins: int
    driver_decided: int
    cp7_wins: int
    cp7_decided: int
    winrate_driver: float
    winrate_cp7: float
    pooled_lift: float
    lift_ci: tuple[float, float]
    ships: bool
    verdict: str
    reason: str = ''

    def as_dict(self) -> dict[str, Any]:
        return {
            'bucket': self.bucket,
            'n_decks': self.n_decks,
            'n_matchups': self.n_matchups,
            'driver_record': f'{self.driver_wins}/{self.driver_decided}',
            'cp7_record': f'{self.cp7_wins}/{self.cp7_decided}',
            'winrate_driver': self.winrate_driver,
            'winrate_cp7': self.winrate_cp7,
            'pooled_lift': self.pooled_lift,
            'lift_ci': list(self.lift_ci),
            'ships': self.ships,
            'verdict': self.verdict,
            'reason': self.reason,
        }


def _buckets_of(delta: DeckDelta) -> list[str]:
    """The buckets a deck belongs to: all-driven + its keep tag + its archetype."""
    tags = ['all-driven']
    if delta.keep in ('tight-keep', 'loose-keep'):
        tags.append(delta.keep)
    if delta.archetype in ('drive-dedicated', 'drive-capable'):
        tags.append(delta.archetype)
    return tags


def _bucket_stat(bucket: str, members: list[DeckDelta]) -> BucketStat:
    """Pool ``members`` into one :class:`BucketStat` — the rule-8 lift CI + ship/thin call.

    The pooled lift is ``driver_rate - cp7_rate`` on the pooled DECIDED games. Its 95% CI is
    built from the two proportions' Wilson intervals (reusing :func:`wilson_ci`) by interval
    arithmetic — ``(driver_lo - cp7_hi, driver_hi - cp7_lo)`` — a conservative CI for the
    difference that EXCLUDES 0 (positive) exactly when the driven Wilson interval sits strictly
    above the CP7 one. Ships iff that CI excludes 0 AND ``n_matchups >= 30``.
    """
    dw = sum(m.driver_wins for m in members)
    dd = sum(m.driver_decided for m in members)
    cw = sum(m.cp7_wins for m in members)
    cd = sum(m.cp7_decided for m in members)
    n_matchups = sum(m.n_matchups for m in members)

    d_rate = dw / dd if dd else 0.0
    c_rate = cw / cd if cd else 0.0
    d_lo, d_hi = wilson_ci(dw, dd)
    c_lo, c_hi = wilson_ci(cw, cd)
    lift = d_rate - c_rate
    lift_ci = (d_lo - c_hi, d_hi - c_lo)

    excludes_zero = lift_ci[0] > 0.0
    enough = n_matchups >= _MIN_SHIP_MATCHUPS
    ships = excludes_zero and enough
    if ships:
        verdict, reason = 'ships-driven', ''
    else:
        verdict = 'thin-inconclusive'
        bits = []
        if not excludes_zero:
            # Distinguish "straddles 0" (truly inconclusive) from "entirely below 0"
            # (driver significantly WORSE) — reporting the latter as "includes 0" misleads.
            if lift_ci[1] < 0.0:
                bits.append('lift 95% CI entirely below 0 (driver significantly WORSE)')
            else:
                bits.append('lift 95% CI includes 0')
        if not enough:
            bits.append(f'n={n_matchups} < {_MIN_SHIP_MATCHUPS} matchups')
        reason = '; '.join(bits)
    return BucketStat(
        bucket=bucket,
        n_decks=len(members),
        n_matchups=n_matchups,
        driver_wins=dw,
        driver_decided=dd,
        cp7_wins=cw,
        cp7_decided=cd,
        winrate_driver=d_rate,
        winrate_cp7=c_rate,
        pooled_lift=lift,
        lift_ci=lift_ci,
        ships=ships,
        verdict=verdict,
        reason=reason,
    )


def aggregate_buckets(deltas: Iterable[DeckDelta]) -> dict[str, BucketStat]:
    """Group ``deltas`` into the rule-8 buckets and pool each into a :class:`BucketStat`.

    A deck contributes to every bucket :func:`_buckets_of` returns (all-driven + keep +
    archetype). A bucket with no members is omitted. Returns ``{bucket: BucketStat}`` in
    :data:`BUCKETS` order.
    """
    members: dict[str, list[DeckDelta]] = {b: [] for b in BUCKETS}
    for d in deltas:
        for b in _buckets_of(d):
            members[b].append(d)
    return {b: _bucket_stat(b, members[b]) for b in BUCKETS if members[b]}


# --------------------------------------------------------------------------- #
# Extract DeckDeltas from a compared run-ledger + the run-set keep tags.
# --------------------------------------------------------------------------- #


def _keep_tags(run_set_json: dict[str, Any]) -> dict[str, str]:
    """Map deck_id -> ``tight-keep`` | ``loose-keep`` from the P5 run-set json."""
    out: dict[str, str] = {}
    for deck_id in run_set_json.get('tight_keep', []):
        out[deck_id] = 'tight-keep'
    for deck_id in run_set_json.get('loose_keep', []):
        out.setdefault(deck_id, 'loose-keep')
    return out


def deltas_from_ledger(run_ledger: Ledger, run_set_json: dict[str, Any]) -> list[DeckDelta]:
    """Extract a :class:`DeckDelta` per ``compared`` run-ledger row (pooling per-opponent games)."""
    keep = _keep_tags(run_set_json)
    deltas: list[DeckDelta] = []
    for row in run_ledger.rows():
        if row.get('stage') != 'compared':
            continue
        comp = row.get('comparison') or {}
        gaunt = comp.get('gauntlet') or {}
        per_opp = gaunt.get('per_opponent') or []
        dw = dd = cw = cd = 0
        for o in per_opp:
            drec = str(o.get('driver_record', '0/0')).split('/')
            crec = str(o.get('cp7_record', '0/0')).split('/')
            dw += int(drec[0])
            dd += int(drec[1])
            cw += int(crec[0])
            cd += int(crec[1])
        deck_id = str(row['deck_id'])
        deltas.append(
            DeckDelta(
                deck_id=deck_id,
                keep=keep.get(deck_id, ''),
                archetype=str(row.get('archetype') or ''),
                driver_wins=dw,
                driver_decided=dd,
                cp7_wins=cw,
                cp7_decided=cd,
                n_matchups=len(per_opp),
            )
        )
    return deltas


# --------------------------------------------------------------------------- #
# Bucket-table rendering (JSON + readable markdown).
# --------------------------------------------------------------------------- #


def render_bucket_markdown(stats: dict[str, BucketStat], *, field_names: Sequence[str] | None = None) -> str:
    """A readable markdown bucket table (headline = tight-keep) for the plan dir."""
    lines = ['# Driver corpus bucket table (rule 8)', '']
    if field_names:
        lines += [f'Opponent field ({len(field_names)}): {", ".join(field_names)}', '']
    headline = stats.get('tight-keep')
    if headline:
        lines += [
            f'**Headline (tight-keep):** {headline.verdict} — pooled lift '
            f'{headline.pooled_lift:+.3f} (95% CI {headline.lift_ci[0]:+.3f}..{headline.lift_ci[1]:+.3f}), '
            f'n={headline.n_matchups} matchups.',
            '',
        ]
    lines += [
        '| bucket | decks | matchups | driver wr | cp7 wr | lift | 95% CI | verdict |',
        '| --- | --- | --- | --- | --- | --- | --- | --- |',
    ]
    for b in BUCKETS:
        s = stats.get(b)
        if s is None:
            continue
        lines.append(
            f'| {s.bucket} | {s.n_decks} | {s.n_matchups} | {s.winrate_driver:.3f} | {s.winrate_cp7:.3f} '
            f'| {s.pooled_lift:+.3f} | {s.lift_ci[0]:+.3f}..{s.lift_ci[1]:+.3f} | {s.verdict} |'
        )
    return '\n'.join(lines) + '\n'


def _write_json(path: Path, payload: Any) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.buckets.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_bucket_table(
    stats: dict[str, BucketStat],
    *,
    out_dir: str | os.PathLike[str],
    field_names: Sequence[str] | None = None,
) -> tuple[Path, Path]:
    """Write ``buckets.json`` + ``buckets.md`` into ``out_dir``; return both paths."""
    out = Path(out_dir)
    json_path = out / 'driver-run-buckets.json'
    md_path = out / 'driver-run-buckets.md'
    _write_json(
        json_path,
        {'field': list(field_names or []), 'buckets': [stats[b].as_dict() for b in BUCKETS if b in stats]},
    )
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_bucket_markdown(stats, field_names=field_names), encoding='utf-8')
    return json_path, md_path


# --------------------------------------------------------------------------- #
# CLI entry point (how the orchestrator launches the FULL monitored run).
# --------------------------------------------------------------------------- #


def _load_run_set(path: str | os.PathLike[str] | None) -> tuple[list[str], dict[str, Any]]:
    p = Path(path) if path else _RUN_SET_PATH
    data = json.loads(p.read_text(encoding='utf-8'))
    return list(data.get('p5_driven_set', [])), data


def run(argv: list[str] | None = None) -> None:
    """CLI: ``python -m pipeline.sim.driver_run --run`` — the full monitored corpus run + aggregate.

    Restartable (re-run skips ``compared`` decks). ``--aggregate-only`` skips the run and just
    (re)builds the bucket table from the existing run ledger.
    """
    import argparse

    parser = argparse.ArgumentParser(prog='driver-run', description='Corpus driver-vs-CP7 run + bucket aggregation.')
    parser.add_argument('--run', action='store_true', help='Execute the gate->compare corpus run.')
    parser.add_argument('--aggregate-only', action='store_true', help='Skip the run; rebuild the bucket table only.')
    parser.add_argument('--games', type=int, default=20, help='Games per gate + per matchup (>=20 for the gate).')
    parser.add_argument('--run-set', default=None, help='Run-set JSON path (default: the plan-dir p5-run-set.json).')
    parser.add_argument('--run-ledger', default=None, help='Run ledger path (default: <data_dir>/sim/driver_run/...).')
    parser.add_argument('--batch-ledger', default=None, help='P3 author/compile ledger (default: <data_dir>/...).')
    parser.add_argument('--out-dir', default=str(_PLAN_DIR), help='Where the bucket table (json+md) is written.')
    parser.add_argument('--no-monitor', action='store_true', help='Do not start the resource monitor (tests/dev).')
    parser.add_argument('--limit', type=int, default=None, help='Run only the first N run-set decks (reduced run).')
    parser.add_argument('--compare-games', type=int, default=None, help='Games/matchup in compare (default --games).')
    parser.add_argument('--field-size', type=int, default=None, help='Cap the opponent field to the first K decks.')
    parser.add_argument(
        '--skip-gate',
        action='store_true',
        help='Bypass the per-deck solo gate (force-stamp a valid meta); reduced-run scope call, not a quality claim.',
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    run_set, run_set_json = _load_run_set(args.run_set)
    if args.limit is not None:
        run_set = run_set[: args.limit]
    run_ledger_path = Path(args.run_ledger) if args.run_ledger else default_run_ledger_path()

    field_names: list[str] = []
    if args.run and not args.aggregate_only:
        from pipeline.sim.engine import get_engine
        from pipeline.sim.monitor import ResourceMonitor

        engine = get_engine('xmage')
        install = engine.resolve(provision=False)
        monitor = None if args.no_monitor else ResourceMonitor()
        summary = run_corpus(
            run_set=run_set,
            install=install,
            games=args.games,
            batch_ledger_path=args.batch_ledger,
            run_ledger_path=run_ledger_path,
            engine=engine,
            monitor=monitor,
            compare_games=args.compare_games,
            skip_gate=args.skip_gate,
            field_size=args.field_size,
        )
        field_names = summary['field']
        log.info('run complete: %s', summary['counts'])

    run_ledger = Ledger(run_ledger_path)
    deltas = deltas_from_ledger(run_ledger, run_set_json)
    stats = aggregate_buckets(deltas)
    json_path, md_path = write_bucket_table(stats, out_dir=args.out_dir, field_names=field_names)
    print(render_bucket_markdown(stats, field_names=field_names))
    print(f'buckets: {json_path}')
    print(f'buckets: {md_path}')


def main(argv: list[str] | None = None) -> None:
    run(argv)


if __name__ == '__main__':
    main()
