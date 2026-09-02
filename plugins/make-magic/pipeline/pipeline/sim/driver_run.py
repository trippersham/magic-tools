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
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.sim.core import wilson_ci
from pipeline.sim.driver_batch import (
    Ledger,
    _driver_uuid,
    _parse_dck,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from pipeline.contracts.models import Deck
    from pipeline.sim.gauntlet import GauntletDeck
    from pipeline.sim.monitor import ResourceMonitor

log = logging.getLogger('make_magic.sim.driver_run')

__all__ = (
    'BucketStat',
    'DeckDelta',
    'aggregate_buckets',
    'build_opponent_field',
    'run_corpus_queue',
    'xmage_deck_loads',
)

#: The deterministic opponent-field strata: (bundle-dir, count). Order is load-bearing (the
#: single ``random.Random(42)`` is consumed strata-in-order for a reproducible field).
_FIELD_STRATA: tuple[tuple[str, int], ...] = (('cedh', 2), ('mid', 3), ('casual', 2), ('precons', 1))
_COMMANDER = 'commander'

#: The bundle order a redistributed (unfillable) stratum slot is back-filled from. Order is
#: load-bearing (deterministic): a slot the ``precons`` stratum can't fill with a LOADABLE deck
#: is redistributed here (casual first, then the mid/cedh pools), never left as a 0-card seat.
_FALLBACK_BUNDLES: tuple[str, ...] = ('casual', 'mid', 'cedh')

#: An opponent deck must translate to at least this many loadable cards to be seated. A commander
#: deck is ~99 cards; a deck that translates to fewer is broken (empty/garbage/format-unhandled)
#: and would seat a degenerate 0-/few-card opponent — the A4 precon blocker. Used by the JVM-free
#: default loadability check :func:`_structural_card_count`.
_MIN_LOADABLE_CARDS = 60

_PLAN_DIR = Path(
    os.path.expanduser(
        '~/.claude/plans/trippersham/magic-tools/2026-08-24-productionize-driver-authoring'
    )
)


def default_batch_ledger_path(data_dir: str | os.PathLike[str] | None = None) -> Path:
    """The P3 author/compile ledger the run reads compiled-driver rows from (legacy 32-driver)."""
    from pipeline.sim.driver_batch import default_ledger_path

    return default_ledger_path(data_dir)


def default_batch_ledger_v2_path(data_dir: str | os.PathLike[str] | None = None) -> Path:
    """The v2 batch ledger: ``<data_dir>/sim/driver_batch_v2/ledger.jsonl``.

    The 47-DRIVER roster (widened win-predicate + light nudge), each row tagged with a
    ``p_tightness`` of ``tight`` | ``loose``. This is the DEFAULT the corpus run reads —
    NOT the legacy 32-driver :func:`default_batch_ledger_path`."""
    from pipeline import store

    root = Path(data_dir) if data_dir is not None else store.StorePaths.resolve().data_dir
    return root / 'sim' / 'driver_batch_v2' / 'ledger.jsonl'


def run_set_from_batch_ledger(batch_ledger: Ledger) -> tuple[list[str], dict[str, str]]:
    """The DRIVE run-set + ``deck_id -> p_tightness`` map, read straight off a batch ledger.

    The run-set is every ``drive`` row at/after ``compiled`` (a thin/uncompiled row has no
    driver to run), in ledger order. The tightness map carries each row's ``p_tightness``
    (``tight`` | ``loose``) for the tight-P/loose-P bucketing — a row missing the tag maps
    to ``''`` (untagged, contributes only to the archetype buckets)."""
    run_set: list[str] = []
    tightness: dict[str, str] = {}
    for row in batch_ledger.rows():
        if not row.get('drive') or row.get('stage') not in ('compiled', 'gated'):
            continue
        deck_id = str(row['deck_id'])
        run_set.append(deck_id)
        tightness[deck_id] = str(row.get('p_tightness') or '')
    return run_set, tightness


# --------------------------------------------------------------------------- #
# The deterministic opponent field.
# --------------------------------------------------------------------------- #


def _structural_card_count(g: GauntletDeck) -> int:
    """The number of loadable card lines ``g`` translates to (JVM-free loadability proxy).

    Runs the SAME Forge->XMage translation the worker stages
    (:func:`~pipeline.sim.engines.xmage._forge_dck_to_xmage_txt`) and counts the non-empty
    ``N Name`` lines. A deck that translates to 0 (empty/garbage/format-unhandled) is exactly the
    A4 precon blocker — a 0-card opponent that produces degenerate games. This is deterministic
    and offline; it CANNOT see a well-formed name that is simply absent from the base card DB (an
    unreleased-set card), which is why :func:`build_opponent_field` accepts an injectable ``loads``
    so the live corpus run can pass the stronger real-DB check (:func:`xmage_deck_loads`).
    """
    from pipeline.sim.engines import xmage as xe

    return sum(1 for line in xe._forge_dck_to_xmage_txt(g.dck_text).splitlines() if line.strip())


def _default_loads(g: GauntletDeck) -> bool:
    """The default loadability + integrity predicate (JVM-free, deterministic).

    A commander opponent is seated only if it translates to a LEGAL commander size — exactly 100
    cards total with a 1-2 card command zone (:func:`~pipeline.sim.engines.xmage.commander_deck_ok`)
    - tightening the historical non-zero card-count check (the A4 precon blocker) so a short-staged
    opponent (e.g. the 95-card Kenrith / 98-card K'rrik gauntlet sources) is EXCLUDED the same way a
    0-card opponent is, rather than silently seating a thinner library.
    """
    from pipeline.sim.engines import xmage as xe

    return xe.commander_deck_ok(xe._forge_dck_to_xmage_txt(g.dck_text))


def build_opponent_field(
    drive_deck_ids: Iterable[str],
    *,
    seed: int = 42,
    loads: Callable[[GauntletDeck], bool] | None = None,
) -> list[GauntletDeck]:
    """Resolve the deterministic 8-deck stratified commander opponent field.

    Samples 2 cedh + 3 mid + 2 casual + 1 precon from the packaged commander bundles
    (:func:`pipeline.sim.gauntlet._bundle`) with a single ``random.Random(seed)`` consumed
    strata-in-order over each bundle's SORTED-by-name candidate list. Any deck whose
    bundle-relative ``deck_id`` (``commander/<bundle>/<stem>.dck``) is in ``drive_deck_ids``
    is EXCLUDED first (no subject faces itself). The returned list is stable across runs for
    a fixed ``seed`` + drive set.

    **Loadability guard (the A4 precon blocker).** A candidate is seated only if ``loads(g)`` is
    True. ``loads`` defaults to :func:`_default_loads` (a JVM-free structural card-count check that
    guarantees no 0-card seat); the live corpus run injects the stronger real base-card-DB check
    (:func:`xmage_deck_loads`) so a deck referencing an UNRELEASED-set card absent from the base DB
    (which loads 0 cards / aborts the game at seat time) is dropped too. A stratum that cannot fill
    its ``k`` slots with LOADABLE decks contributes what it can; the shortfall is REDISTRIBUTED,
    deterministically, from the :data:`_FALLBACK_BUNDLES` pool (loadable, not already seated). If
    the whole field still cannot reach 8 loadable opponents the builder RAISES — it never seats a
    0-card opponent, and it never silently ships a short field.
    """
    from pipeline.sim.gauntlet import _bundle

    loads = loads or _default_loads
    drive = set(drive_deck_ids)
    rng = random.Random(seed)

    def _eligible(bundle: str) -> list[GauntletDeck]:
        candidates = sorted(_bundle(_COMMANDER, bundle), key=lambda g: g.name)
        return [
            g
            for g in candidates
            if f'{_COMMANDER}/{bundle}/{g.name}.dck' not in drive and loads(g)
        ]

    field: list[GauntletDeck] = []
    seated: set[str] = set()
    shortfall = 0
    for bundle, k in _FIELD_STRATA:
        eligible = _eligible(bundle)
        take = min(k, len(eligible))
        chosen = rng.sample(eligible, take) if take else []
        field.extend(chosen)
        seated.update(g.name for g in chosen)
        shortfall += k - take
        if take < k:
            log.warning(
                'opponent-field stratum %r filled %d/%d loadable decks — redistributing %d slot(s)',
                bundle, take, k, k - take,
            )

    if shortfall:
        pool: list[GauntletDeck] = []
        pool_names: set[str] = set()
        for bundle in _FALLBACK_BUNDLES:
            for g in _eligible(bundle):
                if g.name in seated or g.name in pool_names:
                    continue
                pool_names.add(g.name)
                pool.append(g)
        if len(pool) < shortfall:
            raise ValueError(
                f'opponent field cannot be built: {shortfall} slot(s) unfilled and only '
                f'{len(pool)} loadable fallback deck(s) available (all others are unloadable or '
                'already seated). Refusing to seat a 0-card opponent.'
            )
        field.extend(rng.sample(pool, shortfall))

    # Defense-in-depth: NO seated opponent may fail the loadability guard.
    assert all(loads(g) for g in field), 'build_opponent_field seated an unloadable deck'
    return field


#: The command-zone passer PlayerB the load-check seats opposite the candidate: a legal 1v1 EDH
#: deck of base-set cards, so any "deck too small" / "Card not found" the run reports is the
#: CANDIDATE's fault, never PlayerB's.
_LOADCHECK_PASSER_TXT = '99 Forest\nSB: 1 Yargle, Glutton of Urborg\n'


def xmage_deck_loads(
    g: GauntletDeck,
    *,
    install: Any = None,
    data_dir: str | os.PathLike[str] | None = None,
    boot_timeout_s: float = 300.0,
    seat_grace_s: float = 12.0,
) -> bool:
    """True iff ``g`` SEATS against the REAL base XMage card DB with no missing card.

    The stronger loadability oracle :func:`build_opponent_field` accepts as ``loads`` for the live
    corpus run: it stages ``g`` through the SAME translation the worker uses
    (:func:`~pipeline.sim.engines.xmage._forge_dck_to_xmage_txt`), gives the JVM a PRIVATE
    copy-on-write card-DB clone (the H2 parallel-init discipline), and launches one ``XMageBatch``
    commander game against a base-set passer. A candidate that references a card ABSENT from the
    base DB (an UNRELEASED-set precon card — the residual A4 blocker the JVM-free structural check
    cannot see) makes XMage throw ``deck too small (0 cards)`` / ``Card not found`` SYNCHRONOUSLY at
    seat time, right after the ``card db ready`` banner; a loadable deck instead proceeds into the
    (long) game. So the check STREAMS the JVM's output and decides the moment seating resolves —
    a load error -> ``False``; ``seat_grace_s`` elapsing past the ready banner with no error ->
    ``True`` (seating succeeded) — then KILLS the JVM without playing the game out. Requires a
    resolvable XMage install + runnable JRE + built card DB; raises
    :class:`~pipeline.sim.xmage_runtime.XMageUnavailableError` when they are absent so the caller
    can decide (the live run degrades to the structural default).
    """
    import queue
    import shutil
    import subprocess
    import threading
    import time

    from pipeline.sim import runner, xmage_runtime
    from pipeline.sim.engines import xmage as xe

    if install is None:
        install = xmage_runtime.resolve(data_dir=data_dir)

    staging = runner.staging_root()
    staging.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix='xmage-loadcheck-', dir=staging))
    proc: subprocess.Popen[str] | None = None
    try:
        xe._stage_private_db(install, run_dir)
        cand = xe._stage_txt(run_dir, 'cand', xe._forge_dck_to_xmage_txt(g.dck_text))
        passer = xe._stage_txt(run_dir, 'passer', _LOADCHECK_PASSER_TXT)
        cmd = xe._compose_launch_cmd(
            install, [str(cand), str(passer), '1', '7', 'commander'], heap='3g'
        )
        proc = subprocess.Popen(
            cmd, cwd=run_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        assert proc.stdout is not None
        # A reader thread drains stdout into a queue so a SILENT JVM (a loadable deck mid-game
        # emits nothing after the ready banner) can't wedge the grace/boot deadlines.
        lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=_drain, args=(proc.stdout, lines), daemon=True).start()

        boot_deadline = time.monotonic() + boot_timeout_s
        seat_deadline: float | None = None
        while True:
            if seat_deadline is not None and time.monotonic() >= seat_deadline:
                return True  # ready banner + no load error within grace -> seated OK.
            if time.monotonic() >= boot_deadline:
                raise xe.XMageError(
                    f'load-check for {g.name} never seated within {boot_timeout_s:.0f}s'
                )
            try:
                line = lines.get(timeout=1.0)
            except queue.Empty:
                continue
            if line is None:
                # Stream closed (process exited): seated iff the DB was ready and no error was seen
                # (a clean short game on a loadable deck can end this way).
                return seat_deadline is not None
            if 'deck too small' in line or 'Card not found' in line:
                log.info('load-check REJECT %s: %s', g.name, line.strip())
                return False
            if seat_deadline is None and 'card db ready' in line.lower():
                # Seating throws synchronously right after this banner; give it a brief grace
                # window to surface a load error before we call the deck seated.
                seat_deadline = time.monotonic() + seat_grace_s
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        shutil.rmtree(run_dir, ignore_errors=True)


