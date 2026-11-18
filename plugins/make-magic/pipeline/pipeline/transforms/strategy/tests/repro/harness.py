"""Standing reproducibility gate — Leg A / Leg B harness (library + CLI).

Promotes the proven two-leg reproducibility procedure into a repeatable,
*infrequently-run* verification gate. It proves the rich strategy-engine
extraction (attributes -> events -> eventeffects -> strategies -> engines) is a
property of the *project* (code + its declared inputs), not of a developer's
local machine state:

* **Leg B (determinism, ADR 0003/0022).** Two independent fresh runs against the
  committed pinned lake, under a deliberately varied ``PYTHONHASHSEED`` + external
  scheduling pressure, must produce **identical** composite SHAs (ENGINE /
  RESOLVE / FULL_CORPUS) == the pinned manifest values AND a **zero-divergence**
  semantic diff. Any divergence is a code-non-determinism bug.

* **Leg A (drift impact).** A genuinely live Scryfall ``oracle_tags.sync`` into an
  isolated lake that keeps the pinned card bulk frozen, re-rolled into a fresh
  ``card_otag``, then extracted and diffed against the pinned reference. Quantifies
  how much real upstream tag drift perturbs the rich outputs. The card-universe
  bucket MUST stay empty (else the isolation leaked). ``live`` + opt-in gated.

Design doctrine this honors:

* Every extraction runs in a **fresh OS subprocess** with ``MAKE_MAGIC_DATA_DIR``
  pointed at an isolated ``tempfile.mkdtemp()`` lake (the pipeline memoizes assets
  and ``@lru_cache``-backs adjacency, so swapping the data dir in-process serves
  stale caches).
* Isolated lakes are seeded **only** from the committed pinned fixture, whose
  SHAs are verified against the manifest before every use. The harness refuses to
  run on a fixture that does not match the manifest.
* ``build.run()`` / ``hydrate-lake`` are **never** called (they hardcode the
  primary ``~/.local/share/make-magic`` store). The only builder invoked is
  ``pipeline.transforms.otag_rollup`` (Leg A), and only inside the isolated dir.
* The ADR-0022 enumerated-adjacency fallback (a *different* algorithm, fired when
  the lake lacks ``raw/oracle_tags.parquet``) **fails the leg** if its warning
  appears in any subprocess's output.
* A primary-lake integrity self-check SHAs the live-lake files before/after and
  fails if the harness mutated them.

CLI::

    python -m pipeline.transforms.strategy.tests.repro.harness legb --out <dir>
    python -m pipeline.transforms.strategy.tests.repro.harness lega --out <dir>   # live fetch
    python -m pipeline.transforms.strategy.tests.repro.harness all  --out <dir>   # legb then lega
    python -m pipeline.transforms.strategy.tests.repro.harness repin --from <lake>  # re-pin fixture

Each run writes ``verdict.json`` (machine) + ``verdict.md`` (human) to ``--out``;
exit 0 on PASS, non-zero on FAIL (naming the failing leg).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Locations + constants
# --------------------------------------------------------------------------- #

REPRO_DIR = Path(__file__).resolve().parent
PINNED_DIR = REPRO_DIR / 'pinned_lake'
MANIFEST_PATH = PINNED_DIR / 'manifest.json'

#: The four lake files every isolated extraction lake needs (ADR-0022: omitting
#: ``raw/oracle_tags.parquet`` silently switches to the enumerated-adjacency
#: fallback — a different algorithm).
SEED_FILES: tuple[str, ...] = (
    'raw/oracle_cards.parquet',
    'raw/oracle_tags.parquet',
    'normalized/card_otag.parquet',
    'normalized/combo.parquet',
)

#: Leg A freezes the card bulk + combo mart and refetches tags live, so it seeds
#: ONLY these two (no tags, no cursor, no card_otag) before the live sync.
LEG_A_SEED: tuple[str, ...] = ('raw/oracle_cards.parquet', 'normalized/combo.parquet')

#: Substring that marks the ADR-0022 enumerated-adjacency fallback warning
#: (emitted by both ``assets.load_assets`` and ``engine_hydration._load_events``).
FALLBACK_SIGNATURE = 'enumerated event adjacency'

#: Leg-A liveness floor: the bundled offline snapshot carries ~4,499 tags, so a
#: genuine live fetch must exceed it comfortably (fail-open yields the snapshot).
MIN_LIVE_TAGS = 4600

#: The coherent pre-drift raw-tags SHA that produced the pinned ``card_otag``
#: (``a51097a5``). It is no longer on any live disk; a live fetch returning it
#: would be impossible, but we assert the fetched SHA differs from it as a
#: fail-open tripwire (fail-open could never produce it either).
COHERENT_SOURCE_SHA = 'df31df7ceaf4354a0ee5b9b15f5b897fd6b59a9902fa3df605f01833b71dfe3d'

#: The six primary-store files the harness must never mutate.
PRIMARY_FILES: tuple[str, ...] = (
    'raw/oracle_cards.parquet',
    'raw/oracle_tags.parquet',
    'normalized/card_otag.parquet',
    'normalized/combo.parquet',
    'machine_vocab/attributes.jsonl',
    'raw/_cursors.json',
)


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #


def _now() -> str:
    return dt.datetime.now().isoformat()


def sha256_file(path: Path) -> str | None:
    """sha256 hex of a file, or ``None`` when it does not exist."""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pipeline_root() -> Path:
    """The outer pipeline dir (holds ``pyproject.toml``) — subprocess cwd."""
    for parent in REPRO_DIR.parents:
        if (parent / 'pyproject.toml').exists():
            return parent
    raise RuntimeError('could not locate the pipeline root (no pyproject.toml above repro/)')


def primary_lake_root() -> Path:
    """The real canonical store — explicitly, so an env override cannot hide it."""
    return Path.home() / '.local' / 'share' / 'make-magic'


def primary_lake_shas() -> dict[str, str | None]:
    root = primary_lake_root()
    return {rel: sha256_file(root / rel) for rel in PRIMARY_FILES}


def tag_count(parquet: Path) -> int:
    """Distinct-tag count of a ``raw/oracle_tags`` parquet (liveness check)."""
    import duckdb

    con = duckdb.connect(':memory:')
    try:
        row = con.execute(f"SELECT COUNT(*) FROM read_parquet('{parquet}')").fetchone()
        return int(row[0]) if row else -1
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Manifest + fixture integrity
# --------------------------------------------------------------------------- #


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


def pinned_composites() -> dict[str, str]:
    """ENGINE / RESOLVE / FULL_CORPUS as pinned in the committed manifest."""
    return dict(load_manifest()['pinned_composites'])


def verify_pinned_fixture() -> dict[str, str]:
    """Verify each committed fixture file's SHA == the manifest; raise on mismatch.

    Returns the ``{rel: sha}`` map on success. This is the gate's root of trust:
    a reproducibility proof against the wrong reference proves nothing.
    """
    manifest = load_manifest()
    files = manifest['files']
    observed: dict[str, str] = {}
    mismatches: list[str] = []
    for rel in SEED_FILES:
        want = files[rel]['sha256']
        got = sha256_file(PINNED_DIR / rel)
        observed[rel] = got or '<missing>'
        if got != want:
            mismatches.append(f'{rel}: manifest {want[:12]}… != fixture {(got or "<missing>")[:12]}…')
    if mismatches:
        raise RuntimeError('pinned-lake fixture does not match manifest:\n  ' + '\n  '.join(mismatches))
    return observed


def seed_lake(dest: Path, rels: tuple[str, ...]) -> None:
    """Copy ``rels`` from the pinned fixture into ``dest`` (lake layout), sha-verified."""
    manifest = load_manifest()
    for rel in rels:
        src = PINNED_DIR / rel
        tgt = dest / rel
        tgt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, tgt)
        got = sha256_file(tgt)
        want = manifest['files'][rel]['sha256']
        if got != want:
            raise RuntimeError(f'seed copy mismatch for {rel}: {got} != {want}')


# --------------------------------------------------------------------------- #
# Subprocess extraction primitives (every extraction = a fresh OS subprocess)
# --------------------------------------------------------------------------- #


def _subprocess_env(data_dir: Path, seed: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env['MAKE_MAGIC_DATA_DIR'] = str(data_dir)
    env['PYTHONHASHSEED'] = seed
    if extra:
        env.update(extra)
    return env


def _run(cmd: list[str], env: dict[str, str], timeout: int = 1200) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(pipeline_root()),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _assert_no_fallback(cp: subprocess.CompletedProcess, label: str) -> None:
    blob = (cp.stdout or '') + (cp.stderr or '')
    if FALLBACK_SIGNATURE in blob:
        raise RuntimeError(
            f'{label}: ADR-0022 enumerated-adjacency fallback fired (lake missing raw/oracle_tags.parquet). '
            f'This is a DIFFERENT algorithm; the leg is invalid.'
        )


def run_oracle(data_dir: Path, seed: str) -> dict[str, str]:
    """Run the parity oracle in an isolated lake; return the 3 composites.

    Parses ``engine_parity_oracle``'s stdout for the ENGINE / RESOLVE /
    FULL_CORPUS composites. Fails on the ADR-0022 fallback warning. The oracle
    rewrites ``fixtures/engine_isolation_baseline.json`` byte-identically when
    nothing moved — callers assert the repo tree stays clean afterwards.
    """
    cp = _run(
        [sys.executable, '-m', 'pipeline.transforms.strategy.tests.engine_parity_oracle'],
        _subprocess_env(data_dir, seed),
    )
    if cp.returncode != 0:
        raise RuntimeError(f'oracle failed (rc={cp.returncode}):\n{cp.stderr[-2000:]}')
    _assert_no_fallback(cp, 'oracle')
    out = cp.stdout
    composites: dict[str, str] = {}
    for line, key in (
        ('COMPOSITE engine_isolation SHA:', 'ENGINE'),
        ('COMPOSITE resolve SHA:', 'RESOLVE'),
        ('Canonical full-corpus SHA:', 'FULL_CORPUS'),
    ):
        for row in out.splitlines():
            if line in row:
                composites[key] = row.split(line, 1)[1].strip()
                break
    missing = [k for k in ('ENGINE', 'RESOLVE', 'FULL_CORPUS') if k not in composites]
    if missing:
        raise RuntimeError(f'oracle stdout missing composites {missing}:\n{out[-2000:]}')
    return composites


def run_dump(data_dir: Path, out_path: Path, seed: str) -> Path:
    """Write a semantic dump in an isolated lake (fresh subprocess). Returns ``out_path``."""
    cp = _run(
        [
            sys.executable,
            '-m',
            'pipeline.transforms.strategy.tests.repro.semantic_dump',
            '--out',
            str(out_path),
            '--frozen-meta',
        ],
        _subprocess_env(data_dir, seed),
    )
    if cp.returncode != 0:
        raise RuntimeError(f'semantic_dump failed (rc={cp.returncode}):\n{cp.stderr[-2000:]}')
    _assert_no_fallback(cp, 'semantic_dump')
    return out_path


def run_rollup(data_dir: Path) -> str:
    """Rebuild ``normalized/card_otag`` in an isolated lake (the only builder we run)."""
    cp = _run(
        [sys.executable, '-m', 'pipeline.transforms.otag_rollup'],
        _subprocess_env(data_dir, '0'),
    )
    if cp.returncode != 0:
        raise RuntimeError(f'otag_rollup failed (rc={cp.returncode}):\n{cp.stderr[-2000:]}')
    return (cp.stdout or '') + (cp.stderr or '')


def run_diff(a: Path, b: Path, report: Path | None = None, json_out: Path | None = None) -> dict:
    """Diff two dumps in-process (pure; reads JSON, no lake). Returns a summary dict."""
    from pipeline.transforms.strategy.tests.repro import semantic_diff as sd

    da = json.loads(a.read_text())
    db = json.loads(b.read_text())
    diff = sd.diff_dumps(da, db)
    code = sd.exit_code(diff)
    if report is not None:
        report.write_text(sd.render_report(diff))
    if json_out is not None:
        json_out.write_text(json.dumps(diff, sort_keys=True, indent=2, default=list))
    return {
        'exit_code': code,
        'decks_moved': diff['decks_moved'],
        'total_decks': diff['total_decks'],
        'n_substantive': len(diff['substantive_list']),
        'n_benign': len(diff['benign_list']),
        'n_card_universe': len(diff['card_universe_list']),
        'substantive_list': diff['substantive_list'],
        'card_universe_list': diff['card_universe_list'],
        'counts': diff['counts'],
    }


# --------------------------------------------------------------------------- #
# CPU-load variation for Leg B run2 (best-effort, portable, self-terminating)
# --------------------------------------------------------------------------- #


def _start_burners(n: int, max_seconds: int = 900) -> list[subprocess.Popen]:
    """Spawn ``n`` portable CPU burners that self-terminate after ``max_seconds``.

    There is no DuckDB thread knob in the strategy path (every ``duckdb.connect``
    is ``':memory:'`` with no ``threads``/``PRAGMA``/``config=`` — verified), so
    scheduling variation for run2 is applied externally. Best-effort: a failure to
    spawn is swallowed (the leg still runs, just without the extra pressure).
    """
    burners: list[subprocess.Popen] = []
    code = f'import time\nend=time.time()+{max_seconds}\nwhile time.time()<end: pass'
    for _ in range(n):
        try:
            burners.append(
                subprocess.Popen(
                    [sys.executable, '-c', code],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        except Exception:
            break
    return burners


def _stop_burners(burners: list[subprocess.Popen]) -> None:
    for b in burners:
        with contextlib.suppress(Exception):
            b.kill()
    for b in burners:
        with contextlib.suppress(Exception):
            b.wait(timeout=5)


# --------------------------------------------------------------------------- #
# Live oracle_tags fetch (Leg A) — ONE sync, structured evidence log
# --------------------------------------------------------------------------- #


def _fetch_log(path: Path, line: str) -> None:
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(line + '\n')


def _do_live_fetch(data_dir: Path, fetch_log: Path) -> int:
    """Perform exactly ONE live ``oracle_tags.sync(force=True)`` (2 HTTP GETs).

    Runs inside the ``_fetch`` subcommand (fresh subprocess, ``MAKE_MAGIC_DATA_DIR``
    already set). Captures real HTTP evidence via httpx event hooks and appends
    FETCH-START/META/PAYLOAD/RESULT/ERROR lines to ``fetch_log`` (the reference
    fetch-driver pattern). Returns 0 genuine-live, 3 fail-open, 2 sync raised.
    Polite client only: the sync's own UA + throttle; one attempt; honors any
    surfaced 429/Retry-After (sync does not retry-loop).
    """
    import httpx

    from pipeline.sources import oracle_tags

    records: list[dict] = []

    def _on_response(response: httpx.Response) -> None:
        response.read()
        url = str(response.url)
        rec = {'url': url, 'status': response.status_code, 'bytes': len(response.content)}
        rec['secs'] = response.elapsed.total_seconds()
        records.append(rec)
        if url == oracle_tags.BULK_META_URL:
            try:
                updated = str(response.json().get('updated_at'))
            except Exception:
                updated = '<unparsed>'
            _fetch_log(fetch_log, f'FETCH-META {updated} {response.status_code}')
        else:
            _fetch_log(fetch_log, f'FETCH-PAYLOAD {rec["bytes"]} {rec["secs"]:.3f}')

    _fetch_log(fetch_log, f'FETCH-START {_now()}')
    client = httpx.Client(event_hooks={'response': [_on_response]})
    try:
        path = oracle_tags.sync(client=client, force=True)
    except Exception as exc:
        _fetch_log(fetch_log, f'FETCH-ERROR sync-raised {type(exc).__name__}: {exc}')
        client.close()
        return 2
    client.close()

    got_meta = any(r['url'] == oracle_tags.BULK_META_URL and r['status'] == 200 for r in records)
    got_payload = any(
        r['url'] != oracle_tags.BULK_META_URL and r['status'] == 200 and r['bytes'] > 1_000_000 for r in records
    )
    if not (got_meta and got_payload):
        _fetch_log(fetch_log, 'FETCH-ERROR fail-open-detected (no genuine-live meta+payload HTTP evidence)')
        return 3
    sha = sha256_file(Path(path)) or ''
    try:
        count = tag_count(Path(path))
    except Exception:
        count = -1
    _fetch_log(fetch_log, f'FETCH-RESULT {sha[:8]} {count}')
    return 0


def fetch_live_tags(data_dir: Path, fetch_log: Path) -> dict[str, Any]:
    """Drive the single live fetch in a fresh subprocess; verify genuine-live.

    Returns an evidence dict with ``genuine`` set iff the subprocess reported a
    real meat+payload fetch AND the resulting parquet has >= MIN_LIVE_TAGS tags
    AND its SHA differs from the (unrecoverable) coherent pre-drift source.
    """
    cp = _run(
        [
            sys.executable,
            '-m',
            'pipeline.transforms.strategy.tests.repro.harness',
            '_fetch',
            '--data-dir',
            str(data_dir),
            '--fetch-log',
            str(fetch_log),
        ],
        _subprocess_env(data_dir, '0'),
    )
    parquet = data_dir / 'raw' / 'oracle_tags.parquet'
    sha = sha256_file(parquet)
    count = tag_count(parquet) if parquet.exists() else -1
    genuine = cp.returncode == 0 and count >= MIN_LIVE_TAGS and sha is not None and sha != COHERENT_SOURCE_SHA
    reasons: list[str] = []
    if cp.returncode != 0:
        reasons.append(f'fetch subprocess rc={cp.returncode} (fail-open / sync error — no genuine-live HTTP evidence)')
    if count < MIN_LIVE_TAGS:
        reasons.append(f'tag count {count} < {MIN_LIVE_TAGS} (bundled-snapshot-sized — fail-open)')
    if sha == COHERENT_SOURCE_SHA:
        reasons.append('fetched SHA == coherent pre-drift source (impossible from live — reference corrupted)')
    return {
        'genuine': genuine,
        'returncode': cp.returncode,
        'fetched_sha': sha,
        'tag_count': count,
        'invalid_reasons': reasons,
        'stdout_tail': (cp.stdout or '')[-800:],
        'stderr_tail': (cp.stderr or '')[-800:],
    }


# --------------------------------------------------------------------------- #
# Repo-tree cleanliness (the oracle rewrites a committed fixture)
# --------------------------------------------------------------------------- #

FIXTURE_REWRITTEN = 'pipeline/transforms/strategy/tests/fixtures/engine_isolation_baseline.json'


def repo_tree_dirty_paths() -> list[str]:
    """``git status --porcelain`` paths relative to the repo (empty == clean)."""
    cp = subprocess.run(
        ['git', 'status', '--porcelain'],
        cwd=str(pipeline_root()),
        capture_output=True,
        text=True,
    )
    return [ln[3:] for ln in cp.stdout.splitlines() if ln.strip()]


def restore_fixture() -> None:
    """Restore the committed ``engine_isolation_baseline.json`` from git.

    The parity oracle rewrites this *repo* file (not the isolated lake). On Leg B
    the rewrite is byte-identical (a no-op checkout); if a determinism bug or
    genuine tag drift moves it, this still leaves the working tree clean so the
    harness never pollutes the repo. Best-effort (swallows git errors).
    """
    subprocess.run(
        ['git', 'checkout', '--', FIXTURE_REWRITTEN],
        cwd=str(pipeline_root()),
        capture_output=True,
        text=True,
    )


def dump_composites(dump_path: Path) -> dict[str, str]:
    """Read ENGINE/RESOLVE/FULL_CORPUS composites from a dump's ``meta.composites``.

    These are computed by ``semantic_dump`` over the SAME records it serializes,
    and (on a full corpus) are byte-equal to the parity oracle's composites — so
    Leg A reads them here instead of running the oracle, which would write to the
    repo fixture (drift would dirty the tree).
    """
    comp = json.loads(dump_path.read_text())['meta']['composites']
    return {
        'ENGINE': comp.get('engine', {}).get('sha', ''),
        'RESOLVE': comp.get('resolve', {}).get('sha', ''),
        'FULL_CORPUS': comp.get('full_corpus', {}).get('sha', ''),
    }


# --------------------------------------------------------------------------- #
# Legs
# --------------------------------------------------------------------------- #


def leg_b(out_dir: Path) -> dict:
    """Determinism: two fresh runs, pinned lake, varied seed + CPU load.

    PASS iff run1 SHAs == run2 SHAs == pinned composites (all three) AND the
    semantic diff is zero-divergence.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    verify_pinned_fixture()
    primary_before = primary_lake_shas()
    pinned = pinned_composites()

    tmp = Path(tempfile.mkdtemp(prefix='repro-legb-'))
    lake_a = tmp / 'lake-runA'
    lake_b = tmp / 'lake-runB'
    dump_a = out_dir / 'runA-dump.json'
    dump_b = out_dir / 'runB-dump.json'
    fixture_before = sha256_file(pipeline_root() / FIXTURE_REWRITTEN)
    burners: list[subprocess.Popen] = []
    try:
        seed_lake(lake_a, SEED_FILES)
        seed_lake(lake_b, SEED_FILES)

        # run1: PYTHONHASHSEED=0, no external load.
        comp_a = run_oracle(lake_a, '0')
        run_dump(lake_a, dump_a, '0')

        # run2: PYTHONHASHSEED=4242, external CPU scheduling pressure.
        burners = _start_burners(min(6, (os.cpu_count() or 2)))
        comp_b = run_oracle(lake_b, '4242')
        run_dump(lake_b, dump_b, '4242')
        _stop_burners(burners)
        burners = []

        diff = run_diff(dump_a, dump_b, out_dir / 'runB-vs-runA.md', out_dir / 'runB-vs-runA.json')
    finally:
        _stop_burners(burners)
        shutil.rmtree(tmp, ignore_errors=True)

    primary_after = primary_lake_shas()
    fixture_after = sha256_file(pipeline_root() / FIXTURE_REWRITTEN)
    dirty = repo_tree_dirty_paths()
    # Always leave the working tree clean (the oracle rewrote the repo fixture).
    restore_fixture()

    sha_match = comp_a == comp_b == pinned
    diff_clean = diff['exit_code'] == 0 and diff['decks_moved'] == 0
    primary_ok = primary_before == primary_after
    fixture_clean = fixture_before == fixture_after and FIXTURE_REWRITTEN not in dirty
    passed = sha_match and diff_clean and primary_ok and fixture_clean

    reasons: list[str] = []
    if not sha_match:
        for key in ('ENGINE', 'RESOLVE', 'FULL_CORPUS'):
            if not (comp_a.get(key) == comp_b.get(key) == pinned.get(key)):
                ra, rb, pp = comp_a.get(key, '?')[:12], comp_b.get(key, '?')[:12], pinned.get(key, '?')[:12]
                reasons.append(
                    f'{key} composite diverged (code-non-determinism: DuckDB scan order / hash seed / float): '
                    f'runA={ra} runB={rb} pinned={pp}'
                )
    if not diff_clean:
        reasons.append(
            f'semantic diff nonzero (exit {diff["exit_code"]}, {diff["decks_moved"]} decks moved, '
            f'{diff["n_substantive"]} substantive) — a layer is non-deterministic'
        )
    if not primary_ok:
        reasons.append('primary lake MUTATED by the harness (integrity breach)')
    if not fixture_clean:
        reasons.append(f'{FIXTURE_REWRITTEN} not rewritten byte-identically (held fixture moved)')

    return {
        'leg': 'B',
        'name': 'determinism',
        'pass': passed,
        'verdict': 'PASS' if passed else 'FAIL',
        'reasons': reasons,
        'composites_runA': comp_a,
        'composites_runB': comp_b,
        'composites_pinned': pinned,
        'diff': {
            k: diff[k]
            for k in ('exit_code', 'decks_moved', 'total_decks', 'n_substantive', 'n_benign', 'n_card_universe')
        },
        'primary_lake_integrity': 'ok' if primary_ok else 'MUTATED',
        'artifacts': {'runA_dump': str(dump_a), 'runB_dump': str(dump_b)},
    }


