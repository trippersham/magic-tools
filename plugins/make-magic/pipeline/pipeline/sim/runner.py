"""Run ONE AI-vs-AI matchup through headless Forge and tally the result.

:func:`run_matchup` stages two already-rendered ``.dck`` files into the Forge
profile decks dir, launches a single JVM running the committed sim-AI HARNESS
(``org.makemagic.simai.SimAIMatch`` with ``-sim 1`` — Forge's real simulation AI,
not the stock heuristic ``sim`` verb) from the Forge home (so ``res/`` resolves),
enforces an EXTERNAL subprocess timeout + kill (Forge's own ``-c`` is only the
in-game draw clock), and hands the captured log to :func:`parse_match_log`.

Parsing is a pure function so it is unit-testable against real captured logs.
Every gotcha below is empirically validated against Forge 2.0.13 + Temurin 21:

  * Count ONLY ``Game Result:`` lines. Each finished game ALSO prints a
    ``Game Outcome: … has won`` twin — tallying both DOUBLE-counts every win.
  * A missing/broken deck EXITS 0 and prints ``Could not load deck`` — success
    is judged by counting ``Game Result`` lines, never by exit code.
  * The winner maps by the ``Ai(1)``/``Ai(2)`` slot = the ``-d`` order, so
    ``deck_a`` is always ``Ai(1)`` and ``deck_b`` is ``Ai(2)``; deck names with
    spaces are safe.
  * Headless flags are platform-specific: macOS needs
    ``-Dapple.awt.UIElement=true``; a truly headless Linux host needs an
    ``xvfb-run`` wrapper. NEVER ``-Djava.awt.headless=true`` (silent exit 1).

Telemetry (kill-turn, per-turn parsing) is deliberately NOT extracted here — the
verbose log is captured whole in ``MatchResult.raw_log`` for a later phase.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.contracts import Deck
from pipeline.destinations.deck_export import get_exporter
from pipeline.sim._log_patterns import DRAW_RESULT_RE, RESULT_RE, WINNER_RE
from pipeline.sim.forge_runtime import ForgeInstall
from pipeline.store.paths import StorePaths

__all__ = (
    'ForgeError',
    'GameOutcome',
    'MatchResult',
    'deck_to_dck',
    'is_clockout_segment',
    'parse_match_log',
    'reap_stale_staging',
    'run_matchup',
    'staging_root',
)

#: The committed sim-AI harness jar (shipped package data), resolved off this
#: module's location — the SAME path the 1.5 presence guard uses
#: (``pipeline/sim/java/forge-simai/make-magic-forge-simai.jar``). It MUST precede
#: the Forge jar on the classpath so its ``StaticAbilityContinuous`` shadow wins
#: the class-load. Task 1.6c relocated it OUT of ``dist/`` (which the repo-root
#: ``**/dist/`` ignore was excluding from the wheel) so it ships normally.
_HARNESS_JAR = Path(__file__).parent / 'java' / 'forge-simai' / 'make-magic-forge-simai.jar'
#: The harness Main-Class (launched via ``-cp`` so the classpath ordering holds;
#: ``-jar`` would ignore the ``-cp`` and thus lose the shadow-first ordering).
_HARNESS_MAIN_CLASS = 'org.makemagic.simai.SimAIMatch'

#: The game-terminator + winner regex triplet is SHARED with
#: :mod:`pipeline.sim.telemetry` via :mod:`pipeline.sim._log_patterns` (one
#: definition, imported by both) so the tally and the telemetry segmentation can
#: never silently desync (R2-1 / R3-3). The module-local ``_``-prefixed aliases
#: keep the parse body below unchanged.
_RESULT_RE = RESULT_RE
_DRAW_RESULT_RE = DRAW_RESULT_RE
_WINNER_RE = WINNER_RE
#: exit-0 deck-load failures. Presence -> ForgeError regardless of exit code.
_LOAD_FAILURE_MARKERS = ('Could not load deck', 'No deck found in')
#: CLOCKOUT marker (``SimAIMatch.java:199``): a game that hit Forge's in-game draw
#: clock prints this, then forces ``GameEndReason.Draw`` — but the log's
#: reverse-printed ``Game Outcome`` block awards a DUAL ``has won because all
#: opponents have lost`` to BOTH players and the terminator still reads
#: ``… has won!``. That "win" is FABRICATED; a segment carrying this marker is
#: NON-DECISIVE (0W/0L/1 draw) and must be excluded from tally AND telemetry (B1b).
_CLOCKOUT_MARKER = 'Stopping slow match as draw'

#: One-time card-DB load was 15-25s empirically; headroom for the external kill
#: on top of the caller's per-game timeout budget.
_JVM_LOAD_HEADROOM_S = 120

#: Base headless JVM args (shared across platforms); the platform headless flag is
#: added in :func:`_jvm_args`. Empirically tuned against Forge 2.0.13:
#:   * ``-Xmx2g`` — a single Forge `sim` game fits comfortably in ~2 GiB; this caps
#:     each JVM so the governor can size the pool by ``free_ram / per_jvm_budget``.
#:   * ``-XX:+DisableExplicitGC`` — Forge calls ``System.gc()`` between games, which
#:     under a parallel pool causes stop-the-world stalls that wreck throughput
#:     (~2.8x better scaling + ~20% less RAM with it disabled). Load-bearing.
_BASE_JVM_ARGS = ('-Xmx2g', '-XX:+DisableExplicitGC')


class ForgeError(RuntimeError):
    """A Forge ``sim`` run produced no usable result (deck-load failure, no games,
    or an output that does not parse). Distinct from
    :class:`~pipeline.sim.forge_runtime.ForgeUnavailableError` (no install)."""


@dataclass(frozen=True)
class GameOutcome:
    """One game's result within a match: ``winner`` is ``'a'`` / ``'b'`` / ``'draw'``."""

    winner: str
    elapsed_ms: int


