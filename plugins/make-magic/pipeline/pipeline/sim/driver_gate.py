"""Phase 2 -- the per-deck driver COMPILE + BEHAVIORAL validation gate.

A thin driver authored by :mod:`pipeline.sim.driver_authoring` is only trusted once
it clears two gates against the real XMage jar:

  1. **Compile gate** (:func:`compile_driver`): ``javac`` the ``Driver.java`` against
     the dist jar (which carries ``ComputerPlayer7`` -- a driver needs nothing outside
     the shaded jar, per Phase-0/W2). A non-zero ``javac`` exit raises
     :class:`DriverCompileError` with the compiler output and stamps NO meta.
  2. **Behavioral gate** (:func:`gate_driver`), both checks via the Phase-1 solo
     ``goldfish`` path:

       (a) **executes its owned line** -- the driven solo run's combined output must
           contain the ``DRIVER_LINE_FIRED`` marker (the driver ran its line; the
           standalone harness emits no ``comboFired`` static, so the marker is the
           only honest signal).
       (b) **never worse than a throwaway pure-CP7 baseline** -- the driven run's
           ``median_kills_own`` must be ``<=`` the driverless baseline's PLUS a small
           ``tolerance`` (a lower own-turn kill is faster ⇒ better; the ``-1.0`` "never
           killed" sentinel is treated as ``+inf`` so a no-kill run is the worst
           possible). The tolerance exists because XMage has NO reproducible seed, so
           two independent solo samples of the same deck jitter run-to-run -- measured at
           ~±1 own-turn on the Mikaeus deck (driven 8→9, baseline 8). A strict ``<=``
           would be a coin-flip at small ``games``; the default 2-turn tolerance
           (:data:`_DEFAULT_TOLERANCE`) absorbs that jitter while still rejecting a
           driver that is *meaningfully* slower or that stops killing.

     Pass ⇒ stamp ``meta.json`` with ``gates_passed=True`` (the driver becomes
     ``driver_valid``). Fail ⇒ do NOT stamp ``gates_passed=True`` -- the driver stays
     invalid and every caller falls back to the driverless path.

The gate NEVER downgrades silently: a compile failure raises; a behavioral failure
returns a :class:`GateResult` naming the failed check.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pipeline.decks.version import version
from pipeline.sim import drivers
from pipeline.sim.driver_authoring import DRIVER_LINE_FIRED_MARKER, driver_fqcn

if TYPE_CHECKING:
    from pipeline.contracts import Deck
    from pipeline.sim.engine import EngineInstall
    from pipeline.sim.engines.xmage import GoldfishResult

__all__ = (
    'DriverCompileError',
    'GateResult',
    'compile_driver',
    'gate_driver',
)

#: Own-turn-kill slack allowed on the never-worse check (b). XMage has no reproducible
#: seed, so the driven + baseline solo medians are two independent noisy samples that
#: jitter ~±1 own-turn between runs (empirically on the Mikaeus deck). 2 turns of slack
#: makes a neutral driver pass deterministically while a driver that is genuinely slower
#: (or that stops killing → the +inf sentinel) still fails.
_DEFAULT_TOLERANCE = 2.0


class DriverCompileError(RuntimeError):
    """``javac`` rejected the generated ``Driver.java`` (source is not compilable)."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class GateResult:
    """The behavioral gate's verdict for one driver.

    ``passed`` is the conjunction of the line-fired check and the never-worse check.
    ``reason`` names the failed check (empty on pass). ``line_fired`` records whether
    the ``DRIVER_LINE_FIRED`` marker was seen; ``driven_median`` / ``baseline_median``
    are the raw ``medianKillsOwn`` values compared (``-1.0`` = the harness "never
    killed" sentinel).
    """

    passed: bool
    reason: str = ''
    line_fired: bool = False
    driven_median: float | None = None
    baseline_median: float | None = None
    fqcn: str = ''
    extra: dict[str, object] = field(default_factory=dict)


class _GoldfishEngine(Protocol):
    """The slice of :class:`~pipeline.sim.engines.xmage.XMageEngine` the gate needs --
    a Protocol so tests can inject a fake with no JVM."""

    def goldfish_output(
        self,
        deck_a: tuple[str, str],
        *,
        games: int,
        install: EngineInstall,
        driver: tuple[str, str] | None = ...,
    ) -> tuple[GoldfishResult, str]: ...

    def goldfish(
        self,
        deck_a: tuple[str, str],
        *,
        games: int,
        install: EngineInstall,
        driver: tuple[str, str] | None = ...,
    ) -> GoldfishResult: ...


def _default_engine() -> _GoldfishEngine:
    """The registered XMage engine singleton (the real solo runner)."""
    from pipeline.sim.engine import get_engine

    return get_engine('xmage')  # type: ignore[return-value]


def _resolve_javac(install: EngineInstall, javac: str | os.PathLike[str] | None) -> str:
    """Pick the ``javac`` binary: explicit arg > sibling of the install's ``java`` > PATH.

    The compiled driver only needs to be loadable by the run JVM, so any JDK whose
    ``javac`` targets a bytecode version the run JRE accepts works. An explicit
    ``javac`` wins (the caller knows its toolchain); otherwise try a ``javac`` next to
    the resolved ``java``; otherwise fall back to a PATH lookup. A miss raises so the
    compile gate fails loudly rather than silently skipping compilation.
    """
    if javac is not None:
        return str(javac)
    java = getattr(install.handle, 'java', None)
    if java is not None:
        sibling = Path(java).parent / 'javac'
        if sibling.is_file():
            return str(sibling)
    found = shutil.which('javac')
    if found is not None:
        return found
    raise DriverCompileError(
        'no javac found (pass javac=, or put a JDK javac beside the install java / on PATH)'
    )


