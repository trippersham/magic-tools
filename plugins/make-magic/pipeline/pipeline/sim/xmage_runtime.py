"""Resolve a local XMage install for the sim engine (mirrors :mod:`forge_runtime`).

XMage is a multi-module Maven app — unlike Forge, upstream publishes no single
fat jar to fetch. So (until the shaded-jar distribution build, task 2.3b) the
XMage engine runs against a LOCAL built reactor pointed to by
``MAKE_MAGIC_XMAGE_HOME``: a checked-out + built XMage clone (``mvn -pl Mage.Tests
-am install -DskipTests``). ``pipeline/sim/java/xmage/build.sh`` compiles the small
harness jar AND caches the reactor's transitive classpath to
``<home>/make-magic-xmage-classpath.txt`` — so resolution here is a fast,
network-free read (no ``mvn`` at run time).

Everything FAILS OPEN into a clear :class:`XMageUnavailableError` (re-raised as
``EngineUnavailableError`` by the engine) — never a traceback — so ``doctor`` and
a missing-install run stay actionable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pipeline.sim.forge_runtime import ENV_JAVA  # shared JRE override (MAKE_MAGIC_JAVA)

__all__ = (
    'ENV_JAVA',
    'ENV_XMAGE_HOME',
    'XMAGE_VERSION',
    'XMageInstall',
    'XMageUnavailableError',
    'resolve',
)

#: Point this at a BUILT XMage reactor (a clone where ``mvn -pl Mage.Tests -am
#: install -DskipTests`` has run) — the dir that contains ``Mage.Tests/``.
ENV_XMAGE_HOME = 'MAKE_MAGIC_XMAGE_HOME'

#: The pinned XMage version the harness is compiled + verified against.
XMAGE_VERSION = '1.4.60'

#: The reactor's transitive classpath, cached here by ``build.sh`` (one file of
#: ``:``-joined jar paths) so run-time resolution needs no ``mvn`` call.
_CLASSPATH_CACHE = 'make-magic-xmage-classpath.txt'

#: The committed harness jar (XMageBatch + the mage.collectors shadow), built by
#: ``pipeline/sim/java/xmage/build.sh`` and shipped alongside it (like forge-simai).
_HARNESS_JAR = Path(__file__).parent / 'java' / 'xmage' / 'make-magic-xmage.jar'


class XMageUnavailableError(RuntimeError):
    """No usable XMage install — carries an ACTIONABLE how-to-enable message."""


@dataclass(frozen=True)
class XMageInstall:
    """A resolved, launchable XMage install.

    ``mage_tests_dir`` is the cwd every ``XMageBatch`` run launches from, so the H2
    card DB (``db/``, built by ``CardScanner.scan()`` on first use) resolves.
    ``classpath`` is the full ``:``-joined run classpath (harness jar FIRST, then
    the reactor's transitive deps). ``java`` is the JRE launcher.
    """

    mage_tests_dir: Path
    classpath: str
    java: Path


def _resolve_java() -> Path:
    """The JRE launcher: ``MAKE_MAGIC_JAVA`` if set (+ validated), else PATH ``java``."""
    override = os.environ.get(ENV_JAVA)
    if override:
        java = Path(override)
        if not java.is_file():
            raise XMageUnavailableError(f'{ENV_JAVA}={override!r} is not an executable java launcher.')
        return java
    return Path('java')


def resolve(data_dir: str | os.PathLike[str] | None = None) -> XMageInstall:
    """Resolve the local XMage install, or raise :class:`XMageUnavailableError`.

    Read-only: validates ``MAKE_MAGIC_XMAGE_HOME`` (a built reactor with
    ``Mage.Tests/`` + the cached classpath) + the harness jar + the JRE, and
    assembles the run classpath. ``data_dir`` is accepted for signature parity with
    :func:`forge_runtime.resolve` (XMage's card DB lives under the reactor, not the
    sim data dir).
    """
    del data_dir  # XMage's card DB is under the reactor (Mage.Tests/db), not data_dir.

    home_env = os.environ.get(ENV_XMAGE_HOME)
    if not home_env:
        raise XMageUnavailableError(
            f'{ENV_XMAGE_HOME} is not set. Point it at a BUILT XMage {XMAGE_VERSION} reactor '
            '(a clone where `mvn -pl Mage.Tests -am install -DskipTests` has run).'
        )
    home = Path(home_env)
    mage_tests = home / 'Mage.Tests'
    if not mage_tests.is_dir():
        raise XMageUnavailableError(
            f'{ENV_XMAGE_HOME}={home_env!r} has no Mage.Tests/ — not a built XMage reactor. '
            'Clone XMage, then run `mvn -pl Mage.Tests -am install -DskipTests` in it.'
        )

    if not _HARNESS_JAR.is_file():
        raise XMageUnavailableError(
            f'XMage harness jar not found: {_HARNESS_JAR}. Build it with '
            'pipeline/sim/java/xmage/build.sh (needs the reactor on MAKE_MAGIC_XMAGE_HOME).'
        )

    cache = home / _CLASSPATH_CACHE
    if not cache.is_file():
        raise XMageUnavailableError(
            f'XMage classpath cache missing: {cache}. Run pipeline/sim/java/xmage/build.sh '
            'to assemble + cache the reactor classpath (one-time).'
        )
    reactor_cp = cache.read_text(encoding='utf-8').strip()
    if not reactor_cp:
        raise XMageUnavailableError(f'XMage classpath cache {cache} is empty; re-run build.sh.')

    java = _resolve_java()
    classpath = os.pathsep.join((str(_HARNESS_JAR), reactor_cp))
    return XMageInstall(mage_tests_dir=mage_tests, classpath=classpath, java=java)