def _drain(stream: Any, out: Any) -> None:
    """Feed each line of ``stream`` into the queue ``out``; enqueue ``None`` at EOF."""
    try:
        for line in stream:
            out.put(line)
    finally:
        out.put(None)


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
# Run-set resolution.
# --------------------------------------------------------------------------- #


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


def _flat_deck_basename(prefix: str, ident: str) -> str:
    """A ``/``- and ``|``-free ``.txt`` basename for a staged deck.

    ``ident`` (a deck_id like ``commander/casual/x.dck`` or an opponent name) is sanitized so the
    derived ``deck_path`` carries no path/id separator — the task_id stays a valid filename for
    ``<logdir>/<task_id>.log`` (BUG-2's Python-side guarantee) and never trips
    :func:`~pipeline.sim.game_tasks._ident`'s ``|`` check. ``prefix`` (``s``/``o``) keeps a subject
    and an opponent that sanitize to the same string distinct."""
    import re

    safe = re.sub(r'[^A-Za-z0-9._-]', '_', ident)
    return f'{prefix}_{safe}.txt'


def build_corpus_game_tasks(
    rows: Sequence[dict[str, Any]],
    field: Sequence[GauntletDeck],
    *,
    games: int,
    stage_dir: str | os.PathLike[str],
    data_dir: str | os.PathLike[str] | None = None,
):
    """Phase-3 opt-in: expand the compiled DRIVE ``rows`` x ``field`` into a flat
    :class:`~pipeline.sim.game_tasks.GameTask` list for the persistent-worker queue.

    Each subject seat carries its compiled driver ``(classes_dir, fqcn)`` (dual-driver
    methodology); the opponent seats are the plain gauntlet decks (``driver=None`` — thin).
    :func:`~pipeline.sim.game_tasks.build_game_tasks` enumerates BOTH pilotings per cell, so the
    baseline arm is structurally guaranteed (the cp7 0/0 fix).

    **Deck translation (BUG-1).** The P2 worker loads ``seat.deck`` as an XMage ``.txt`` deck file
    it finds by bare basename in its cwd (linked there from ``--decks-dir``). A raw Forge ``.dck``
    loads as 0 cards and a bare opponent NAME is not a path. So this builder STAGES every subject
    and opponent deck as a translated XMage ``.txt`` (via
    :func:`~pipeline.sim.engines.xmage._forge_dck_to_xmage_txt`) into ``stage_dir`` under a
    flat ``/``-free basename, and puts that basename in ``deck_path``. The caller points the
    workers at ``stage_dir`` via ``resolve_worker_cmd(decks_dir=...)``. Runs NO games, needs no
    JVM. Returns the task list; the staged files live in ``stage_dir``."""
    from pipeline.sim import drivers
    from pipeline.sim.engines import xmage as xe
    from pipeline.sim.game_tasks import DriverRef, SeatSpec, build_game_tasks

    stage = Path(stage_dir)
    stage.mkdir(parents=True, exist_ok=True)

    subjects: list[SeatSpec] = []
    for row in rows:
        deck, _deck_ref = deck_and_ref_for_row(row)
        deck_id = str(row['deck_id'])
        name = _flat_deck_basename('s', deck_id)
        dck_text = _gauntlet_dck_path(deck_id).read_text(encoding='utf-8')
        xtxt = xe._forge_dck_to_xmage_txt(dck_text)
        # Fail LOUD before any JVM: a wrong-size SUBJECT must never silently seat into a real game.
        main, commander = xe.validate_commander_deck(xtxt, deck_name=deck_id)
        (stage / name).write_text(xe.audit_header(main, commander) + xtxt, encoding='utf-8')
        driver = DriverRef(classpath=str(drivers.classes_dir(deck, data_dir=data_dir)), fqcn=str(row['fqcn']))
        subjects.append(SeatSpec(deck_path=name, driver=driver))

    opponents: list[SeatSpec] = []
    for g in field:
        name = _flat_deck_basename('o', g.name)
        xtxt = xe._forge_dck_to_xmage_txt(g.dck_text)
        # The field builder already EXCLUDED undersized opponents; re-validate as defense-in-depth
        # (an undersized opponent that reached here is a builder bug, not a silent thin seat).
        main, commander = xe.validate_commander_deck(xtxt, deck_name=g.name)
        (stage / name).write_text(xe.audit_header(main, commander) + xtxt, encoding='utf-8')
        opponents.append(SeatSpec(deck_path=name, driver=None))

    return build_game_tasks(subjects, opponents, games, fmt=_COMMANDER)