@dataclass(frozen=True)
class MatchResult:
    """The tallied outcome of one matchup + the raw verbose log.

    ``per_game`` preserves per-game order (winner + elapsed_ms). ``raw_log`` holds
    the full verbose stdout+stderr so a later telemetry phase can re-parse it
    without re-running the match.
    """

    deck_a: str
    deck_b: str
    wins_a: int
    wins_b: int
    draws: int
    per_game: tuple[GameOutcome, ...]
    raw_log: str = field(repr=False)

    @property
    def games(self) -> int:
        """Total decided + drawn games (== ``len(per_game)``)."""
        return self.wins_a + self.wins_b + self.draws


def deck_to_dck(deck: Deck) -> str:
    """Render a :class:`~pipeline.contracts.Deck` to ``.dck`` text via the Phase-1
    exporter — a thin convenience so callers can produce the text
    :func:`run_matchup` consumes. ``run_matchup`` itself stays decoupled (it takes
    already-rendered text), so this helper is optional."""
    return get_exporter('forge_dck').export(deck)


def is_clockout_segment(segment: str) -> bool:
    """True when a single game's log ``segment`` is a CLOCKOUT (non-decisive).

    A game that hit Forge's in-game draw clock prints ``Stopping slow match as
    draw`` (``SimAIMatch.java:199``) before its terminator; the harness then
    forces a draw yet the reverse-printed ``Game Outcome`` block awards a DUAL
    ``has won because all opponents have lost`` to BOTH players and the
    ``Game Result`` terminator still reads ``… has won!``. That decisive "win" is
    fabricated — the game is really a timeout non-decision. Detected by the
    unambiguous marker (equivalently the dual-win pair, used as a fallback in case
    the marker line is ever dropped from a truncated capture).
    """
    if _CLOCKOUT_MARKER in segment:
        return True
    # LOAD-BEARING (not a mere truncation backstop): the DUAL win — both slots
    # "has won because all opponents have lost" — is the ONLY signal for a fast
    # MARKER-LESS forced draw (Forge's `startGame` returns without a game-over →
    # the reverse-printed Game Outcome block awards BOTH players a win, but NO
    # "Stopping slow match as draw" marker line is emitted). Those games would
    # otherwise be mis-tallied as fabricated wins. This class is a real fraction
    # of the ~30% non-decisive rate (see forge.py `_FORGE_CAPABILITIES`, R2-2).
    return segment.count('has won because all opponents have lost') >= 2