def compile_driver(
    deck: Deck,
    java_source: str,
    *,
    install: EngineInstall,
    javac: str | os.PathLike[str] | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Write + ``javac``-compile ``deck``'s ``Driver.java`` against the dist jar.

    Writes ``java_source`` to ``<driver_dir>/Driver.java``, compiles it with
    ``javac -cp <dist.jar> -d <classes_dir>`` (the shaded jar carries
    ``ComputerPlayer7`` -- nothing else is needed), and returns the ``classes_dir`` the
    run path prepends onto the classpath. The classes dir is wiped first so a stale
    prior ``.class`` can never linger. A non-zero ``javac`` exit raises
    :class:`DriverCompileError` with the compiler output; NO meta is stamped here (the
    behavioral gate owns the ``gates_passed`` stamp).
    """
    ddir = drivers.driver_dir(deck, data_dir=data_dir)
    ddir.mkdir(parents=True, exist_ok=True)
    src = ddir / 'Driver.java'
    src.write_text(java_source, encoding='utf-8')

    classes = drivers.classes_dir(deck, data_dir=data_dir)
    shutil.rmtree(classes, ignore_errors=True)  # no stale .class survives a re-author
    classes.mkdir(parents=True, exist_ok=True)

    javac_bin = _resolve_javac(install, javac)
    classpath = install.handle.classpath
    proc = subprocess.run(
        [javac_bin, '-cp', classpath, '-d', str(classes), str(src)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise DriverCompileError(
            f'javac failed for {src} (exit {proc.returncode}):\n'
            f'{proc.stdout}\n{proc.stderr}'.strip()
        )
    return classes


def _kill_metric(median_kills_own: float) -> float:
    """Map a ``medianKillsOwn`` to a comparable "faster is smaller" metric.

    A real kill turn compares directly (turn 6 beats turn 8). The harness ``-1.0``
    "never killed" sentinel (and any negative) becomes ``+inf`` so a no-kill run is
    strictly the worst -- a driver that stops killing can never pass the never-worse
    check against a baseline that does kill.
    """
    return math.inf if median_kills_own < 0 else median_kills_own


def gate_driver(
    deck: Deck,
    deck_ref: tuple[str, str],
    *,
    install: EngineInstall,
    games: int,
    tolerance: float = _DEFAULT_TOLERANCE,
    engine: _GoldfishEngine | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> GateResult:
    """Run the two behavioral checks on ``deck``'s compiled driver; stamp on pass.

    ``deck_ref`` is the ``(name, forge_dck_text)`` tuple the solo ``goldfish`` path
    consumes (kept an explicit arg so the gate does not couple to the deck exporter /
    network). Runs the DRIVEN solo (line-fired + median) then a throwaway DRIVERLESS
    CP7 baseline (median) over ``games`` games each, and:

      * (a) requires the ``DRIVER_LINE_FIRED`` marker in the driven output;
      * (b) requires the driven ``medianKillsOwn`` to be no worse than the baseline
        plus ``tolerance`` own-turns (via :func:`_kill_metric`, so the no-kill sentinel
        is worst); the tolerance absorbs XMage's seedless run-to-run jitter (see
        :data:`_DEFAULT_TOLERANCE`).

    On BOTH passing, stamps ``meta.json`` (``gates_passed=True``, plus the compared
    medians in ``extra``) so :func:`~pipeline.sim.drivers.driver_valid` turns True. On
    EITHER failing, stamps nothing and returns a failing :class:`GateResult` naming the
    check -- the caller falls back to driverless.
    """
    eng = engine if engine is not None else _default_engine()
    classes = drivers.classes_dir(deck, data_dir=data_dir)
    fqcn = driver_fqcn(deck)
    driver = (str(classes), fqcn)

    driven, output = eng.goldfish_output(deck_ref, games=games, install=install, driver=driver)
    line_fired = DRIVER_LINE_FIRED_MARKER in output
    baseline = eng.goldfish(deck_ref, games=games, install=install, driver=None)

    driven_median = driven.median_kills_own
    baseline_median = baseline.median_kills_own
    never_worse = _kill_metric(driven_median) <= _kill_metric(baseline_median) + tolerance

    if not line_fired:
        return GateResult(
            passed=False,
            reason=(
                f'driver never emitted {DRIVER_LINE_FIRED_MARKER} over {games} solo games '
                '(the owned line did not execute)'
            ),
            line_fired=False,
            driven_median=driven_median,
            baseline_median=baseline_median,
            fqcn=fqcn,
        )
    if not never_worse:
        return GateResult(
            passed=False,
            reason=(
                f'driven medianKillsOwn={driven_median} is WORSE than the CP7 baseline '
                f'{baseline_median} + tolerance {tolerance} (a driver must never meaningfully '
                'slow the deck down)'
            ),
            line_fired=True,
            driven_median=driven_median,
            baseline_median=baseline_median,
            fqcn=fqcn,
        )

    extra = {
        'gate_games': games,
        'gate_tolerance': tolerance,
        'gate_driven_median_kills_own': driven_median,
        'gate_baseline_median_kills_own': baseline_median,
    }
    drivers.write_meta(
        deck,
        drivers.DriverMeta(
            deck_version=version(deck),
            harness_version=drivers.harness_version(data_dir=data_dir),
            fqcn=fqcn,
            gates_passed=True,
            extra=extra,
        ),
        data_dir=data_dir,
    )
    return GateResult(
        passed=True,
        reason='',
        line_fired=True,
        driven_median=driven_median,
        baseline_median=baseline_median,
        fqcn=fqcn,
        extra=extra,
    )