def resolve_worker_cmd(
    *,
    max_games: int = 500,
    log_dir: str | os.PathLike[str] | None = None,
    data_dir: str | os.PathLike[str] | None = None,
    decks_dir: str | os.PathLike[str] | None = None,
) -> list[str]:
    """The persistent-worker launch command — the COW-staging bootstrap around ``XMageBatch
    --worker`` (Phase 2).

    Returns the argv the :class:`~pipeline.sim.worker_pool.WorkerPool` spawns for each worker:
    :mod:`pipeline.sim.game_worker` run as ``python -m``. That shim mints a PRIVATE copy-on-write
    card-DB per worker (reusing :func:`~pipeline.sim.engines.xmage._stage_private_db` /
    ``_clone_tree_cow`` under :func:`~pipeline.sim.runner.staging_root`), ``chdir``\\s in, and
    ``exec``\\s the real JVM in place (so the pool's stdin/stdout wire-protocol pipes flow straight
    to the JVM). The staged JVM argv itself is
    :func:`pipeline.sim.game_worker.build_worker_java_argv` — ``[java, -Xmx3g, …headless JVM
    args…, -cp, <harness_jar>:<dist_jar>, org.makemagic.xmage.XMageBatch, --worker,
    --worker-max-games, N]`` — reusing :func:`~pipeline.sim.engines.xmage._compose_launch_cmd`.

    **COW/logs staging choice: ONCE PER WORKER, self-staged.** ``run_games`` / ``WorkerPool``
    spawn every worker from ONE shared argv with no per-worker ``cwd`` and do not thread
    ``env_for_worker``, so the governor cannot "stage a dir per worker and pass it" without
    touching the P1/P3 core. So each worker self-stages at startup: one COW clone per worker
    (not per game — the worker is long-lived across ``max_games`` games). ``log_dir`` (absolute)
    points every worker's transcripts at ONE shared dir so the governor can ingest by ``task_id``
    afterward; ``None`` uses each worker's private ``<staging>/logs``.

    The canonical warm db must already exist (a single ``XMageBatch --warm`` before the pool —
    the cold ``CardScanner.scan`` is not concurrency-safe)."""
    cmd = [sys.executable, '-m', 'pipeline.sim.game_worker', '--worker-max-games', str(max_games)]
    if log_dir is not None:
        cmd += ['--log-dir', str(Path(log_dir).resolve())]
    if data_dir is not None:
        cmd += ['--data-dir', str(data_dir)]
    if decks_dir is not None:
        cmd += ['--decks-dir', str(Path(decks_dir).resolve())]
    return cmd