def parse_match_log(output: str, *, deck_a: str, deck_b: str) -> MatchResult:
    """Tally a Forge ``sim`` log into a :class:`MatchResult` (pure function).

    Counts ONLY ``Game Result:`` lines (the ``Game Outcome:`` twin would
    double-count), mapping the ``Ai(1)``/``Ai(2)`` slot to ``deck_a``/``deck_b``.
    The tally is GAME-SEGMENT-AWARE: a game whose segment carries the CLOCKOUT
    marker (:func:`is_clockout_segment`) is NON-DECISIVE — its fabricated
    ``… has won!`` terminator is NOT counted as a win; it is tallied as a draw
    (0W/0L, +1 draw) so a clocked-out game can never masquerade as a real result
    (B1b). A GENUINE draw terminator (``… ended in a Draw! Took <ms> ms.``,
    ``SimAIMatch.java:219``) is likewise a draw (M4). Raises :class:`ForgeError`
    on a deck-load failure marker (exit code is NOT reliable — a broken deck still
    exits 0) or when no ``Game Result`` line is present at all.
    """
    for marker in _LOAD_FAILURE_MARKERS:
        if marker in output:
            raise ForgeError(
                f'Forge could not load a deck ({marker!r} in output). '
                'Exit code is NOT reliable — a broken deck still exits 0.'
            )

    wins_a = wins_b = draws = 0
    per_game: list[GameOutcome] = []
    # Accumulate each game's lines up to (and including) its ``Game Result``
    # terminator, so the clockout marker earlier in the same segment is visible
    # when the terminator is tallied (the marker precedes the reverse-printed
    # ``Game Outcome`` block and the terminator).
    segment: list[str] = []
    for raw_line in output.splitlines():
        segment.append(raw_line)
        stripped = raw_line.strip()
        draw_m = _DRAW_RESULT_RE.match(stripped)
        if draw_m:
            # A genuine harness-emitted draw terminator (M4).
            draws += 1
            per_game.append(GameOutcome(winner='draw', elapsed_ms=int(draw_m.group(1))))
            segment = []
            continue
        m = _RESULT_RE.match(stripped)
        if not m:
            continue
        elapsed_ms = int(m.group(1))
        tail = m.group(2)
        if is_clockout_segment('\n'.join(segment)):
            # CLOCKOUT: discard the fabricated ``has won!`` — count as a draw (B1b).
            draws += 1
            winner = 'draw'
        else:
            winner_match = _WINNER_RE.search(tail)
            if winner_match:
                if winner_match.group(1) == '1':
                    wins_a += 1
                    winner = 'a'
                else:
                    wins_b += 1
                    winner = 'b'
            elif 'Draw' in tail:
                draws += 1
                winner = 'draw'
            else:
                raise ForgeError(f'unparseable Game Result line: {raw_line!r}')
        per_game.append(GameOutcome(winner=winner, elapsed_ms=elapsed_ms))
        segment = []

    if not per_game:
        raise ForgeError(f'Forge produced no Game Result lines (no games played). Output tail:\n{output[-1000:]}')

    return MatchResult(
        deck_a=deck_a,
        deck_b=deck_b,
        wins_a=wins_a,
        wins_b=wins_b,
        draws=draws,
        per_game=tuple(per_game),
        raw_log=output,
    )


def _jvm_args() -> tuple[str, ...]:
    """Base + platform headless JVM args.

    macOS: ``-Dapple.awt.UIElement=true`` (background-agent AWT; the ONLY reliable
    headless flag — ``-Djava.awt.headless=true`` makes Forge exit 1 silently).
    Other platforms rely on an ``xvfb-run`` wrapper (see :func:`_launch_prefix`).
    """
    import platform

    args: list[str] = list(_BASE_JVM_ARGS)
    if platform.system() == 'Darwin':
        args.append('-Dapple.awt.UIElement=true')
    return tuple(args)