def leg_a(out_dir: Path, *, reference_dump: Path | None = None, fetch_log: Path | None = None) -> dict:
    """Drift impact: live tags into a frozen-bulk lake, diffed vs the pinned reference.

    PASS iff the fetch is genuine-live, the card-universe bucket is empty (else
    INVALID — isolation breach), and there are zero substantive divergences.
    Substantive>0 yields SUBSTANTIVE-REVIEW (needs human adjudication, non-zero
    exit): drift is expected, the verdict is the benign-vs-substantive judgment.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    verify_pinned_fixture()
    primary_before = primary_lake_shas()
    pinned = pinned_composites()
    if fetch_log is None:
        fetch_log = out_dir / 'fetch.log'

    tmp = Path(tempfile.mkdtemp(prefix='repro-lega-'))
    lake = tmp / 'lake-legA'
    ref_lake = tmp / 'lake-ref'
    dump_lega = out_dir / 'legA-dump.json'
    try:
        # Reference dump: if not supplied (e.g. `all` passes Leg B's run1), build
        # one from the full pinned 4-file seed so Leg A has a pinned baseline.
        if reference_dump is None:
            seed_lake(ref_lake, SEED_FILES)
            reference_dump = out_dir / 'pinned-ref-dump.json'
            run_dump(ref_lake, reference_dump, '0')

        # Frozen bulk only: no tags, no cursor, no card_otag.
        seed_lake(lake, LEG_A_SEED)
        fetch = fetch_live_tags(lake, fetch_log)
        card_otag_sha = None
        card_otag_counts: dict[str, int] = {}
        comp: dict[str, str] = {}
        diff: dict = {}
        if fetch['genuine']:
            run_rollup(lake)
            card_otag_parquet = lake / 'normalized' / 'card_otag.parquet'
            card_otag_sha = sha256_file(card_otag_parquet)
            card_otag_counts = _card_otag_counts(card_otag_parquet)
            # NB: Leg A does NOT run the parity oracle — the oracle writes the repo
            # fixture (engine_isolation_baseline.json), which genuine tag drift would
            # legitimately move, dirtying the tree. Composites come from the dump's
            # own meta (same records, byte-equal to the oracle on a full corpus); the
            # fixture-layer drift surfaces properly as fixture:: decks in the diff.
            run_dump(lake, dump_lega, '0')
            comp = dump_composites(dump_lega)
            diff = run_diff(reference_dump, dump_lega, out_dir / 'legA-vs-pinned.md', out_dir / 'legA-vs-pinned.json')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        restore_fixture()  # belt-and-suspenders: never leave the repo dirty

    primary_after = primary_lake_shas()
    primary_ok = primary_before == primary_after

    reasons: list[str] = []
    if not fetch['genuine']:
        verdict = 'INVALID'
        reasons.append('live fetch not genuine (liveness guard): ' + '; '.join(fetch['invalid_reasons']))
    elif diff.get('n_card_universe', 0) > 0:
        verdict = 'INVALID'
        reasons.append(
            f'card-universe bucket NON-EMPTY ({diff["n_card_universe"]}) — the pinned card bulk leaked; '
            f'isolation breach (test-integrity failure, not a drift finding)'
        )
    elif not primary_ok:
        verdict = 'INVALID'
        reasons.append('primary lake MUTATED by the harness (integrity breach)')
    elif diff.get('n_substantive', 0) > 0:
        verdict = 'SUBSTANTIVE-REVIEW'
        reasons.append(
            f'{diff["n_substantive"]} substantive divergence(s) across {diff["decks_moved"]} deck(s) — '
            f'needs human adjudication (benign-by-judgment / improvement / regression). See legA-vs-pinned.md.'
        )
    else:
        verdict = 'PASS'

    composites_match = bool(comp) and comp == pinned
    return {
        'leg': 'A',
        'name': 'drift-impact',
        'pass': verdict == 'PASS',
        'verdict': verdict,
        'reasons': reasons,
        'fetch': {k: fetch[k] for k in ('genuine', 'returncode', 'fetched_sha', 'tag_count', 'invalid_reasons')},
        'card_otag_rebuilt_sha': card_otag_sha,
        'card_otag_counts': card_otag_counts,
        'composites_legA': comp,
        'composites_pinned': pinned,
        'composites_match_pinned': composites_match,
        'diff': {
            k: diff.get(k)
            for k in ('exit_code', 'decks_moved', 'total_decks', 'n_substantive', 'n_benign', 'n_card_universe')
        },
        'drift_impact': (
            f'{diff.get("decks_moved", "?")}/{diff.get("total_decks", "?")} decks moved, '
            f'{diff.get("n_substantive", "?")} substantive'
        ),
        'primary_lake_integrity': 'ok' if primary_ok else 'MUTATED',
        'artifacts': {'legA_dump': str(dump_lega), 'fetch_log': str(fetch_log)},
    }


def _card_otag_counts(parquet: Path) -> dict[str, int]:
    import duckdb

    if not parquet.exists():
        return {}
    con = duckdb.connect(':memory:')
    try:
        row = con.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT oracle_id), COUNT(DISTINCT slug) FROM read_parquet('{parquet}')"
        ).fetchone()
        if not row:
            return {}
        return {'rows': int(row[0]), 'distinct_cards': int(row[1]), 'distinct_slugs': int(row[2])}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Verdict writers
# --------------------------------------------------------------------------- #


def write_verdicts(out_dir: Path, legs: list[dict]) -> bool:
    """Write verdict.json + verdict.md. Returns the overall pass flag."""
    out_dir.mkdir(parents=True, exist_ok=True)
    overall = all(leg['pass'] for leg in legs)
    payload = {
        'overall_pass': overall,
        'created_at': _now(),
        'pinned_commit': load_manifest().get('git_commit'),
        'legs': legs,
    }
    (out_dir / 'verdict.json').write_text(json.dumps(payload, indent=2, default=str))

    lines = ['# Reproducibility gate verdict', '', f'**Overall: {"PASS" if overall else "FAIL"}**', '']
    for leg in legs:
        lines.append(f'## Leg {leg["leg"]} — {leg["name"]}: {leg["verdict"]}')
        lines.append('')
        if leg['leg'] == 'B':
            lines.append('| composite | runA | runB | pinned | match |')
            lines.append('| --- | --- | --- | --- | --- |')
            for key in ('ENGINE', 'RESOLVE', 'FULL_CORPUS'):
                a = leg['composites_runA'].get(key, '?')
                b = leg['composites_runB'].get(key, '?')
                p = leg['composites_pinned'].get(key, '?')
                ok = '✅' if a == b == p else '❌'
                lines.append(f'| {key} | `{a[:12]}…` | `{b[:12]}…` | `{p[:12]}…` | {ok} |')
            lines.append('')
            d = leg['diff']
            lines.append(
                f'- semantic diff: exit {d["exit_code"]}, {d["decks_moved"]}/{d["total_decks"]} decks moved, '
                f'{d["n_substantive"]} substantive · {d["n_benign"]} benign · {d["n_card_universe"]} card-universe'
            )
        else:
            f = leg['fetch']
            sha8 = str(f['fetched_sha'])[:8]
            lines.append(
                f'- live fetch genuine: **{f["genuine"]}** (rc={f["returncode"]}, sha `{sha8}`, {f["tag_count"]} tags)'
            )
            lines.append(f'- card_otag rebuilt: `{str(leg["card_otag_rebuilt_sha"])[:12]}` {leg["card_otag_counts"]}')
            lines.append(f'- composites match pinned: **{leg["composites_match_pinned"]}**')
            lines.append(f'- drift impact: **{leg["drift_impact"]}**')
            d = leg['diff']
            lines.append(f'- card-universe bucket: **{d.get("n_card_universe")}** (MUST be 0)')
        lines.append(f'- primary-lake integrity: {leg["primary_lake_integrity"]}')
        if leg['reasons']:
            lines.append('- reasons:')
            lines.extend(f'  - {r}' for r in leg['reasons'])
        lines.append('')
    (out_dir / 'verdict.md').write_text('\n'.join(lines))
    return overall


# --------------------------------------------------------------------------- #
# Re-pin (when a coherence re-baseline intentionally moves FULL_CORPUS)
# --------------------------------------------------------------------------- #


def repin(source_lake: Path, out_dir: Path | None = None) -> dict:
    """Re-pin the committed fixture from ``source_lake`` + regenerate the manifest.

    Copies the four lake files into ``pinned_lake/``, computes the three composites
    by running the oracle against ``source_lake`` in a fresh subprocess, and writes
    a fresh ``manifest.json`` (file SHAs + composites + carried-over provenance
    notes). Use ONLY after an intended, proof-carrying re-baseline (ADR 0003).
    """
    for rel in SEED_FILES:
        src = source_lake / rel
        if not src.exists():
            raise RuntimeError(f'source lake missing {rel} (cannot re-pin from {source_lake})')
    # Compute the new composites from the source lake.
    composites = run_oracle(source_lake, '0')
    # Copy the four files into the fixture.
    for rel in SEED_FILES:
        tgt = PINNED_DIR / rel
        tgt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_lake / rel, tgt)
    old = load_manifest()
    files: dict[str, Any] = {}
    for rel in SEED_FILES:
        p = PINNED_DIR / rel
        prev = old.get('files', {}).get(rel, {})
        files[rel] = {
            'lake_rel_path': rel,
            'bytes': p.stat().st_size,
            'sha256': sha256_file(p),
            'read_by_extraction': prev.get('read_by_extraction', rel != 'raw/oracle_tags.parquet'),
        }
        for carry in ('note', 'provenance_only', 'coherent_source_sha256', 'drift_note'):
            if carry in prev:
                files[rel][carry] = prev[carry]
    manifest = {
        'description': old.get('description'),
        'pinned_at': _now(),
        'git_commit': _git_head(),
        'git_branch': _git_branch(),
        'pinned_composites': composites,
        'extraction_reads': old.get('extraction_reads'),
        'extraction_reads_note': old.get('extraction_reads_note'),
        'files': files,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    verify_pinned_fixture()
    result = {'repinned': True, 'composites': composites, 'files': {k: files[k]['sha256'] for k in files}}
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / 'repin.json').write_text(json.dumps(result, indent=2, default=str))
    return result


def _git_head() -> str:
    cp = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=str(pipeline_root()), capture_output=True, text=True)
    return cp.stdout.strip()


def _git_branch() -> str:
    cp = subprocess.run(
        ['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=str(pipeline_root()), capture_output=True, text=True
    )
    return cp.stdout.strip()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _default_out() -> Path:
    return Path(tempfile.mkdtemp(prefix='repro-out-'))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Standing reproducibility gate (Leg A / Leg B).')
    sub = ap.add_subparsers(dest='cmd', required=True)

    for name in ('legb', 'lega', 'all'):
        p = sub.add_parser(name)
        p.add_argument('--out', type=Path, default=None, help='artifact dir (default: a fresh tmp dir)')
        p.add_argument(
            '--fetch-log', type=Path, default=None, help='Leg A: append FETCH-* lines here (default <out>/fetch.log)'
        )

    pre = sub.add_parser('repin', help='re-pin the fixture from a source lake (ADR 0003 re-baseline)')
    pre.add_argument(
        '--from', dest='source', type=Path, required=True, help='source lake dir (MAKE_MAGIC_DATA_DIR-style)'
    )
    pre.add_argument('--out', type=Path, default=None)

    pf = sub.add_parser('_fetch', help=argparse.SUPPRESS)
    pf.add_argument('--data-dir', type=Path, required=True)
    pf.add_argument('--fetch-log', type=Path, required=True)

    args = ap.parse_args(argv)

    if args.cmd == '_fetch':
        return _do_live_fetch(args.data_dir, args.fetch_log)

    if args.cmd == 'repin':
        res = repin(args.source, args.out)
        print(f'RE-PINNED. composites={json.dumps(res["composites"])}')
        print(f'  files: {json.dumps(res["files"], indent=2)}')
        return 0

    out = args.out or _default_out()
    fetch_log = args.fetch_log or (out / 'fetch.log')
    legs: list[dict] = []

    if args.cmd in ('legb', 'all'):
        legs.append(leg_b(out))
    if args.cmd in ('lega', 'all'):
        ref = (out / 'runA-dump.json') if args.cmd == 'all' else None
        ref = ref if (ref and ref.exists()) else None
        legs.append(leg_a(out, reference_dump=ref, fetch_log=fetch_log))

    overall = write_verdicts(out, legs)
    for leg in legs:
        print(f'Leg {leg["leg"]} ({leg["name"]}): {leg["verdict"]}')
        for r in leg['reasons']:
            print(f'    - {r}')
    print(f'verdict -> {out}/verdict.json  (overall: {"PASS" if overall else "FAIL"})')
    if not overall:
        failed = [f'Leg {leg["leg"]}' for leg in legs if not leg['pass']]
        print(f'FAIL: {", ".join(failed)}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