def ingest_queue_transcripts(
    result: Any,
    *,
    data_dir: str | os.PathLike[str] | None = None,
    policy: str = 'all',
    cleanup: bool = False,
) -> int:
    """Ingest every drained game's transcript into ``sim_game_features`` / ``sim_game_logs``.

    The simd engine (:func:`~pipeline.sim.simd.engine.run_games_simd`) tallies coverage but does
    NOT ingest transcripts — this thin glue closes that gap. For each completed ``task_id`` in
    ``result.results`` it splits the id into the ``(subject|opponent|piloting)`` cell key +
    the integer game index, and calls :func:`~pipeline.sim.transcript_parser.ingest_transcript`
    on the RESULT's ``log_path``. Best-effort per row (a missing/bad log is skipped, never
    fatal). Returns the number of rows written. ``cleanup=False`` keeps the transcript files
    (the e2e re-reads them); a production run may pass ``True`` to reclaim disk."""
    from pipeline.sim.transcript_parser import ingest_transcript

    written = 0
    for task_id, res in result.results.items():
        log_path = getattr(res, 'log_path', None)
        if not log_path:
            continue
        parts = str(task_id).split('|')
        if len(parts) != 4:
            log.warning('queue ingest: malformed task_id %r — skipped', task_id)
            continue
        cell = '|'.join(parts[:3])
        try:
            game_index = int(parts[3])
        except ValueError:
            log.warning('queue ingest: non-integer game index in %r — skipped', task_id)
            continue
        if ingest_transcript(data_dir, cell, game_index, log_path, policy=policy, cleanup=cleanup):
            written += 1
    return written


