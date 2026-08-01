"""Run ONE AI-vs-AI matchup through headless Forge and tally the result.

:func:`run_matchup` stages two already-rendered ``.dck`` files into the Forge
profile decks dir, launches a single ``sim`` JVM from the Forge home (so ``res/``
resolves), enforces an EXTERNAL subprocess timeout + kill (Forge's own ``-c`` is
only the in-game draw clock), and hands the captured log to :func:`parse_match_log`.

Parsing is a pure function so it is unit-testable against real captured logs.
Every gotcha below is empirically grounded in ``~/mtg-sim-lab/forge_backend.py``
(Forge 2.0.13, Temurin 21):

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

import re
import shutil
import subprocess
from dataclasses import dataclass, field

from pipeline.contracts import Deck
from pipeline.destinations.deck_export import get_exporter, safe_deck_stem
from pipeline.sim.forge_runtime import ForgeInstall

__all__ = (
    'ForgeError',
    'GameOutcome',
    'MatchResult',
    'deck_to_dck',
    'parse_match_log',
    'run_matchup',
)

#: The ONLY line the tally counts: ``Game Result: Game N ended in <ms> ms. <tail>``.
_RESULT_RE = re.compile(r'^Game Result: Game \d+ ended in (\d+) ms\. (.+)$')
#: Winner tail: ``Ai(<slot>)-<name> has won!`` — slot 1 = deck_a, 2 = deck_b.
#: The name is matched non-greedily (``.+?``, NOT ``\S+``) so deck names with
#: spaces/parens (e.g. a real Airtable deck ``UR Izzet (Chaos Sealed)``) parse —
#: only the SLOT drives attribution, so the name span is irrelevant otherwise.
_WINNER_RE = re.compile(r'Ai\((\d)\)-.+? has won!')
#: exit-0 deck-load failures. Presence -> ForgeError regardless of exit code.
_LOAD_FAILURE_MARKERS = ('Could not load deck', 'No deck found in')

#: One-time card-DB load was 15-25s empirically; headroom for the external kill
#: on top of the caller's per-game timeout budget.
_JVM_LOAD_HEADROOM_S = 120

#: Base headless JVM args (shared across platforms). Xmx + DisableExplicitGC per
#: the empirical tuning; the platform headless flag is added in :func:`_jvm_args`.
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


def parse_match_log(output: str, *, deck_a: str, deck_b: str) -> MatchResult:
    """Tally a Forge ``sim`` log into a :class:`MatchResult` (pure function).

    Counts ONLY ``Game Result:`` lines (the ``Game Outcome:`` twin would
    double-count), mapping the ``Ai(1)``/``Ai(2)`` slot to ``deck_a``/``deck_b``.
    Raises :class:`ForgeError` on a deck-load failure marker (exit code is NOT
    reliable — a broken deck still exits 0) or when no ``Game Result`` line is
    present at all.
    """
    for marker in _LOAD_FAILURE_MARKERS:
        if marker in output:
            raise ForgeError(
                f'Forge could not load a deck ({marker!r} in output). '
                'Exit code is NOT reliable — a broken deck still exits 0.'
            )

    wins_a = wins_b = draws = 0
    per_game: list[GameOutcome] = []
    for raw_line in output.splitlines():
        m = _RESULT_RE.match(raw_line.strip())
        if not m:
            continue
        elapsed_ms = int(m.group(1))
        tail = m.group(2)
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
    :class:`~pipeline.contracts.Deck`). Each is written into
    ``install.decks_dir/<constructed|commander>/`` (Forge resolves ``-d`` against
    the profile dir, never absolute paths). The JVM runs from ``install.forge_dir``
    so ``res/`` resolves, VERBOSE (no ``-q``) so the log is captured for later
    telemetry, with an EXTERNAL timeout + kill on top of Forge's in-game ``-c``
    clock.

    Raises :class:`ForgeError` on a deck-load failure or an unparseable/empty log,
    and re-raises the external timeout as :class:`ForgeError` (the JVM is killed).
    """
    name_a, text_a = deck_a
    name_b, text_b = deck_b

    # Stage under FILESYSTEM-SAFE stems (the human name may contain '/' etc. and
    # is used only for display — it survives inside each .dck's `Name=`). Forge
    # resolves `-d <stem>` against the profile dir, so the stem drives both the
    # filename and the `-d` arg. Disambiguate the rare case where two distinct
    # decks sanitize to the same stem (e.g. 'A/B' and 'A:B' -> 'A_B').
    stem_a = safe_deck_stem(name_a)
    stem_b = safe_deck_stem(name_b)
    if stem_a == stem_b and text_a != text_b:
        stem_a, stem_b = f'{stem_a}__a', f'{stem_b}__b'

    fmt_dir = 'commander' if fmt == 'commander' else 'constructed'
    decks_dir = install.decks_dir / fmt_dir
    decks_dir.mkdir(parents=True, exist_ok=True)
    (decks_dir / f'{stem_a}.dck').write_text(text_a)
    (decks_dir / f'{stem_b}.dck').write_text(text_b)

    cmd = [
        *_launch_prefix(),
        str(install.java),
        *_jvm_args(),
        '-jar',
        str(install.jar),
        'sim',
        '-d',
        f'{stem_a}.dck',
        f'{stem_b}.dck',
        '-n',
        str(n),
        '-c',
        str(timeout_s),
        '-s',
        str(seed),
    ]
    if fmt == 'commander':
        cmd += ['-f', 'commander']

    # EXTERNAL kill-switch: Forge's -c is only the per-game draw clock, so bound
    # the whole JVM at one-time-load headroom + per-game budget across n games.
    external_timeout = _JVM_LOAD_HEADROOM_S + max(1, n) * timeout_s
    try:
        proc = subprocess.run(
            cmd,
            cwd=install.forge_dir,
            capture_output=True,
            text=True,
            timeout=external_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run already killed the child on timeout.
        raise ForgeError(
            f'Forge sim exceeded the external {external_timeout}s timeout and was killed ({name_a} vs {name_b}, n={n}).'
        ) from exc

    output = (proc.stdout or '') + (proc.stderr or '')
    result = parse_match_log(output, deck_a=name_a, deck_b=name_b)
    if result.games != n:
        raise ForgeError(
            f'expected {n} Game Result lines, got {result.games} '
            f'(exit {proc.returncode}). Output tail:\n{output[-1000:]}'
        )
    return result
