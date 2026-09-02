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

import datetime as _dt
import json
import logging
import os
import random
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.sim.core import newcombe_diff_ci
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
    'publish_corpus_results',
    'rollup_to_lake',
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
    thin_rows: Sequence[dict[str, Any]] = (),
):
    """Phase-3 opt-in: expand the compiled DRIVE ``rows`` (+ any baseline-only ``thin_rows``) x
    ``field`` into a flat :class:`~pipeline.sim.game_tasks.GameTask` list for the persistent-worker
    queue.

    Each DRIVE subject seat carries its compiled driver ``(classes_dir, fqcn)`` (dual-driver
    methodology); the opponent seats are the plain gauntlet decks (``driver=None`` — thin).
    :func:`~pipeline.sim.game_tasks.build_game_tasks` enumerates BOTH pilotings for a driven subject,
    so the baseline arm is structurally guaranteed (the cp7 0/0 fix).

    **Baseline-only subjects (``thin_rows``).** A row in ``thin_rows`` is a driverless deck — it has
    no compiled driver to run, so its subject seat is bare (``driver=None``) and
    :func:`~pipeline.sim.game_tasks.build_game_tasks` enumerates ONLY its baseline arm (one piloting x
    ``games`` per opponent, task ids ``subject|opponent|baseline|index``). These rows carry no
    ``fqcn`` / class tree; they still stage + size-validate their deck exactly like a DRIVE subject.
    Kept a distinct parameter (not folded into ``rows``) so ``rows`` stays "compiled DRIVE rows".

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

    def _stage_subject_deck(deck_id: str) -> str:
        """Translate + size-validate ``deck_id`` and write its staged ``.txt`` (returns the basename)."""
        name = _flat_deck_basename('s', deck_id)
        dck_text = _gauntlet_dck_path(deck_id).read_text(encoding='utf-8')
        xtxt = xe._forge_dck_to_xmage_txt(dck_text)
        # Fail LOUD before any JVM: a wrong-size SUBJECT must never silently seat into a real game.
        main, commander = xe.validate_commander_deck(xtxt, deck_name=deck_id)
        (stage / name).write_text(xe.audit_header(main, commander) + xtxt, encoding='utf-8')
        return name

    subjects: list[SeatSpec] = []
    for row in rows:
        deck, _deck_ref = deck_and_ref_for_row(row)
        name = _stage_subject_deck(str(row['deck_id']))
        driver = DriverRef(classpath=str(drivers.classes_dir(deck, data_dir=data_dir)), fqcn=str(row['fqcn']))
        subjects.append(SeatSpec(deck_path=name, driver=driver))

    # Baseline-only subjects: stage the deck exactly like a DRIVE subject, but seat it driverless in
    # the SEPARATE single-arm roster so build_game_tasks enumerates ONLY its baseline arm (no phantom
    # driven cell). Kept apart from `subjects` so a two-arm subject's baseline seat is never confused
    # with a genuinely single-arm one.
    thin_subjects: list[SeatSpec] = []
    for row in thin_rows:
        name = _stage_subject_deck(str(row['deck_id']))
        thin_subjects.append(SeatSpec(deck_path=name, driver=None))

    opponents: list[SeatSpec] = []
    for g in field:
        name = _flat_deck_basename('o', g.name)
        xtxt = xe._forge_dck_to_xmage_txt(g.dck_text)
        # The field builder already EXCLUDED undersized opponents; re-validate as defense-in-depth
        # (an undersized opponent that reached here is a builder bug, not a silent thin seat).
        main, commander = xe.validate_commander_deck(xtxt, deck_name=g.name)
        (stage / name).write_text(xe.audit_header(main, commander) + xtxt, encoding='utf-8')
        opponents.append(SeatSpec(deck_path=name, driver=None))

    return build_game_tasks(
        subjects, opponents, games, fmt=_COMMANDER, baseline_only_subjects=thin_subjects
    )


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


#: A top-up replacement game's lake ``game_index`` is offset past any plausible original index so a
#: cell's originals (0..needed) and its top-ups never collide on the ``(matchup_key, game_index)``
#: grain. Top-up ``topup-<n>`` → ``_TOPUP_INDEX_BASE + n``.
_TOPUP_INDEX_BASE = 100_000


def _lake_matchup_key(subject: str, opponent: str, piloting: str) -> str:
    """A STABLE, reproducible lake ``matchup_key`` for a simd cell.

    THE (c) KEYING FIX. The retired ``ingest_queue_transcripts`` wrote ``sim_game_features`` /
    ``sim_game_logs`` rows keyed by the raw cell STRING while creating no ``sim_matchups`` parent
    row — so every lake reader that ``JOIN sim_matchups USING (matchup_key)`` (``feature_stats`` /
    ``find_matchups``) matched NOTHING, and the whole queue corpus never landed in the lake. The
    roll-in now derives a deterministic hashed key from the cell identity AND writes the matching
    ``sim_matchups`` parent, so queue-run games are joinable exactly like a Forge/XMage matchup.
    Deterministic → re-running the roll-in reproduces the same key (idempotent replace)."""
    import hashlib

    payload = '\x00'.join(('simd-cell', subject, opponent, piloting)).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _lake_game_index(raw_index: str) -> int:
    """The lake INTEGER ``game_index`` for an ops ``game_index`` field (original ``<n>`` or
    ``topup-<n>``). A top-up is offset by :data:`_TOPUP_INDEX_BASE` so it never collides with an
    original on the ``(matchup_key, game_index)`` grain; an unparseable field falls back to 0."""
    if raw_index.startswith('topup-'):
        try:
            return _TOPUP_INDEX_BASE + int(raw_index[len('topup-') :])
        except ValueError:
            return _TOPUP_INDEX_BASE
    try:
        return int(raw_index)
    except ValueError:
        return 0


def _winner_ab(winner: str | None) -> str:
    w = (winner or '').strip().lower()
    if w in ('a', 'subject', 'player_a', 'playera'):
        return 'a'
    if w in ('b', 'opponent', 'player_b', 'playerb'):
        return 'b'
    return 'draw'


def rollup_to_lake(
    ops_db_path: str | os.PathLike[str],
    *,
    data_dir: str | os.PathLike[str] | None = None,
    run_id: str | None = None,
) -> int:
    """Roll the per-game durable ops records into the lake — REPRODUCIBLE + idempotent.

    Reads ``simd_game_logs`` / ``simd_game_features`` from the crash-safe ``ops.duckdb`` (NOT the
    filesystem — the transient transcript files are already drained + deleted) and writes correctly
    KEYED rows into the lake's ``sim_matchups`` / ``sim_game_features`` / ``sim_game_logs`` so the
    queue corpus actually lands there (the (c) fix — see :func:`_lake_matchup_key`). One
    ``sim_matchups`` parent row per simd cell (pooled W/L tally over the cell's drained games) makes
    every lake reader's ``JOIN sim_matchups`` resolve.

    Runs at end-of-run from :func:`run_corpus_queue`, AND ships as a standalone re-drain path: call
    it anytime over an ops file to (re)do a crashed run's lake roll-in. IDEMPOTENT — each cell's lake
    rows are fully replaced from the ops state, so a re-run yields no duplicates. ``run_id`` scopes
    the roll-in to one run; ``None`` rolls in every run in the file. Rows flagged
    ``missing_transcript`` contribute their W/L (from the recorded winner) but carry no raw log.
    Returns the number of games rolled in. Brief-exclusive lake writes (one connection, one batch).
    """
    from pathlib import Path as _Path

    from pipeline import store
    from pipeline.sim import store as sim_store
    from pipeline.sim import transcript_parser as tp
    from pipeline.sim.simd.ops_store import OpsStore

    ops_path = _Path(ops_db_path)
    if not ops_path.is_file():
        return 0

    # Which runs to roll in.
    if run_id is not None:
        run_ids = [run_id]
    else:
        import duckdb

        conn = duckdb.connect(str(ops_path), read_only=True)
        try:
            run_ids = [
                r[0] for r in conn.execute('SELECT DISTINCT run_id FROM simd_game_logs').fetchall()
            ]
        finally:
            conn.close()
    if not run_ids:
        return 0

    # Gather every drained game across the requested run(s) from the durable ops tables.
    logs: dict[str, tuple[str, str, str, str, str | None, bool]] = {}
    feats: dict[str, dict[str, object]] = {}
    for rid in run_ids:
        with OpsStore(ops_path, run_id=rid) as ops:
            logs.update(ops.load_game_logs())
            feats.update(ops.load_game_features())
    if not logs:
        return 0

    # Group by cell → the lake matchup_key + parent-row tally.
    from collections import defaultdict

    by_cell: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for tid, (subject, opp, pil, _gidx, _raw, _missing) in logs.items():
        by_cell[(subject, opp, pil)].append(tid)

    written = 0
    db_path = sim_store._db_path(os.fspath(data_dir) if data_dir is not None else None)
    with store.connect(db_path) as conn:
        sim_store._ensure_tables(conn)
        tp._ensure_transcript_columns(conn)
        for (subject, opp, pil), tids in by_cell.items():
            key = _lake_matchup_key(subject, opp, pil)
            # Idempotent replace: clear any prior roll-in of this cell first.
            conn.execute('DELETE FROM sim_game_logs WHERE matchup_key = ?', [key])
            conn.execute('DELETE FROM sim_game_features WHERE matchup_key = ?', [key])
            conn.execute('DELETE FROM sim_matchups WHERE matchup_key = ?', [key])
            wins_a = wins_b = draws = 0
            for tid in tids:
                _s, _o, _p, raw_index, raw_log, _missing = logs[tid]
                gi = _lake_game_index(raw_index)
                f = feats.get(tid, {})
                bucket = _winner_ab(f.get('winner') if f else None)  # type: ignore[arg-type]
                if bucket == 'a':
                    wins_a += 1
                elif bucket == 'b':
                    wins_b += 1
                else:
                    draws += 1
                if raw_log is not None:
                    conn.execute(
                        'INSERT INTO sim_game_logs (matchup_key, game_index, raw_log) VALUES (?, ?, ?)',
                        [key, gi, raw_log],
                    )
                conn.execute(
                    'INSERT INTO sim_game_features '
                    '(matchup_key, game_index, winner, kill_turn, win_margin_life, wincon, '
                    'mulligans_a, mulligans_b, game_length_ms, assembled_turn, fired_turn, '
                    'driver_registered, macro_reachable, steer_fired, storm_count, life_swing, '
                    'disruption_survived, incomplete, timeout) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    [
                        key, gi, f.get('winner'), f.get('kill_turn'), f.get('win_margin_life'),
                        f.get('wincon'), f.get('mulligans_a'), f.get('mulligans_b'),
                        f.get('game_length_ms'), f.get('assembled_turn'), f.get('fired_turn'),
                        f.get('driver_registered'), f.get('macro_reachable'), f.get('steer_fired'),
                        f.get('storm_count'), f.get('life_swing'), f.get('disruption_survived'),
                        f.get('incomplete'), f.get('timeout'),
                    ],
                )
                written += 1
            # The parent row makes the lake JOINs resolve (the (c) fix). Deck hashes carry the staged
            # subject/opponent basenames (the cell's stable identity); engine tags the simd source.
            conn.execute(
                'INSERT INTO sim_matchups '
                '(matchup_key, deck_a_hash, deck_b_hash, seed, n_games, format, engine, '
                'engine_version, wins_a, wins_b, draws, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                [
                    key, subject, opp, 0, len(tids), _COMMANDER, 'xmage', 'simd',
                    wins_a, wins_b, draws, _dt.datetime.now(_dt.UTC),
                ],
            )
    return written


def default_ops_db_path(data_dir: str | os.PathLike[str] | None = None) -> Path:
    """The default per-run ``ops.duckdb`` operational store path (simd's crash-safe done-set)."""
    from pipeline import store

    root = Path(data_dir) if data_dir is not None else store.StorePaths.resolve().data_dir
    return root / 'sim' / 'driver_run_queue' / 'ops.duckdb'


def _task_content_fingerprint(task: Any, stage_dir: Path) -> str:
    """A content-complete fingerprint of ONE staged task: the BYTES of both staged deck files +
    the driver's fqcn + a digest of its compiled class-dir bytes. Unlike
    :func:`~pipeline.sim.simd.ops_store.task_fingerprint` (deck BASENAMES), this binds the actual
    deck content — the decks are staged right here in ``stage_dir``, so a same-name-different-content
    swap yields a different fingerprint (Sol BLOCKER 1)."""
    import hashlib

    from pipeline.sim.simd.ops_store import dir_content_digest

    h = hashlib.sha256()
    h.update(task.fmt.encode('utf-8'))
    for seat in (task.seat_a, task.seat_b):
        h.update(b'\0deck\0')
        deck_file = stage_dir / seat.deck_path
        try:
            h.update(deck_file.read_bytes())
        except OSError:
            h.update(b'MISSING')
        drv = getattr(seat, 'driver', None)
        h.update(b'\0drv\0')
        if drv is not None:
            h.update(drv.fqcn.encode('utf-8'))
            h.update(b'\0')
            h.update(dir_content_digest(drv.classpath).encode('utf-8'))
    return h.hexdigest()


def _content_run_id(
    tasks: Sequence[Any],
    stage_dir: str | os.PathLike[str],
    *,
    games: int,
    attempt_cap: int,
    topup_cap: int,
    bailout_floor_ms: int,
    data_dir: str | os.PathLike[str] | None = None,
) -> str:
    """Derive a CONTENT-COMPLETE run identity from an immutable manifest (Sol BLOCKER 1).

    The run_id is a hash of EVERY input that defines the science: format, games/needed, attempt/
    top-up caps, bailout floor, the harness + dist jar shas (the runtime that produces the results),
    and — the universe itself — the sorted ``{task_id: content_fingerprint}`` map (each fingerprint
    binds the staged deck BYTES + driver class bytes, not names/paths). Same universe + same config
    → same run_id → a resume replays committed state. ANY change — ``--games`` shrunk, a field deck
    added/removed, a deck's or driver's bytes swapped, the jar rebuilt — yields a DIFFERENT run_id,
    so the old run's rows are invisible under the new id and can never mix incompatible science into
    a nominally-complete result. Config drift within a fixed run_id still raises via the manifest."""
    import hashlib

    stage = Path(stage_dir)
    manifest = {
        'fmt': tasks[0].fmt if tasks else _COMMANDER,
        'games': games,
        'attempt_cap': attempt_cap,
        'topup_cap': topup_cap,
        'bailout_floor_ms': bailout_floor_ms,
        'harness_jar': _harness_jar_sha(),
        'dist_jar': _dist_jar_sha(data_dir),
        'tasks': {t.task_id: _task_content_fingerprint(t, stage) for t in tasks},
    }
    blob = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    return 'run-' + hashlib.sha256(blob.encode('utf-8')).hexdigest()[:24]


def _harness_jar_sha() -> str:
    """The committed XMage harness jar's content sha (``'unknown'`` if unresolvable) — a harness
    rebuild busts the run identity so results from a different harness never resume-mix."""
    try:
        from pipeline.sim.engines import xmage as xe

        return xe._harness_jarhash()
    except Exception:
        return 'unknown'


def _dist_jar_sha(data_dir: str | os.PathLike[str] | None) -> str:
    """The cached XMage dist jar's content sha if resolvable, else ``'unknown'`` (never raises) — a
    dist-jar swap busts the run identity."""
    import hashlib

    try:
        from pipeline.sim import xmage_runtime as xr

        dist_jar = xr._dist_dir(data_dir) / xr._DIST_JAR_NAME
        if dist_jar.is_file() and dist_jar.stat().st_size > 0:
            return hashlib.sha256(dist_jar.read_bytes()).hexdigest()[:16]
    except Exception:
        return 'unknown'
    return 'unknown'


def run_corpus_queue(
    *,
    rows: Sequence[dict[str, Any]],
    field: Sequence[GauntletDeck],
    games: int,
    thin_rows: Sequence[dict[str, Any]] = (),
    stall_timeout_s: float = 900.0,
    monitor: ResourceMonitor | None = None,
    ops_db_path: str | os.PathLike[str] | None = None,
    worker_cmd: Sequence[str] | None = None,
    data_dir: str | os.PathLike[str] | None = None,
    preflight: Any = None,
    disk_governor: Any = None,
    breaker: Any = None,
    boot_deadline_s: float | None = None,
    boot_backoff_base_s: float = 0.0,
    own_pgroup: bool = True,
) -> Any:
    """The LIVE corpus run over the simd engine (the ONE owner after the A3 cutover).

    Builds the dual-driver :class:`GameTask` list (:func:`build_corpus_game_tasks`) and drives
    :func:`~pipeline.sim.simd.engine.run_games_simd` — fair round-robin scheduling, per-cell
    attempt-cap + persistent quarantine, and a crash-safe ``ops.duckdb`` operational store that
    RESUMES a torn run with zero completed-game loss (the JSONL done-set is retired). ``worker_cmd``
    defaults to :func:`resolve_worker_cmd` (the Phase-2 JVM); tests inject a fake.

    **Protection BEFORE staging (Sol BLOCKER 3).** The Java/runtime ``preflight`` and the singleton
    ``flock`` are acquired FIRST — before any deck is staged — so a missing-Java launch or a refused
    second launch fails LOUD with ZERO staging artifacts. Only then are the ``corpus-decks-*`` /
    ``corpus-logs-*`` dirs created, inside a ``try/finally`` that sweeps them on BOTH normal and
    error exit (the drained transcripts are ingested into the durable store first, then the
    ephemeral files are removed). :func:`~pipeline.sim.runner.reap_stale_staging` is called at
    startup to reclaim a crashed predecessor's orphaned staging dirs (the ``corpus-`` prefix is now
    in the sweeper's filter — Fable M7).

    Production carry-forwards armed here: the A1 plausibility gate (``bailout_floor_ms =
    BAILOUT_HARD_FLOOR_MS`` — sub-2s "wins" are engine bailouts, non-decisive, surfaced in
    coverage), the singleton ``flock`` (held across the whole run) + orphan-reaping pidfile (a
    crashed predecessor's JVM tree is reaped first), and ``own_pgroup=True`` so a future reaper can
    killpg this run's whole tree. ``preflight`` / ``disk_governor`` / ``breaker`` /
    ``boot_deadline_s`` / ``boot_backoff_base_s`` are the A2b resilience hooks (opt-in; the
    production CLI wires the Java preflight + a default ``DiskGovernor`` + the crash-loop breaker)."""
    import contextlib
    import signal

    from pipeline.sim import runner

    ops_db = Path(ops_db_path) if ops_db_path is not None else default_ops_db_path(data_dir)
    lock_path = ops_db.parent / 'simd.lock'
    pidfile = ops_db.parent / 'simd.pid'
    ops_db.parent.mkdir(parents=True, exist_ok=True)  # the flock + pidfile need the dir to exist.

    # Startup GC: sweep orphaned staging dirs a crashed predecessor left behind, BEFORE we stage
    # (Sol BLOCKER 3 + Fable M7). Best-effort — reaping must never break a run.
    with contextlib.suppress(Exception):
        runner.reap_stale_staging()

    # SIGTERM handler: a resource watchdog / orchestrator kill must not SIGKILL Python without
    # running teardown (the post-mortem's failure (a): a raw TERM skipped ``finally`` → no roll-in
    # + the next run's staging GC reaped the un-drained corpus). Trapping TERM turns it into a
    # normal exception so the ExitStack + the staging ``finally`` DO run, the flock is released, and
    # the run exits nonzero + RESUMABLE. Per-game drain already made every committed game durable in
    # ops.duckdb, so a TERM loses ZERO transcripts. Only installable on the main thread; a library
    # caller on a worker thread degrades to the prior (untrapped) behaviour.
    class _Terminated(BaseException):
        """Raised in-process when SIGTERM is trapped, to unwind through the teardown path."""

    _prev_term: Any = None
    _term_installed = False

    def _on_term(_signum: int, _frame: Any) -> None:
        raise _Terminated

    try:
        _prev_term = signal.signal(signal.SIGTERM, _on_term)
        _term_installed = True
    except (ValueError, OSError):
        _term_installed = False  # not the main thread — leave the default disposition.

    try:
        return _run_corpus_queue_body(
            rows=rows, field=field, games=games, thin_rows=thin_rows, stall_timeout_s=stall_timeout_s,
            monitor=monitor, worker_cmd=worker_cmd, data_dir=data_dir, preflight=preflight,
            disk_governor=disk_governor, breaker=breaker, boot_deadline_s=boot_deadline_s,
            boot_backoff_base_s=boot_backoff_base_s, own_pgroup=own_pgroup, ops_db=ops_db,
            lock_path=lock_path, pidfile=pidfile,
        )
    except _Terminated:
        log.error('SIGTERM received — teardown ran (staging swept, flock released); every committed '
                  'game is durable in ops.duckdb. Exiting nonzero + RESUMABLE.')
        raise SystemExit(1) from None
    finally:
        if _term_installed:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGTERM, _prev_term)


def _run_corpus_queue_body(
    *,
    rows: Sequence[dict[str, Any]],
    field: Sequence[GauntletDeck],
    games: int,
    thin_rows: Sequence[dict[str, Any]],
    stall_timeout_s: float,
    monitor: ResourceMonitor | None,
    worker_cmd: Sequence[str] | None,
    data_dir: str | os.PathLike[str] | None,
    preflight: Any,
    disk_governor: Any,
    breaker: Any,
    boot_deadline_s: float | None,
    boot_backoff_base_s: float,
    own_pgroup: bool,
    ops_db: Path,
    lock_path: Path,
    pidfile: Path,
) -> Any:
    """The staging + engine-drive body of :func:`run_corpus_queue` (extracted so the SIGTERM trap
    wraps it cleanly). See :func:`run_corpus_queue` for the contract."""
    import contextlib
    import shutil

    from pipeline.sim import runner
    from pipeline.sim.aggregate import BAILOUT_HARD_FLOOR_MS
    from pipeline.sim.simd.engine import run_games_simd
    from pipeline.sim.simd.reaper import SingletonLock

    with contextlib.ExitStack() as stack:
        # PROTECTION BEFORE STAGING (Sol BLOCKER 3). The Java/runtime preflight and the singleton
        # flock run FIRST, so a missing-Java launch or a refused second launch fails LOUD with ZERO
        # staging artifacts — the engine used to run these only AFTER this function had already
        # staged corpus-decks-*/corpus-logs-* dirs.
        # 1. PREFLIGHT — fail loud (BootFailure/XMageUnavailable) before any staging/spawn.
        if preflight is not None:
            preflight()
        # 2. SINGLETON flock — a 2nd concurrent simd launch raises AlreadyRunning, zero staging.
        stack.enter_context(SingletonLock(lock_path))

        # 3. NOW stage translated decks + the shared transcript dir under the staging root, wrapped
        # so NORMAL and ERROR exits both clean them (no leaked corpus-* dirs). The pid is encoded
        # in the prefix so mid-run staging GC's liveness check never reaps a live run's dir.
        staging = runner.staging_root()
        staging.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(tempfile.mkdtemp(prefix=f'corpus-decks-{os.getpid()}-', dir=staging))
        log_dir = Path(tempfile.mkdtemp(prefix=f'corpus-logs-{os.getpid()}-', dir=staging))
        try:
            tasks = build_corpus_game_tasks(
                rows, field, games=games, stage_dir=stage_dir, data_dir=data_dir, thin_rows=thin_rows
            )
            cmd = (
                list(worker_cmd)
                if worker_cmd is not None
                else resolve_worker_cmd(log_dir=log_dir, data_dir=data_dir, decks_dir=stage_dir)
            )
            # CONTENT-COMPLETE run identity (Sol BLOCKER 1): derive the run_id from an immutable
            # manifest of the whole science (games, task universe, staged deck bytes, driver class
            # bytes, caps, bailout floor, harness/dist jar shas). Same universe+config → same run_id
            # → a resume replays committed state; ANY change → a DIFFERENT run_id → a fresh run whose
            # ops.duckdb rows never mix with the incompatible prior universe. The attempt/top-up caps
            # baked into the id are the ones passed to run_games_simd below (its defaults).
            attempt_cap, topup_cap = 2, 2
            run_id = _content_run_id(
                tasks, stage_dir, games=games, attempt_cap=attempt_cap, topup_cap=topup_cap,
                bailout_floor_ms=BAILOUT_HARD_FLOOR_MS, data_dir=data_dir,
            )
            result = run_games_simd(
                tasks,
                worker_cmd=cmd,
                ops_db_path=ops_db,
                stall_timeout_s=stall_timeout_s,
                monitor=monitor,
                attempt_cap=attempt_cap,
                topup_cap=topup_cap,
                run_id=run_id,
                # Arm the plausibility gate on the production path: sub-2s "wins" are engine bailouts
                # (audit) → non-decisive, excluded from W/L, surfaced in coverage.
                bailout_floor_ms=BAILOUT_HARD_FLOOR_MS,
                # preflight + singleton ALREADY done above (pre-staging). Passing them again would
                # double-run preflight and self-deadlock the flock (same process, second fd).
                preflight=None,
                singleton_lock_path=None,
                disk_governor=disk_governor,
                breaker=breaker,
                boot_deadline_s=boot_deadline_s,
                boot_backoff_base_s=boot_backoff_base_s,
                # Orphan reaping + own process group still carry through the engine's pidfile path.
                pidfile_path=pidfile,
                own_pgroup=own_pgroup,
            )
            # REPRODUCIBLE lake roll-in from the durable ops tables (NOT the filesystem — the
            # per-game drain already persisted + deleted each transcript at result-commit). This
            # re-keys the queue corpus onto joinable lake matchup rows (the (c) fix) and is
            # idempotent + re-runnable standalone via :func:`rollup_to_lake`. Best-effort — a lake
            # hiccup must never fail a completed run (the science is already durable in ops).
            try:
                rollup_to_lake(ops_db, data_dir=data_dir, run_id=run_id)
            except Exception:
                log.warning('lake roll-in failed (non-fatal — ops holds the durable record)', exc_info=True)
            return result
        finally:
            # Clean the corpus staging dirs on NORMAL and ERROR exit (Sol BLOCKER 3) — every
            # committed game's transcript is already drained into ops.duckdb (per-game, at
            # result-commit), so sweeping the ephemeral log dir loses NOTHING.
            shutil.rmtree(stage_dir, ignore_errors=True)
            shutil.rmtree(log_dir, ignore_errors=True)


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
    """One bucket's pooled lift ± 95% Newcombe CI + the rule-8 ship/thin verdict.

    ``lift_ci`` is Newcombe's Wilson-score interval for the difference of two independent
    proportions (method 10). The estimand is a POOLED GAME-LEVEL difference assuming the two arms'
    games are independent (no deck/opponent clustering modelled)."""

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
    **Newcombe's Wilson-score interval for the difference of two independent proportions** (method
    10 — the square-and-add of the two per-arm Wilson intervals; :func:`newcombe_diff_ci`), which
    carries a stated ~95% coverage guarantee — unlike the old naive endpoint subtraction
    ``(driver_lo - cp7_hi, driver_hi - cp7_lo)``, which had none. The estimand is a POOLED
    GAME-LEVEL difference of proportions assuming the two arms' games are INDEPENDENT (clustering by
    deck/opponent is not modelled). Ships iff that CI excludes 0 (lower bound > 0) AND
    ``n_matchups >= 30``.
    """
    dw = sum(m.driver_wins for m in members)
    dd = sum(m.driver_decided for m in members)
    cw = sum(m.cp7_wins for m in members)
    cd = sum(m.cp7_decided for m in members)
    n_matchups = sum(m.n_matchups for m in members)

    d_rate = dw / dd if dd else 0.0
    c_rate = cw / cd if cd else 0.0
    lift = d_rate - c_rate
    # Newcombe method 10 — Wilson-score CI for a difference of two independent proportions.
    lift_ci = newcombe_diff_ci(dw, dd, cw, cd)

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
    lines += [
        '_Lift 95% CI = Newcombe method-10 Wilson-score interval for a difference of two '
        'independent proportions; estimand = pooled game-level difference (independence assumed, '
        'no deck/opponent clustering modelled)._',
        '',
    ]
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


def _write_partial_coverage(
    result: Any,
    never_run: Sequence[str],
    *,
    out_dir: str | os.PathLike[str],
    field_names: Sequence[str] | None = None,
) -> Path:
    """Write the explicitly-named PARTIAL coverage artifact for an incomplete/invalid run.

    This is NOT the final ``driver-run-buckets.json`` W/L table (that publication is REFUSED for an
    incomplete run — Sol BLOCKER 1); it is a distinct ``driver-run-buckets.PARTIAL.json`` whose
    ``coverage`` block names every incomplete / exhausted / invalid cell and every never-run task, so
    the missing science is loudly visible instead of silently blessed."""
    out = Path(out_dir)
    partial_path = out / 'driver-run-buckets.PARTIAL.json'
    _write_json(
        partial_path,
        {
            'field': list(field_names or []),
            'partial': True,
            'coverage': {
                'complete': result.complete,
                'incomplete_cells': [list(c) for c in result.incomplete_cells],
                'exhausted_cells': [list(c) for c in result.exhausted_cells],
                'quarantined_cells': [list(c) for c in result.quarantined_cells],
                'invalid_cells': [list(c) for c in result.invalid_cells],
                'never_run_tasks': list(never_run),
                'cells': {'|'.join(k): list(v) for k, v in result.cells.items()},
            },
        },
    )
    return partial_path


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


def render_baseline_markdown(rows: Sequence[Any], *, field_names: Sequence[str] | None = None) -> str:
    """A readable markdown table of the baseline-only (single-arm) subjects' ABSOLUTE win-rates.

    ``rows`` are :class:`~pipeline.sim.aggregate.AbsoluteBaseline` records — a driverless deck has no
    driver delta, so it is reported as an absolute baseline win-rate (Wilson CI on decided games)
    plus the first-player starter split, kept clearly separate from the two-arm delta buckets."""
    lines = ['# Baseline-only subjects — absolute baseline win-rate (single-arm)', '']
    lines += [
        '_Driverless decks have NO driven arm, so they carry no driver delta. Each row is the '
        'absolute baseline win-rate vs the shared field (Wilson CI on DECIDED games) + the '
        'first-player starter split — reported apart from the two-arm delta buckets._',
        '',
    ]
    if field_names:
        lines += [f'Opponent field ({len(field_names)}): {", ".join(field_names)}', '']
    lines += [
        '| subject | matchups | baseline wr | 95% CI | record | A-start wr | B-start wr |',
        '| --- | --- | --- | --- | --- | --- | --- |',
    ]
    for r in rows:
        a = r.starter_split.get('A', {})
        b = r.starter_split.get('B', {})
        lines.append(
            f'| {r.subject} | {r.n_matchups} | {r.winrate:.3f} '
            f'| {r.winrate_ci[0]:.3f}..{r.winrate_ci[1]:.3f} | {r.wins}/{r.decided} '
            f'| {float(a.get("winrate", 0.0)):.3f} | {float(b.get("winrate", 0.0)):.3f} |'
        )
    return '\n'.join(lines) + '\n'


def write_baseline_table(
    rows: Sequence[Any],
    *,
    out_dir: str | os.PathLike[str],
    field_names: Sequence[str] | None = None,
) -> tuple[Path, Path]:
    """Write the baseline-only subjects' absolute-win-rate table (json + md) into ``out_dir``."""
    out = Path(out_dir)
    json_path = out / 'driver-run-baselines.json'
    md_path = out / 'driver-run-baselines.md'
    _write_json(
        json_path,
        {
            'field': list(field_names or []),
            'baselines': [
                {
                    'subject': r.subject,
                    'n_matchups': r.n_matchups,
                    'wins': r.wins,
                    'decided': r.decided,
                    'winrate': r.winrate,
                    'winrate_ci': list(r.winrate_ci),
                    'record': f'{r.wins}/{r.decided}',
                    'starter_split': r.starter_split,
                }
                for r in rows
            ],
        },
    )
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_baseline_markdown(rows, field_names=field_names), encoding='utf-8')
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
    from pipeline.sim.simd.governor import DiskGovernor

    # The simd engine calls ``preflight()`` with ZERO args, but ``preflight_java`` needs the
    # ``java`` launcher. Wire a zero-arg closure that resolves the runtime Java path (fail-loud if
    # missing) and version-gates it — F-1: passing the bare ``preflight_java`` TypeError'd at boot.
    # A2.5 disk protection (Fable M6): wire a production DiskGovernor with default floors so a
    # missing-Java crash-loop filling the disk HALTS the run resumably (previously the CLI passed
    # no governor, leaving the advertised soft/hard disk floors dead on the shipped path). A nonzero
    # boot-backoff base staggers respawns so a transient boot failure does not tight-loop staging.
    result = run_corpus_queue(
        rows=rows,
        field=field,
        games=args.games,
        monitor=monitor,
        preflight=lambda: preflight_java(_resolve_java()),
        disk_governor=DiskGovernor(),
        breaker=CrashLoopBreaker(),
        boot_deadline_s=120.0,
        boot_backoff_base_s=0.5,
    )
    log.info(
        'simd run complete=%s quarantined=%d incomplete_cells=%d fast_games=%d concede_games=%d '
        'invalid_cells=%d',
        result.complete, len(result.quarantined), len(result.incomplete_cells),
        result.fast_games, result.concede_games, len(result.invalid_cells),
    )
    publish_corpus_results(
        result, rows=rows, tightness=tightness, field_names=field_names, out_dir=args.out_dir
    )