def default_ops_db_path(data_dir: str | os.PathLike[str] | None = None) -> Path:
    """The default per-run ``ops.duckdb`` operational store path (simd's crash-safe done-set)."""
    from pipeline import store

    root = Path(data_dir) if data_dir is not None else store.StorePaths.resolve().data_dir
    return root / 'sim' / 'driver_run_queue' / 'ops.duckdb'


def run_corpus_queue(
    *,
    rows: Sequence[dict[str, Any]],
    field: Sequence[GauntletDeck],
    games: int,
    stall_timeout_s: float = 900.0,
    monitor: ResourceMonitor | None = None,
    ops_db_path: str | os.PathLike[str] | None = None,
    worker_cmd: Sequence[str] | None = None,
    data_dir: str | os.PathLike[str] | None = None,
    preflight: Any = None,
    disk_governor: Any = None,
    breaker: Any = None,
    boot_deadline_s: float | None = None,
    own_pgroup: bool = True,
) -> Any:
    """The LIVE corpus run over the simd engine (the ONE owner after the A3 cutover).

    Builds the dual-driver :class:`GameTask` list (:func:`build_corpus_game_tasks`) and drives
    :func:`~pipeline.sim.simd.engine.run_games_simd` — fair round-robin scheduling, per-cell
    attempt-cap + persistent quarantine, and a crash-safe ``ops.duckdb`` operational store that
    RESUMES a torn run with zero completed-game loss (the JSONL done-set is retired). ``worker_cmd``
    defaults to :func:`resolve_worker_cmd` (the Phase-2 JVM); tests inject a fake.

    Production carry-forwards armed here: the A1 plausibility gate (``bailout_floor_ms =
    BAILOUT_HARD_FLOOR_MS`` — sub-2s "wins" are engine bailouts, non-decisive, surfaced in
    coverage), the ``flock`` singleton + orphan-reaping pidfile (a 2nd launch refuses; a crashed
    predecessor's JVM tree is reaped first), and ``own_pgroup=True`` so a future reaper can killpg
    this run's whole tree. ``preflight`` / ``disk_governor`` / ``breaker`` / ``boot_deadline_s``
    are the A2b resilience hooks (opt-in; the production CLI wires the Java preflight + breaker)."""
    from pipeline.sim import runner
    from pipeline.sim.aggregate import BAILOUT_HARD_FLOOR_MS
    from pipeline.sim.simd.engine import run_games_simd

    # Stage translated decks + shared transcript dir under the staging root (swept by
    # reap_stale_staging). The workers load decks by bare basename from decks_dir and write
    # transcripts into log_dir so the ingest-by-task_id pass can find them afterward.
    staging = runner.staging_root()
    staging.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix='corpus-decks-', dir=staging))
    log_dir = Path(tempfile.mkdtemp(prefix='corpus-logs-', dir=staging))

    tasks = build_corpus_game_tasks(rows, field, games=games, stage_dir=stage_dir, data_dir=data_dir)
    cmd = (
        list(worker_cmd)
        if worker_cmd is not None
        else resolve_worker_cmd(log_dir=log_dir, data_dir=data_dir, decks_dir=stage_dir)
    )
    ops_db = Path(ops_db_path) if ops_db_path is not None else default_ops_db_path(data_dir)
    lock_path = ops_db.parent / 'simd.lock'
    pidfile = ops_db.parent / 'simd.pid'

    result = run_games_simd(
        tasks,
        worker_cmd=cmd,
        ops_db_path=ops_db,
        stall_timeout_s=stall_timeout_s,
        monitor=monitor,
        # Arm the plausibility gate on the production path: sub-2s "wins" are engine bailouts
        # (audit) → non-decisive, excluded from W/L, surfaced in coverage.
        bailout_floor_ms=BAILOUT_HARD_FLOOR_MS,
        preflight=preflight,
        disk_governor=disk_governor,
        breaker=breaker,
        boot_deadline_s=boot_deadline_s,
        # Singleton + orphan reaping + own process group — the production isolation carry-forwards.
        singleton_lock_path=lock_path,
        pidfile_path=pidfile,
        own_pgroup=own_pgroup,
    )
    # Ingest each drained game's transcript into sim_game_features / sim_game_logs (best-effort).
    try:
        ingest_queue_transcripts(result, data_dir=data_dir)
    except Exception:
        log.warning('queue transcript ingestion failed (non-fatal)', exc_info=True)
    return result


