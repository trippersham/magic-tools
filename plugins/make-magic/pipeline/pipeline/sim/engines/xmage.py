"""The XMage :class:`~pipeline.sim.engine.SimEngine` — a co-equal second backend.

Runs a headless XMage ComputerPlayer7-vs-ComputerPlayer7 game via the committed
harness (``pipeline/sim/java/xmage/``: ``XMageBatch`` + the ``mage.collectors``
shadow), which emits the SAME line contract as the Forge harness — so
:func:`pipeline.sim.runner.parse_match_log` and the whole telemetry parser consume
its output UNCHANGED. XMage is a drop-in: identical ``MatchResult`` /
``GameFeatures`` / ``PilotingProfile``, with the differentiator that CP7 actually
casts counters (Forge's sim-AI is counter-blind).

Registered on import (bottom of the module), so ``--engine xmage`` + the
registry-driven ``doctor`` pick it up. The install is resolved from a local built
reactor (``MAKE_MAGIC_XMAGE_HOME``) — see :mod:`pipeline.sim.xmage_runtime`; the
shaded distributable jar is task 2.3b.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import threading
import warnings
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline.sim import runner, xmage_runtime
from pipeline.sim.engine import EngineCapabilities, EngineInstall, EngineUnavailableError, register_engine
from pipeline.sim.xmage_runtime import XMAGE_VERSION, XMageInstall, XMageUnavailableError

if TYPE_CHECKING:
    from pipeline.sim.runner import MatchResult

_XMAGE_MAIN_CLASS = 'org.makemagic.xmage.XMageBatch'
#: ComputerPlayer7 minimax skill/depth — the lab-standard level (6).
_CP7_SKILL = 6
#: Per-game budget. XMage games run to a DECISIVE result (no draw clock like
#: Forge's ``-c``), and a control grind can run long — so the bound is the external
#: kill only. Generous; a stalled game (rare) is killed and surfaced as a failure.
_DEFAULT_TIMEOUT_S = 240
#: ``XMageBatch --warm`` argument: build/verify the H2 card DB in ONE process.
_WARM_ARG = '--warm'
#: Bound on the one-time cold H2 build (a from-scratch CardScanner.scan can take a
#: while); a hung warm is killed + surfaced rather than wedging the pool forever.
_WARM_TIMEOUT_S = 420

_XMAGE_CAPABILITIES = EngineCapabilities(
    has_hand_visibility=True,
    has_counter_metrics=True,
    # XMage plays to a decisive result; draws are rare (unlike Forge's ~30% sim-AI
    # non-decisive rate), so an anomalously high rate is a red flag here.
    expected_nondecisive_rate=0.02,
    reliability_note=(
        'XMage ComputerPlayer7 (MAD minimax) via the committed XMageBatch harness, real 7-card hands + '
        'mulligans (testMode=false), emitting the Forge-compatible HANDLOG + Turn:/Life:/Damage:/Game '
        'Result: contract so the same parser applies. CP7 actively casts counters + sequences interaction '
        '(the control-piloting strength Forge lacks). Games run to a decisive result (no draw clock), so '
        'non-decisive is rare; a long control mirror can grind for a minute+, bounded by the external '
        'kill. Runs against a LOCAL built XMage reactor (MAKE_MAGIC_XMAGE_HOME); constructed only for now.'
    ),
    # XMage attributes a combat kill to a generic "combat" hit, not a specific named
    # source — downstream MUST NOT read a named killer from an XMage result.
    kill_attribution='combat_generic',
)


class XMageError(RuntimeError):
    """An XMage run failed (deck-load / unparseable output / killed on timeout)."""


@lru_cache(maxsize=1)
def _harness_jarhash() -> str:
    """Stable short sha256 of the committed XMage harness jar (binds the version to
    the actual harness build, so a harness change busts the content cache). Missing
    jar → ``'unknown'`` rather than crashing the version lookup."""
    try:
        return hashlib.sha256(xmage_runtime._HARNESS_JAR.read_bytes()).hexdigest()[:10]
    except OSError:
        return 'unknown'


def _engine_version() -> str:
    """``<XMAGE_VERSION>+cp7-<jarhash>`` — folded into the matchup_key cache identity."""
    return f'{XMAGE_VERSION}+cp7-{_harness_jarhash()}'


def _forge_dck_to_xmage_txt(text: str) -> str:
    """Translate a Forge ``.dck`` to an XMage plain ``N Cardname`` deck.

    A Forge ``.dck`` is ``[metadata]`` / ``[Main]`` / ``[Sideboard]`` sections whose
    ``[Main]`` lines are already ``N Cardname`` — exactly XMage's ``.txt`` format
    (``DeckImporter`` reads ``.txt``). Keep ONLY the maindeck card lines. (Verified:
    the shipped guilds decks load + play in XMage 1.4.60 with no card-not-found.)
    """
    lines: list[str] = []
    in_main = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith('['):
            in_main = stripped.lower() == '[main]'
            continue
        if in_main and stripped:
            lines.append(stripped)
    return '\n'.join(lines) + '\n'


class XMageEngine:
    """A :class:`~pipeline.sim.engine.SimEngine` that runs matchups via XMage CP7."""

    name = 'xmage'

    def __init__(self) -> None:
        # Per-reactor warm memo, held on the instance (the engine is a registered
        # singleton). The FIRST run_matchup across the governor's parent-process
        # ThreadPoolExecutor builds the canonical H2 card DB while its siblings block
        # on the lock; every later call is a near-free set lookup keyed by the
        # reactor's Mage.Tests dir. Instance state (not module globals) gives tests
        # isolation without a reset fixture.
        self._warm_lock = threading.Lock()
        self._warmed_dirs: set[str] = set()
        #: Memoized COW-probe result per '<reactor>-><staging>' pair (the _stage_private_db
        #: clone path). Guarded by _warm_lock. See :meth:`max_concurrency`.
        self._cow_probe: dict[str, bool] = {}

    def capabilities(self) -> EngineCapabilities:
        return _XMAGE_CAPABILITIES

    def max_concurrency(self, install: EngineInstall) -> int | None:
        """SERIALIZE (cap=1) when the per-run private-db copy would be a FULL real copy.

        :func:`_stage_private_db` prefers a copy-on-write clone (near-free) of the
        canonical card DB, but falls back to a full ~266 MB copy on a non-COW staging
        volume OR when the reactor DB and staging are on DIFFERENT volumes (clonefile
        is intra-volume). There a parallel pool would burn ``pool x 266 MB`` of REAL
        disk — the reboot-class exhaustion this guards (#61). Serializing bounds the
        peak to ONE copy at a time (cleaned per-run), so even a low-free-disk box stays
        safe. On a COW path returns ``None`` (no cap → full parallelism; clones are
        ~free). Probed once + WARNed once per (reactor, staging) pair; the governor
        clamps its RAM-derived pool by this.
        """
        handle: XMageInstall = install.handle
        src = handle.mage_tests_dir
        dst = runner.staging_root()
        key = f'{src}->{dst}'
        with self._warm_lock:
            cow = self._cow_probe.get(key)
            if cow is None:
                cow = _probe_cow(src, dst)
                self._cow_probe[key] = cow
                if not cow:
                    warnings.warn(
                        'XMage: the staging volume does not support copy-on-write clones (a '
                        'non-reflink filesystem, or the reactor DB and staging on different volumes) '
                        '— SERIALIZING matchups so the per-run ~266 MB card-DB copy cannot exhaust '
                        'disk. A COW volume (APFS/btrfs/xfs, same mount) restores full parallelism.',
                        stacklevel=2,
                    )
        return None if cow else 1

    def resolve(self, *, provision: bool, data_dir: Path | None = None) -> EngineInstall:
        """Resolve the local XMage reactor install, wrapped in an :class:`EngineInstall`.

        XMage is not auto-provisionable (no upstream fat jar to fetch), so
        ``provision`` is ignored — a missing reactor raises
        :class:`~pipeline.sim.engine.EngineUnavailableError` with the how-to-enable
        message (the seam's never-crash contract).
        """
        del provision
        try:
            handle = xmage_runtime.resolve(data_dir=data_dir)
        except XMageUnavailableError as exc:
            raise EngineUnavailableError(str(exc)) from exc
        return EngineInstall(version=_engine_version(), handle=handle)

    def run_matchup(
        self,
        deck_a: tuple[str, str],
        deck_b: tuple[str, str],
        *,
        n: int,
        seed: int,
        fmt: str,
        install: EngineInstall,
        timeout_s: int | None = None,
    ) -> MatchResult:
        """Run ONE matchup of ``n`` games: ``deck_a`` (Ai(1)/PlayerA) vs ``deck_b``.

        Translates each Forge ``.dck`` to an XMage ``.txt`` (staged, isolated per-run),
        launches ``XMageBatch`` from ``Mage.Tests`` (so the H2 card DB resolves) with an
        EXTERNAL timeout + process-group kill, and parses the Forge-format output with
        the shared :func:`~pipeline.sim.runner.parse_match_log`. ``seed`` is part of the
        cache identity only — XMage (like the Forge harness) takes no reproducible seed.
        Commander is not yet supported (constructed only).
        """
        handle: XMageInstall = install.handle
        if fmt == 'commander':
            raise EngineUnavailableError('the XMage engine supports constructed only (commander is a follow-up).')
        if timeout_s is None:
            timeout_s = _DEFAULT_TIMEOUT_S
        name_a, text_a = deck_a
        name_b, text_b = deck_b
        del seed  # cache-key identity only; XMage has no reproducible seed.

        if not xmage_runtime._HARNESS_JAR.is_file():
            raise XMageError(
                f'XMage harness jar not found: {xmage_runtime._HARNESS_JAR}. '
                'Rebuild it with pipeline/sim/java/xmage/build.sh.'
            )

        # Build the CANONICAL H2 card DB ONCE, single-process, before any game JVM
        # touches it — the cold CardScanner.scan() is not concurrency-safe.
        self._ensure_card_db_warm(handle)

        staging = runner.staging_root()
        staging.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix='xmage-', dir=staging))
        try:
            txt_a = _stage_txt(run_dir, 'deckA', _forge_dck_to_xmage_txt(text_a))
            txt_b = _stage_txt(run_dir, 'deckB', _forge_dck_to_xmage_txt(text_b))
            # Give THIS JVM a PRIVATE copy of the warm card DB and launch it from
            # ``run_dir`` (the H2 url is ``./db/cards.h2`` relative to cwd). XMage's
            # db opens ``AUTO_SERVER=TRUE`` and runs a ``createTableIfNotExists``
            # health-check on EVERY open — so parallel JVMs sharing one db/ file race
            # the server-handshake + table-check (intermittent ``expansionDao null`` /
            # a corrupted db), which a single warm-up alone does NOT prevent. A private
            # per-run db (the game JVM needs only ``db/`` in its cwd — verified) gives
            # total isolation: zero cross-JVM contention.
            _stage_private_db(handle, run_dir)
            # The JVM is bounded at one-time load/DB-scan headroom + the per-game
            # budget across n games; launched from run_dir so its H2 db is the private
            # copy (see _stage_private_db).
            external_timeout = runner._JVM_LOAD_HEADROOM_S + max(1, n) * timeout_s
            output, returncode = _launch_xmage(
                handle,
                [str(txt_a), str(txt_b), str(n), str(_CP7_SKILL)],
                cwd=run_dir,
                timeout_s=external_timeout,
                what=f'{name_a} vs {name_b} (n={n})',
            )
            result = runner.parse_match_log(output, deck_a=name_a, deck_b=name_b)
            if result.games != n:
                raise XMageError(
                    f'expected {n} Game Result lines, got {result.games} '
                    f'(exit {returncode}). Output tail:\n{output[-1000:]}'
                )
            return result
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def replay(self, matchup_key: str, game_idx: int) -> str:
        """The stored verbose log for one game of a prior matchup (shared store)."""
        from pipeline.sim.store import get_game_logs

        logs = get_game_logs(matchup_key, game_index=game_idx)
        return logs[0] if logs else ''

    def _ensure_card_db_warm(self, handle: XMageInstall) -> None:
        """Build the reactor's CANONICAL H2 card DB ONCE, before any per-run copy is made.

        XMage's ``CardScanner.scan()`` runs in every ``XMageBatch`` JVM, but a COLD
        (from-scratch) build is not concurrency-safe. This runs a single ``XMageBatch
        --warm`` process to complete the build once, so :func:`_stage_private_db` has a
        complete db to copy per run (a cold rebuild per matchup would otherwise be needed
        — and would race). Isolation from the concurrent-OPEN race is provided by the
        per-run private copy; this step just makes that copy cheap + guarantees complete.

        Serialized + memoized by ``self._warm_lock`` / ``self._warmed_dirs`` (keyed by the
        ``Mage.Tests`` dir): the FIRST ``run_matchup`` across the pool warms while the
        siblings block on the lock, then every later call is a near-free set lookup. A
        warm FAILURE does not poison the memo — it propagates as the caller's matchup
        failure and the next matchup retries (a transient race self-heals; a persistent
        break surfaces per-matchup and the run's exit code reflects the failure rate).
        """
        key = str(handle.mage_tests_dir)
        with self._warm_lock:
            if key in self._warmed_dirs:
                return
            self._run_warm_scan(handle)
            self._warmed_dirs.add(key)

    def _run_warm_scan(self, handle: XMageInstall) -> None:
        """Run one ``XMageBatch --warm`` JVM (cwd = Mage.Tests) to build the H2 card DB.

        A non-zero exit or timeout raises :class:`XMageError` (via :func:`_launch_xmage`)
        so the caller's matchup fails loudly rather than proceeding into the cold-scan race.
        """
        output, returncode = _launch_xmage(
            handle, [_WARM_ARG], cwd=handle.mage_tests_dir, timeout_s=_WARM_TIMEOUT_S, what='card-DB warm-up'
        )
        if returncode != 0:
            raise XMageError(f'XMage card-DB warm-up failed (exit {returncode}). Output tail:\n{output[-1000:]}')


def _stage_txt(run_dir: Path, name: str, text: str) -> Path:
    """Write a translated XMage deck to ``<run_dir>/<name>.txt`` and return its path."""
    path = run_dir / f'{name}.txt'
    path.write_text(text, encoding='utf-8')
    return path


def _stage_private_db(handle: XMageInstall, run_dir: Path) -> None:
    """Copy the canonical warm card DB into ``run_dir/db`` for this JVM's exclusive use.

    XMage's H2 url is ``./db/cards.h2`` (relative to cwd) opened ``AUTO_SERVER=TRUE``
    with a ``createTableIfNotExists`` health-check on every open — so pool JVMs that
    share one ``db/`` race the server handshake + table check (the ``expansionDao
    null`` / corrupted-db failures). Launching each JVM from ``run_dir`` with its OWN
    ``db/`` copy removes the shared state entirely. Only ``db/`` is needed in the cwd
    (verified: a game JVM plays a full duel from a bare cwd + db copy).
    """
    src = handle.mage_tests_dir / 'db'
    if not src.is_dir():
        raise XMageError(
            f'XMage card DB not found at {src} after warm-up — the reactor db was not built. '
            'Rebuild the reactor / re-run pipeline/sim/java/xmage/build.sh.'
        )
    dst = run_dir / 'db'
    # The card db is large (a cold CardScanner.scan builds a multi-hundred-MB H2
    # store), so a full copy per matchup is wasteful. Prefer a copy-on-write clone
    # (near-instant, shares blocks until written — APFS / btrfs / xfs); each JVM only
    # writes a little (the open-time table check + trace), so the clone stays mostly
    # shared. Fall back to a full copy on any FS that can't reflink.
    if _clone_tree_cow(src, dst):
        return
    shutil.rmtree(dst, ignore_errors=True)  # clear any partial clone before the copy.
    shutil.copytree(src, dst)


def _probe_cow(src_dir: Path, dst_dir: Path) -> bool:
    """True iff a copy-on-write clone works from ``src_dir``'s volume to ``dst_dir``'s.

    Mirrors the real :func:`_stage_private_db` path (reactor db → staging) with a tiny
    probe file, using reflink-ALWAYS / clonefile (``cp -c`` on macOS, ``cp
    --reflink=always`` on Linux) — which FAIL on a non-reflink filesystem OR across
    volumes, unlike ``--reflink=auto`` (which silently falls back to a full copy). So a
    ``True`` return means the per-run db copy will be near-free; ``False`` means it
    would be a full ~266 MB copy (→ serialize, #61). Never raises; cleans up the probe.
    """
    try:
        src_dir.mkdir(parents=True, exist_ok=True)
        dst_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    probe = src_dir / '.mm-cow-probe'
    clone = dst_dir / '.mm-cow-probe.clone'
    try:
        probe.write_bytes(b'\0' * 4096)
        cmd = (
            ['cp', '-c', str(probe), str(clone)]
            if sys.platform == 'darwin'
            else ['cp', '--reflink=always', str(probe), str(clone)]
        )
        return subprocess.run(cmd, capture_output=True, check=False).returncode == 0
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)
        clone.unlink(missing_ok=True)


def _clone_tree_cow(src: Path, dst: Path) -> bool:
    """Best-effort copy-on-write clone of ``src`` → ``dst`` (``dst`` must not exist).

    Uses the platform ``cp`` reflink path — ``cp -Rc`` (clonefile) on macOS/APFS,
    ``cp -a --reflink=auto`` on Linux (COW where the FS supports it, a plain copy
    otherwise). Returns ``True`` on success; ``False`` (→ caller falls back to a full
    :func:`shutil.copytree`) on a non-COW filesystem or any error — never raises.
    """
    if sys.platform == 'darwin':
        cmd = ['cp', '-Rc', str(src), str(dst)]  # clonefile(2) on APFS.
    else:
        cmd = ['cp', '-a', '--reflink=auto', str(src), str(dst)]  # GNU coreutils COW-or-copy.
    try:
        return subprocess.run(cmd, capture_output=True, check=False).returncode == 0
    except OSError:
        return False


def _launch_xmage(handle: XMageInstall, args: list[str], *, cwd: Path, timeout_s: int, what: str) -> tuple[str, int]:
    """Launch ONE ``XMageBatch`` JVM and return ``(combined stdout+stderr, returncode)``.

    The single place the game-run and warm-up paths build the launch command and
    enforce the kill contract: the JVM gets its OWN process group (``start_new_session``)
    so a timeout kill reaps the whole tree (incl. any ``xvfb-run`` grandchild), and a
    :class:`subprocess.TimeoutExpired` kills the group and raises :class:`XMageError`
    naming ``what``. Callers own the post-run interpretation (parse the log vs check
    the exit code) — this only owns launch + the external timeout. The H2 db is
    ``./db`` relative to ``cwd``, so ``cwd`` selects which db the JVM opens.
    """
    cmd = [
        *runner._launch_prefix(),
        str(handle.java),
        *runner._jvm_args(),
        '-cp',
        handle.classpath,
        _XMAGE_MAIN_CLASS,
        *args,
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        runner._kill_process_group(proc)
        raise XMageError(f'XMage {what} exceeded the external {timeout_s}s timeout and was killed.') from exc
    return (stdout or '') + (stderr or ''), proc.returncode


register_engine(XMageEngine())
