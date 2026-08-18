"""Resolve a local XMage install for the sim engine (mirrors :mod:`forge_runtime`).

XMage is a multi-module Maven app — unlike Forge, upstream publishes no single
fat jar to fetch. So (until the shaded-jar distribution build, task 2.3b) the
XMage engine runs against a LOCAL built reactor pointed to by
``MAKE_MAGIC_XMAGE_HOME``: a checked-out + built XMage clone (``mvn -pl
Mage.Tests,Mage.Server.Plugins/Mage.Player.AI.MA -am install -DskipTests`` — the
mad-bot module carries ComputerPlayer7). ``pipeline/sim/java/xmage/build.sh`` compiles the small
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
from typing import TYPE_CHECKING

from pipeline.sim.forge_runtime import ENV_JAVA  # shared JRE override (MAKE_MAGIC_JAVA)
from pipeline.store.paths import StorePaths

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = (
    'ENV_JAVA',
    'ENV_XMAGE_HOME',
    'XMAGE_VERSION',
    'XMageInstall',
    'XMageUnavailableError',
    'ensure',
    'resolve',
)

#: Point this at a BUILT XMage reactor (a clone where ``mvn -pl
#: Mage.Tests,Mage.Server.Plugins/Mage.Player.AI.MA -am install -DskipTests`` has
#: run) — the dir that contains ``Mage.Tests/``.
ENV_XMAGE_HOME = 'MAKE_MAGIC_XMAGE_HOME'

#: The pinned XMage version the harness is compiled + verified against.
XMAGE_VERSION = '1.4.60'

#: The reactor build command surfaced in "how to enable" errors. Includes the mad-bot
#: module (mage-player-ai-ma = ComputerPlayer7) explicitly — it is not a Mage.Tests dep.
_REACTOR_BUILD_CMD = 'mvn -pl Mage.Tests,Mage.Server.Plugins/Mage.Player.AI.MA -am install -DskipTests'

#: The reactor's transitive classpath, cached here by ``build.sh`` (one file of
#: ``:``-joined jar paths) so run-time resolution needs no ``mvn`` call.
_CLASSPATH_CACHE = 'make-magic-xmage-classpath.txt'

#: The committed harness jar (XMageBatch + the mage.collectors shadow), built by
#: ``pipeline/sim/java/xmage/build.sh`` and shipped alongside it (like forge-simai).
_HARNESS_JAR = Path(__file__).parent / 'java' / 'xmage' / 'make-magic-xmage.jar'

#: The FETCHED shaded distributable (2.3b): one self-contained runnable jar
#: (``make-magic-xmage-dist``) served from the public repo's GitHub Releases, cached
#: under ``<data_dir>/xmage/`` so a fresh box needs NO reactor build. ``ensure``
#: auto-provisions it (mirroring Forge's fetch-at-runtime).
_DIST_JAR_NAME = 'make-magic-xmage-dist.jar'
#: The release asset URL — pinned to the release tag that ``xmage-dist-release.yml``
#: publishes. (Update the tag when a new dist is cut.)
XMAGE_DIST_URL = (
    f'https://github.com/trippersham/magic-tools/releases/download/xmage-dist-{XMAGE_VERSION}/{_DIST_JAR_NAME}'
)
#: SHA256 of the published jar — the fail-closed integrity gate. ``None`` until the
#: FIRST release is cut: ``ensure`` then refuses to fetch (``_download_verified`` fails
#: closed on a missing checksum) with an actionable message. After cutting the release
#: (``build.sh --print-sha`` / the workflow's job summary), pin the hash HERE.
XMAGE_DIST_SHA256: str | None = None


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


def _dist_dir(data_dir: str | os.PathLike[str] | None) -> Path:
    """``<data_dir>/xmage/`` — where the fetched shaded jar + its card DB live."""
    root = Path(data_dir) if data_dir is not None else StorePaths.resolve().data_dir
    return root / 'xmage'


def resolve(data_dir: str | os.PathLike[str] | None = None) -> XMageInstall:
    """Resolve an XMage install (read-only), or raise :class:`XMageUnavailableError`.

    TWO install modes, tried in order:

    1. **Reactor** — if ``MAKE_MAGIC_XMAGE_HOME`` is set: a built reactor with
       ``Mage.Tests/`` + the cached classpath + the committed harness jar (the
       developer / from-source path). A set-but-broken reactor raises (not a
       silent fall-through to the fetched jar).
    2. **Fetched shaded jar** — else the self-contained ``make-magic-xmage-dist.jar``
       cached under ``<data_dir>/xmage/`` by :func:`ensure` (the all-users path; no
       reactor). Its classpath is the ONE jar (deps + harness shaded in); its cwd is
       ``<data_dir>/xmage/`` where ``CardScanner.scan()`` builds ``db/``.

    Neither present → an actionable error naming BOTH how-to-enable paths.
    """
    home_env = os.environ.get(ENV_XMAGE_HOME)
    if home_env:
        return _resolve_reactor(home_env)

    dist_jar = _dist_dir(data_dir) / _DIST_JAR_NAME
    if dist_jar.is_file():
        return XMageInstall(mage_tests_dir=dist_jar.parent, classpath=str(dist_jar), java=_resolve_java())

    raise XMageUnavailableError(
        f'no XMage install. Run `simulate doctor --provision` to auto-download the shaded XMage '
        f'jar (~76 MB, one-time, cached), or set {ENV_XMAGE_HOME} to a BUILT XMage {XMAGE_VERSION} '
        f'reactor (a clone where `{_REACTOR_BUILD_CMD}` has run).'
    )


def _resolve_reactor(home_env: str) -> XMageInstall:
    """Resolve the built-reactor install (``MAKE_MAGIC_XMAGE_HOME``); raise on any gap."""
    home = Path(home_env)
    mage_tests = home / 'Mage.Tests'
    if not mage_tests.is_dir():
        raise XMageUnavailableError(
            f'{ENV_XMAGE_HOME}={home_env!r} has no Mage.Tests/ — not a built XMage reactor. '
            f'Clone XMage, then run `{_REACTOR_BUILD_CMD}` in it.'
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
    classpath = os.pathsep.join((str(_HARNESS_JAR), reactor_cp))
    return XMageInstall(mage_tests_dir=mage_tests, classpath=classpath, java=_resolve_java())


def ensure(
    data_dir: str | os.PathLike[str] | None = None,
    *,
    on_fetch: Callable[[], None] | None = None,
) -> XMageInstall:
    """Resolve XMage, FETCHING the shaded distributable into the cache if nothing resolves.

    Mirrors :func:`forge_runtime.ensure`: tries :func:`resolve` first (a reactor or an
    already-cached jar); on :class:`XMageUnavailableError` downloads the pinned
    ``make-magic-xmage-dist.jar`` from GitHub Releases (SHA256-verified via
    :func:`forge_runtime._download_verified` — fail-closed) into ``<data_dir>/xmage/``
    and re-resolves. ``on_fetch`` fires ONCE, right before the download, only on the
    fetch path (the CLI uses it for a "downloading XMage…" notice). Any
    fetch/verify failure re-raises as :class:`XMageUnavailableError`.
    """
    try:
        return resolve(data_dir=data_dir)
    except XMageUnavailableError:
        pass

    if on_fetch is not None:
        on_fetch()

    from pipeline.sim.forge_runtime import _download_verified

    dist_dir = _dist_dir(data_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)
    dest = dist_dir / _DIST_JAR_NAME
    try:
        _download_verified(XMAGE_DIST_URL, dest, sha256=XMAGE_DIST_SHA256)
    except Exception as exc:
        dest.unlink(missing_ok=True)  # never leave a partial/unverified jar cached.
        raise XMageUnavailableError(
            f'could not fetch the shaded XMage jar from {XMAGE_DIST_URL}: {exc}. '
            f'(Or set {ENV_XMAGE_HOME} to a built reactor to skip the download.)'
        ) from exc
    return resolve(data_dir=data_dir)