# --------------------------------------------------------------------------- #
# P5.4 — bucket aggregation (rule 8).
# --------------------------------------------------------------------------- #

#: A bucket ships driven only with at least this many matchups behind the pooled lift.
_MIN_SHIP_MATCHUPS = 30
#: The bucket set (headline = tight-P). ``all-driven`` holds every compared deck.
#: ``tight-P`` / ``loose-P`` come from the v2 ledger's ``p_tightness`` tag: tight-P is the
#: honest-P headline; loose-P is reported separately (premature-firing caveat). The legacy
#: ``tight-keep`` / ``loose-keep`` are retained for the old run-set-json path (back-compat).
BUCKETS: tuple[str, ...] = (
    'all-driven',
    'tight-P',
    'loose-P',
    'tight-keep',
    'loose-keep',
    'drive-dedicated',
    'drive-capable',
)


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
    #: The v2 ledger's ``p_tightness`` tag: ``'tight'`` | ``'loose'`` | ``''`` (untagged).
    #: Drives the tight-P / loose-P buckets.
    p_tightness: str = ''


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
    if delta.p_tightness == 'tight':
        tags.append('tight-P')
    elif delta.p_tightness == 'loose':
        tags.append('loose-P')
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
# Bucket-table rendering (JSON + readable markdown).
# --------------------------------------------------------------------------- #