def _launch_prefix() -> list[str]:
    """A wrapper prefixed before ``java`` on a headless host.

    On Linux, Forge's AWT init needs a display; wrap with ``xvfb-run`` when
    available. macOS uses the UIElement flag instead, so no wrapper. If a Linux
    host lacks ``xvfb-run`` we return no prefix and let the JVM surface the error
    (documented limitation — install xvfb for headless Linux).
    """
    import platform

    if platform.system() == 'Linux' and shutil.which('xvfb-run'):
        return ['xvfb-run', '-a']
    return []


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the JVM's whole process group, then reap it.

    The child was started with ``start_new_session=True``, so its pid IS its
    process-group id and ``killpg`` takes out every descendant (the JVM itself
    when a launch prefix like ``xvfb-run`` made it a grandchild). The final
    ``communicate`` reaps the child and drains the now-closed pipes — without
    it a surviving grandchild holding the pipe would block the caller forever.
    """
    if hasattr(os, 'killpg'):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    proc.kill()  # belt-and-braces for the direct child on any platform.
    proc.communicate()


def staging_root() -> Path:
    """The isolated root under which each run's ``.dck`` files are staged.

    Lives UNDER the data dir (``<data_dir>/sim/staging/``, honoring
    ``MAKE_MAGIC_DATA_DIR``) so staged decks never touch the real per-platform
    Forge profile dir (``~/Library/Application Support/Forge/decks/``) — the
    isolation leak R3-2 closes. Falls back to the OS temp dir if the data-dir
    lookup fails for any reason (staging must never be the thing that breaks a run).
    """
    try:
        return StorePaths.resolve().data_dir / 'sim' / 'staging'
    except Exception:
        # Staging location must degrade, never crash a run — fall back to OS temp.
        return Path(tempfile.gettempdir()) / 'make-magic-sim-staging'


#: A per-run staging dir older than this is presumed ORPHANED — its process died
#: before the in-run ``finally`` cleanup (a SIGKILL / OOM-killer / power loss /
#: crash), so it is swept on the next batch's start. Comfortably longer than any
#: real matchup batch, so a concurrently-running sim's fresh dirs are never touched.
_STAGING_MAX_AGE_S = 3600.0


def reap_stale_staging(max_age_s: float = _STAGING_MAX_AGE_S) -> int:
    """Sweep orphaned per-run staging dirs older than ``max_age_s``; return the count.

    The per-run ``finally`` rmtree is bypassed by a SIGKILL (a resource watchdog, the
    OOM-killer, power loss, a crash), so a killed run leaves its staging dir behind.
    On a NON-COW filesystem those orphans are FULL card-DB copies that accumulate
    across crashed runs and compound disk pressure — the very failure the per-run
    private-db staging exists to prevent. Called at batch start; best-effort and
    NEVER raises (reaping must not break a run). Touches only this module's own
    ``run-*`` / ``xmage-*`` staging dirs, and only those past the age cutoff (so a
    concurrent sim's in-flight dirs are safe).
    """
    root = staging_root()
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    now = time.time()
    reaped = 0
    for entry in entries:
        if not (entry.name.startswith('run-') or entry.name.startswith('xmage-')):
            continue
        try:
            if now - entry.stat().st_mtime < max_age_s:
                continue
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        reaped += 1
    return reaped


def _stage_dck(run_dir: Path, text: str) -> Path:
    """Write ``text`` to a COLLISION-PROOF file under ``run_dir``; return its abs path.

    The filename is CONTENT-ADDRESSED — a short sha256 of the ``.dck`` text — so two
    decks that would otherwise sanitize to the same stem (the concurrent
    cross-contamination hazard R3-2 closes) land on DISTINCT paths, while identical
    text (a mirror match) harmlessly resolves to the same file. The path is absolute
    (the harness reads ``-d`` as a filesystem path, not a Forge-profile stem).
    """
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
    path = run_dir / f'{digest}.dck'
    path.write_text(text)
    return path


def run_matchup(
    install: ForgeInstall,
    deck_a: tuple[str, str],
    deck_b: tuple[str, str],
    *,
    n: int,
    seed: int,
    fmt: str = 'constructed',
    timeout_s: int = 30,
) -> MatchResult:
    """Run ONE matchup of ``n`` games: ``deck_a`` (Ai(1)) vs ``deck_b`` (Ai(2)).

    ``deck_a`` / ``deck_b`` are ``(name, dck_text)`` pairs — already-rendered
    ``.dck`` content (use :func:`deck_to_dck` to produce it from a
    :class:`~pipeline.contracts.Deck`). Each is staged into a PER-RUN ISOLATED dir
    under the data dir (``<data_dir>/sim/staging/<run>/``) with a CONTENT-ADDRESSED
    filename, NOT into the real Forge profile decks dir — so concurrent runs never
    cross-contaminate and no artifacts accumulate in Forge's own tree (R3-2). The
    harness reads ``-d`` as a filesystem path (``DeckSerializer.fromFile``), so the
    ABSOLUTE staged paths are passed. The staged dir is cleaned up best-effort after
    the run. The JVM runs from ``install.forge_dir`` so ``res/`` resolves, VERBOSE
    (no ``-q``) so the log is captured for later telemetry, with an EXTERNAL timeout
    + kill on top of Forge's in-game ``-c`` clock.

    Raises :class:`ForgeError` on a deck-load failure or an unparseable/empty log,
    and re-raises the external timeout as :class:`ForgeError` (the JVM is killed).
    """
    name_a, text_a = deck_a
    name_b, text_b = deck_b

    # ISOLATED per-run staging: a fresh mkdtemp under the data dir keeps concurrent
    # runs from sharing a filename namespace, and CONTENT-ADDRESSED names within it
    # keep two distinct decks that sanitize to the same stem from clobbering each
    # other (both R3-2 hazards). The whole dir is removed in the `finally` below.
    root = staging_root()
    root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix='run-', dir=root))
    try:
        dck_a = _stage_dck(run_dir, text_a)
        dck_b = _stage_dck(run_dir, text_b)

        # Launch the sim-AI HARNESS (not the stock `sim` verb): its built-in Forge
        # simulation AI (AIOption.USE_SIMULATION, `-sim 1`) is the real decision engine.
        # Classpath ordering is load-bearing — the harness jar MUST come first so its
        # `StaticAbilityContinuous` shadow shadows Forge's crash-prone original; hence
        # `-cp <harness>:<forge>` + explicit Main-Class, NOT `-jar` (which ignores -cp).
        # No `-s seed`: the harness takes no seed (Forge's seed is non-reproducible
        # anyway); the seed lives in the matchup_key for cache identity / per-opponent
        # offset only. `-d` takes ABSOLUTE staged paths (the harness reads the file
        # directly rather than resolving a profile stem like the stock `sim` verb did).
        # Fail LOUDLY (not into the silent 0-0-0 table) if the shipped harness jar is
        # missing — a broken install (wheel that dropped the jar) would otherwise launch
        # the JVM against a phantom classpath entry, hit `Could not find or load main
        # class`, and zero out to a spurious ForgeError with no actionable cause (M1).
        if not _HARNESS_JAR.is_file():
            raise ForgeError(
                f'sim-AI harness jar not found: {_HARNESS_JAR}\n'
                'The Forge sim engine cannot run without it. If this is an installed '
                'package the wheel is broken (the jar was not shipped); reinstall a '
                'complete build. To rebuild it from source run:\n'
                '  pipeline/sim/java/forge-simai/build.sh'
            )
        classpath = os.pathsep.join((str(_HARNESS_JAR), str(install.jar)))
        cmd = [
            *_launch_prefix(),
            str(install.java),
            *_jvm_args(),
            '-cp',
            classpath,
            _HARNESS_MAIN_CLASS,
            '-d',
            str(dck_a),
            str(dck_b),
            '-n',
            str(n),
            '-c',
            str(timeout_s),
            '-sim',
            '1',
        ]
        if fmt == 'commander':
            cmd += ['-f', 'commander']

        # EXTERNAL kill-switch: Forge's -c is only the per-game draw clock, so bound
        # the whole JVM at one-time-load headroom + per-game budget across n games.
        # start_new_session puts the child in its OWN process group so the timeout
        # kill can reap the whole tree — under an `xvfb-run` prefix the JVM is a
        # GRANDCHILD, and killing only the direct child would leak it.
        external_timeout = _JVM_LOAD_HEADROOM_S + max(1, n) * timeout_s
        proc = subprocess.Popen(
            cmd,
            cwd=install.forge_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=external_timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(proc)
            raise ForgeError(
                f'Forge sim exceeded the external {external_timeout}s timeout and was killed '
                f'({name_a} vs {name_b}, n={n}).'
            ) from exc

        output = (stdout or '') + (stderr or '')
        result = parse_match_log(output, deck_a=name_a, deck_b=name_b)
        if result.games != n:
            raise ForgeError(
                f'expected {n} Game Result lines, got {result.games} '
                f'(exit {proc.returncode}). Output tail:\n{output[-1000:]}'
            )
        return result
    finally:
        # Best-effort cleanup — a leftover staged dir must never fail a completed run.
        shutil.rmtree(run_dir, ignore_errors=True)
