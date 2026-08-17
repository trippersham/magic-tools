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
import tempfile
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

    def capabilities(self) -> EngineCapabilities:
        return _XMAGE_CAPABILITIES

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

        staging = runner._staging_root()
        staging.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix='xmage-', dir=staging))
        try:
            txt_a = _stage_txt(run_dir, 'deckA', _forge_dck_to_xmage_txt(text_a))
            txt_b = _stage_txt(run_dir, 'deckB', _forge_dck_to_xmage_txt(text_b))
            cmd = [
                *runner._launch_prefix(),
                str(handle.java),
                *runner._jvm_args(),
                '-cp',
                handle.classpath,
                _XMAGE_MAIN_CLASS,
                str(txt_a),
                str(txt_b),
                str(n),
                str(_CP7_SKILL),
            ]
            # External kill-switch: the whole JVM is bounded at one-time load/DB-scan
            # headroom + per-game budget across n games. Own process group so the
            # kill reaps the whole tree (incl. any xvfb-run grandchild).
            external_timeout = runner._JVM_LOAD_HEADROOM_S + max(1, n) * timeout_s
            proc = subprocess.Popen(
                cmd,
                cwd=handle.mage_tests_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=external_timeout)
            except subprocess.TimeoutExpired as exc:
                runner._kill_process_group(proc)
                raise XMageError(
                    f'XMage exceeded the external {external_timeout}s timeout and was killed '
                    f'({name_a} vs {name_b}, n={n}).'
                ) from exc

            output = (stdout or '') + (stderr or '')
            result = runner.parse_match_log(output, deck_a=name_a, deck_b=name_b)
            if result.games != n:
                raise XMageError(
                    f'expected {n} Game Result lines, got {result.games} '
                    f'(exit {proc.returncode}). Output tail:\n{output[-1000:]}'
                )
            return result
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def replay(self, matchup_key: str, game_idx: int) -> str:
        """The stored verbose log for one game of a prior matchup (shared store)."""
        from pipeline.sim.store import get_game_logs

        logs = get_game_logs(matchup_key, game_index=game_idx)
        return logs[0] if logs else ''


def _stage_txt(run_dir: Path, name: str, text: str) -> Path:
    """Write a translated XMage deck to ``<run_dir>/<name>.txt`` and return its path."""
    path = run_dir / f'{name}.txt'
    path.write_text(text, encoding='utf-8')
    return path


register_engine(XMageEngine())