def render_bucket_markdown(stats: dict[str, BucketStat], *, field_names: Sequence[str] | None = None) -> str:
    """A readable markdown bucket table (headline = tight-keep) for the plan dir."""
    lines = ['# Driver corpus bucket table (rule 8)', '']
    if field_names:
        lines += [f'Opponent field ({len(field_names)}): {", ".join(field_names)}', '']
    headline_bucket = 'tight-P' if 'tight-P' in stats else 'tight-keep'
    headline = stats.get(headline_bucket)
    if headline:
        lines += [
            f'**Headline ({headline_bucket}):** {headline.verdict} — pooled lift '
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


def run(argv: list[str] | None = None) -> None:
    """CLI: ``python -m pipeline.sim.driver_run --run`` — the LIVE simd corpus run + aggregate.

    The single owner after the A3 cutover: the run drives :func:`run_corpus_queue` (simd engine —
    fair scheduling, attempt-cap/quarantine, crash-safe ``ops.duckdb`` resume) and folds the
    committed results into the rule-8 bucket table via :func:`~pipeline.sim.aggregate`. Omitting
    ``--run`` re-derives the run-set/tags only (a dry preview — no games).
    """
    import argparse

    parser = argparse.ArgumentParser(prog='driver-run', description='Corpus driver-vs-CP7 simd run + buckets.')
    parser.add_argument(
        '--run',
        action='store_true',
        help='Execute the simd corpus run. Omit to only re-derive the run-set/tags (no games).',
    )
    parser.add_argument('--games', type=int, default=20, help='Games per matchup arm.')
    parser.add_argument(
        '--batch-ledger',
        default=None,
        help='Author/compile ledger (default: the v2 ledger <data_dir>/sim/driver_batch_v2/ledger.jsonl).',
    )
    parser.add_argument('--out-dir', default=str(_PLAN_DIR), help='Where the bucket table (json+md) is written.')
    parser.add_argument('--no-monitor', action='store_true', help='Do not start the resource monitor (tests/dev).')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    batch_ledger_path = Path(args.batch_ledger) if args.batch_ledger else default_batch_ledger_v2_path()
    # Derive the DRIVE run-set + the tight/loose-P tags straight off the v2 batch ledger (47 drivers).
    batch = Ledger(batch_ledger_path)
    run_set, tightness = run_set_from_batch_ledger(batch)
    log.info('v2 run-set: %d DRIVE decks (tight=%d loose=%d) from %s', len(run_set),
             sum(t == 'tight' for t in tightness.values()),
             sum(t == 'loose' for t in tightness.values()), batch_ledger_path)

    if not args.run:
        print(f'run-set: {len(run_set)} DRIVE decks (dry preview — pass --run to execute)')
        return

    from pipeline.sim.aggregate import BAILOUT_HARD_FLOOR_MS, aggregate_results
    from pipeline.sim.monitor import ResourceMonitor
    from pipeline.sim.simd.preflight import preflight_java
    from pipeline.sim.xmage_runtime import _resolve_java

    rows = _drive_rows_for_run_set(batch, run_set)
    drive_ids = [r['deck_id'] for r in batch.rows() if r.get('drive') and r.get('stage') in ('compiled', 'gated')]
    # The live run validates every opponent against the REAL base card DB (so an unreleased-set
    # precon card that seats 0 cards / aborts the game is dropped + the slot redistributed), and
    # degrades to the JVM-free structural default if XMage can't be resolved here.
    from pipeline.sim import xmage_runtime as xr
    from pipeline.sim.gauntlet import _bundle
    from pipeline.sim.xmage_runtime import XMageUnavailableError

    try:
        install = xr.resolve()
        # The curated cedh/mid/casual bundles are known-loadable; only the precon bundle carries
        # unreleased-set risk, so spend the (JVM) base-DB check ONLY there and keep the cheap
        # structural check for the rest — the field-build stays a handful of load-checks, not dozens.
        precon_names = {g.name for g in _bundle(_COMMANDER, 'precons')}

        def field_loads(g: GauntletDeck) -> bool:
            # Structural size/integrity gate first (JVM-free), for EVERY bundle — so an undersized
            # opponent is excluded even on the precon path that spends the real base-DB check.
            if not _default_loads(g):
                return False
            if g.name in precon_names:
                return xmage_deck_loads(g, install=install)
            return True

        loads_fn: Callable[[GauntletDeck], bool] | None = field_loads
    except XMageUnavailableError as exc:
        log.warning('base-DB load-check unavailable (%s) — using the structural loadability default', exc)
        loads_fn = None
    field = build_opponent_field(drive_ids, loads=loads_fn)
    field_names = [g.name for g in field]
    log.info('opponent field (%d): %s', len(field_names), ', '.join(field_names))

    monitor = None if args.no_monitor else ResourceMonitor()
    # Production carry-forwards: Java preflight (fail-loud, version-gated) + crash-loop breaker are
    # wired here; run_corpus_queue arms the bailout gate, flock singleton, orphan reaper, own_pgroup.
    from pipeline.sim.simd.circuit_breaker import CrashLoopBreaker

    # The simd engine calls ``preflight()`` with ZERO args, but ``preflight_java`` needs the
    # ``java`` launcher. Wire a zero-arg closure that resolves the runtime Java path (fail-loud if
    # missing) and version-gates it — F-1: passing the bare ``preflight_java`` TypeError'd at boot.
    result = run_corpus_queue(
        rows=rows,
        field=field,
        games=args.games,
        monitor=monitor,
        preflight=lambda: preflight_java(_resolve_java()),
        breaker=CrashLoopBreaker(),
        boot_deadline_s=120.0,
    )
    log.info('simd run complete=%s quarantined=%d incomplete_cells=%d',
             result.complete, len(result.quarantined), len(result.incomplete_cells))

    # Fold the committed results into per-subject comparisons, remapping the tag maps from deck_id
    # to the staged subject basename the tasks carry.
    subject_of = {_flat_deck_basename('s', str(r['deck_id'])): str(r['deck_id']) for r in rows}
    tight_by_subject = {sub: tightness.get(did, '') for sub, did in subject_of.items()}
    arche_by_subject = {
        _flat_deck_basename('s', str(r['deck_id'])): str(r.get('archetype') or '') for r in rows
    }
    all_task_ids = list(result.results) + [t for t in result.quarantined if t not in result.results]
    agg = aggregate_results(
        all_task_ids, result.results, quarantined=result.quarantined, bailout_floor_ms=BAILOUT_HARD_FLOOR_MS
    )
    comparisons = agg.comparisons()
    stats = aggregate_buckets(_deltas_from_comparisons(comparisons, arche_by_subject, tight_by_subject))
    json_path, md_path = write_bucket_table(stats, out_dir=args.out_dir, field_names=field_names)
    print(render_bucket_markdown(stats, field_names=field_names))
    print(f'buckets: {json_path}')
    print(f'buckets: {md_path}')


def _deltas_from_comparisons(
    comparisons: dict[str, Any],
    archetypes: dict[str, str],
    tightness: dict[str, str],
) -> list[DeckDelta]:
    """Pool each subject's :class:`PilotingComparison` into a :class:`DeckDelta` (bucket input)."""
    deltas: list[DeckDelta] = []
    for subject, comp in comparisons.items():
        dw = dd = cw = cd = 0
        for o in comp.per_opponent:
            dw += o.driver_wins
            dd += o.driver_decided
            cw += o.cp7_wins
            cd += o.cp7_decided
        deltas.append(
            DeckDelta(
                deck_id=subject,
                keep='',
                archetype=archetypes.get(subject, ''),
                driver_wins=dw,
                driver_decided=dd,
                cp7_wins=cw,
                cp7_decided=cd,
                n_matchups=len(comp.per_opponent),
                p_tightness=tightness.get(subject, ''),
            )
        )
    return deltas


def main(argv: list[str] | None = None) -> None:
    run(argv)


if __name__ == '__main__':
    main()