def publish_corpus_results(
    result: Any,
    *,
    rows: Sequence[dict[str, Any]],
    tightness: dict[str, str],
    field_names: Sequence[str],
    out_dir: str | os.PathLike[str],
    thin_rows: Sequence[dict[str, Any]] = (),
) -> None:
    """Fold a committed simd ``result`` into the published corpus artifacts (or refuse loudly).

    The single owner of the corpus run's post-processing, shared by the CLI :func:`run` and the
    standalone full-corpus launcher so the two can never drift. Honors the PUBLICATION REFUSAL
    semantics (Sol BLOCKER 1): an incomplete/invalid run writes ONLY the explicitly-named PARTIAL
    coverage artifact and ``raise SystemExit(1)`` — it never publishes final buckets.

    A COMPLETE run writes two clearly-separated tables:

      * ``driver-run-buckets.{json,md}`` — the rule-8 driver-vs-CP7 DELTA buckets, over the two-arm
        DRIVE subjects only;
      * ``driver-run-baselines.{json,md}`` — the baseline-only (driverless) subjects' ABSOLUTE
        baseline win-rates (Wilson CI + starter split), which carry no delta and must be kept OUT of
        the delta buckets (a phantom 0/0 driven arm would fabricate a spurious negative lift).

    ``rows`` are the compiled DRIVE rows; ``thin_rows`` the baseline-only rows (used only to map the
    staged subject basename back to a deck_id for the tables). Aggregation runs over the ORIGINAL
    registered task universe (``result.task_ids``) so any never-run task surfaces as coverage."""
    from pipeline.sim.aggregate import BAILOUT_HARD_FLOOR_MS, aggregate_results

    if result.invalid_cells:
        # INVALID = a decisive claim with NO legal terminal cause (macro-game-over / unknown). This
        # must be near-zero; a nonzero count is a loud data-integrity alarm (a driver fabricating a
        # win). Surfaced distinctly so it is never mistaken for an ordinary non-decisive game.
        log.warning('DATA-INTEGRITY ALARM: %d cell(s) saw INVALID games (decisive claim, no legal '
                    'terminal cause) — excluded + topped up + flagged: %s',
                    len(result.invalid_cells), result.invalid_cells)

    # PUBLICATION REFUSAL (Sol BLOCKER 1): the final tables are science that gates ship/no-ship, so
    # they are written ONLY when the run is genuinely complete (every cell ok>=needed) AND saw zero
    # INVALID cells. An incomplete/exhausted/invalid run TERMINATES but must NOT publish: it writes an
    # explicitly-named PARTIAL coverage artifact and exits nonzero instead.
    never_run = [t for t in result.task_ids if t not in result.results and t not in result.quarantined]
    if not result.complete or result.invalid_cells:
        partial_path = _write_partial_coverage(result, never_run, out_dir=out_dir, field_names=field_names)
        log.error(
            'REFUSING to publish final buckets: complete=%s invalid_cells=%d incomplete_cells=%d '
            'exhausted_cells=%d never_run_tasks=%d — wrote PARTIAL coverage artifact: %s',
            result.complete, len(result.invalid_cells), len(result.incomplete_cells),
            len(result.exhausted_cells), len(never_run), partial_path,
        )
        print(f'PARTIAL (incomplete run — final buckets refused): {partial_path}')
        raise SystemExit(1)

    # Remap the tag maps from deck_id to the staged subject basename the tasks carry (both DRIVE and
    # thin rows stage under the same 's'-prefixed basename).
    all_rows = list(rows) + list(thin_rows)
    tight_by_subject = {
        _flat_deck_basename('s', str(r['deck_id'])): tightness.get(str(r['deck_id']), '') for r in all_rows
    }
    arche_by_subject = {
        _flat_deck_basename('s', str(r['deck_id'])): str(r.get('archetype') or '') for r in all_rows
    }
    agg = aggregate_results(
        result.task_ids, result.results, quarantined=result.quarantined, bailout_floor_ms=BAILOUT_HARD_FLOOR_MS
    )
    comparisons = agg.comparisons()
    # Split the roster: baseline-only (single-arm) subjects report an ABSOLUTE baseline win-rate;
    # only the two-arm subjects feed the driver-vs-CP7 delta buckets.
    baseline_only = set(agg.baseline_only_subjects())
    two_arm = {s: c for s, c in comparisons.items() if s not in baseline_only}
    stats = aggregate_buckets(_deltas_from_comparisons(two_arm, arche_by_subject, tight_by_subject))
    json_path, md_path = write_bucket_table(stats, out_dir=out_dir, field_names=field_names)
    print(render_bucket_markdown(stats, field_names=field_names))
    print(f'buckets: {json_path}')
    print(f'buckets: {md_path}')

    baseline_rows = [agg.absolute_baseline(s) for s in agg.baseline_only_subjects()]
    if baseline_rows:
        b_json, b_md = write_baseline_table(baseline_rows, out_dir=out_dir, field_names=field_names)
        print(render_baseline_markdown(baseline_rows, field_names=field_names))
        print(f'baselines: {b_json}')
        print(f'baselines: {b_md}')


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
