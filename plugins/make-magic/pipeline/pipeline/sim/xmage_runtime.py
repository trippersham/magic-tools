"""Resolve a local XMage install for the sim engine (mirrors :mod:`forge_runtime`).

XMage is a multi-module Maven app — unlike Forge, upstream publishes no single
fat jar to fetch. So (until the shaded-jar distribution build, task 2.3b) the
XMage engine runs against a LOCAL built reactor pointed to by
``MAKE_MAGIC_XMAGE_HOME``: a checked-out + built XMage clone (``mvn -pl Mage.Tests,
Mage.Server.Plugins/Mage.Player.AI.MA,Mage.Server.Plugins/Mage.Game.CommanderDuel -am
install -DskipTests`` — the mad-bot module carries ComputerPlayer7, the CommanderDuel
module the EDH game type). ``pipeline/sim/java/xmage/build.sh`` compiles the small
harness jar AND caches the reactor's transitive classpath to
``<home>/make-magic-xmage-classpath.txt`` — so resolution here is a fast,
network-free read (no ``mvn`` at run time).

Everything FAILS OPEN into a clear :class:`XMageUnavailableError` (re-raised as
``EngineUnavailableError`` by the engine) — never a traceback — so ``doctor`` and
a missing-install run stay actionable.
"""

from __future__ import annotations

import hashlib
import logging
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
    'ENV_XMAGE_DIST_JAR',
    'ENV_XMAGE_HOME',
    'XMAGE_VERSION',
    'XMageInstall',
    'XMageUnavailableError',
    'effective_dist_sha256',
    'ensure',
    'resolve',
)

_log = logging.getLogger(__name__)

#: Point this at a BUILT XMage reactor (a clone where the ``_REACTOR_BUILD_CMD`` below
#: has run) — the dir that contains ``Mage.Tests/``.
ENV_XMAGE_HOME = 'MAKE_MAGIC_XMAGE_HOME'

#: DEV-ONLY escape hatch: point this at a LOCAL dist jar (e.g. a freshly-built,
#: not-yet-released ``make-magic-xmage-dist.jar``). When set, :func:`resolve` /
#: :func:`ensure` use THAT jar directly — no fetch, no download, no code-pin SHA
#: verification — and the jar's OWN sha256 becomes the *effective dist SHA*
#: (:func:`effective_dist_sha256`) that keys the driver-compile cache + the ECJ
#: classpath. It takes precedence over BOTH install modes below. Unset restores the
#: byte-for-byte production path (fetch + verify against :data:`XMAGE_DIST_SHA256`,
#: fail-closed on ``None``). Used to compile/test Drivers against a local dist before a
#: release is cut.
ENV_XMAGE_DIST_JAR = 'MAKE_MAGIC_XMAGE_DIST_JAR'

#: The pinned XMage version the harness is compiled + verified against.
XMAGE_VERSION = '1.4.60'

#: The reactor build command surfaced in "how to enable" errors. Names, beyond Mage.Tests,
#: the mad-bot module (mage-player-ai-ma = ComputerPlayer7) and the CommanderDuel module
#: (the 1v1 EDH game type) explicitly — neither is a Mage.Tests dependency.
_REACTOR_BUILD_CMD = (
    'mvn -pl Mage.Tests,Mage.Server.Plugins/Mage.Player.AI.MA,'
    'Mage.Server.Plugins/Mage.Game.CommanderDuel -am install -DskipTests'
)

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
#: The dist-artifact tag ``xmage-dist-release.yml`` publishes to. A DIST REVISION of the
#: upstream ``XMAGE_VERSION`` line, bumped when the shaded jar's CONTENTS change without
#: an upstream XMage bump — here ``-2`` is the first dist that bundles CommanderDuel (so
#: install-mode can run 1v1 commander). Kept prefixed with ``xmage-dist-{XMAGE_VERSION}``
#: so the coupling to the built XMage version stays legible; the workflow's tag trigger
#: matches the ``xmage-dist-*`` wildcard, so any revision publishes.
_DIST_TAG = f'xmage-dist-{XMAGE_VERSION}-2'
#: The release asset URL — pinned to :data:`_DIST_TAG`. (Bump ``_DIST_TAG`` + re-pin the
#: SHA below when a new dist is cut.)
XMAGE_DIST_URL = f'https://github.com/trippersham/magic-tools/releases/download/{_DIST_TAG}/{_DIST_JAR_NAME}'
#: SHA256 of the published jar — the fail-closed integrity gate, pinned to the
#: :data:`_DIST_TAG` release's ``make-magic-xmage-dist.jar.sha256`` asset (shaded jars
#: are not byte-reproducible across builds, so the canonical hash comes from the release
#: build itself, not a local/dry-run rebuild). ``None`` re-arms fail-closed: ``ensure``
#: refuses to fetch (``_download_verified`` rejects a missing checksum) — the state
#: between bumping ``_DIST_TAG`` and pinning the newly-published ``.sha256``.
# Pinned to the ``make-magic-xmage-dist.jar.sha256`` asset published at :data:`_DIST_TAG`
# (``xmage-dist-1.4.60-2``). The shaded jar's bytes are NOT reproducible across builds, so
# this canonical hash comes from the release build itself, not a local rebuild. Re-pin from
# the freshly-published ``.sha256`` whenever ``_DIST_TAG`` is bumped. (``None`` re-arms the
# fail-closed gate: ``ensure`` refuses to fetch without a checksum — the transient state
# between bumping the tag and pinning the new asset.)
XMAGE_DIST_SHA256: str | None = '847f458796843f1010562667df809f42fc4d95e7316ccf96f2e7741fcae1930f'


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
    """The JRE launcher: ``MAKE_MAGIC_JAVA`` if set (+ validated), else PATH ``java`` (resolved).

    NEVER returns a bare, unresolved ``Path('java')``: that silent fallback deferred a
    missing-Java failure to an opaque per-worker JVM crash (every worker died the same way and the
    crash-loop respawner flooded staging). Instead resolve ``java`` on ``PATH`` up front and raise
    an ACTIONABLE :class:`XMageUnavailableError` when it is absent, so the boot preflight
    (:func:`pipeline.sim.simd.preflight.preflight_java`) can version-gate a REAL launcher path.
    """
    override = os.environ.get(ENV_JAVA)
    if override:
        java = Path(override)
        if not java.is_file():
            raise XMageUnavailableError(f'{ENV_JAVA}={override!r} is not an executable java launcher.')
        return java
    import shutil

    found = shutil.which('java')
    if found is None:
        raise XMageUnavailableError(
            f'no `java` on PATH and {ENV_JAVA} is unset. Set {ENV_JAVA} to a JRE {XMAGE_VERSION}-'
            'compatible (Java 21+) launcher, or install one on PATH.'
        )
    return Path(found)


def _dist_override() -> Path | None:
    """The :data:`ENV_XMAGE_DIST_JAR` local-dist override jar, or ``None`` when unset.

    A set-but-unusable override (missing / empty / not a file) RAISES rather than silently
    falling through to the production fetch path — an explicit dev override that cannot be
    honored must fail loudly.
    """
    override = os.environ.get(ENV_XMAGE_DIST_JAR)
    if not override:
        return None
    jar = Path(override)
    if not (jar.is_file() and jar.stat().st_size > 0):
        raise XMageUnavailableError(
            f'{ENV_XMAGE_DIST_JAR}={override!r} is not a readable, non-empty jar file.'
        )
    return jar


def _sha256_of(path: Path) -> str:
    """SHA256 of ``path``, hashed in 1 MiB chunks (the dist jar is ~79 MB)."""
    digest = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def effective_dist_sha256(data_dir: str | os.PathLike[str] | None = None) -> str | None:
    """The dist SHA that keys the driver-compile cache + identifies the ECJ classpath.

    With the local-dist override active, this is the override jar's REAL hash (so swapping
    the local jar re-keys the compile cache — a changed dist forces recompilation). Unset,
    it is the code-pinned :data:`XMAGE_DIST_SHA256` verbatim — ``None`` in the release-cut
    window, so the production path is unchanged by this accessor's presence. ``data_dir`` is
    accepted for signature parity with :func:`resolve` / :func:`ensure` (the override path
    is data-dir-independent).
    """
    override = _dist_override()
    if override is not None:
        return _sha256_of(override)
    return XMAGE_DIST_SHA256


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
    override = _dist_override()
    if override is not None:
        _log.warning(
            'LOCAL-DIST OVERRIDE active (%s=%s): using this jar directly — no fetch, no '
            'download, no code-pin SHA verification. This is a dev-only escape hatch.',
            ENV_XMAGE_DIST_JAR,
            override,
        )
        # Prepend the COMMITTED harness jar so its fresh XMageBatch (register-by-playerId)
        # shadows the STALE shaded XMageBatch bundled inside the dist jar. Class-loading
        # takes the FIRST match on the classpath, so without this the old engine-replacement
        # class wins, ignores -Dmakemagic.driver, and the run silently degrades to bare CP7
        # (exit 0) — defeating the driver gate. Mirrors the reactor branch's ordering.
        classpath = os.pathsep.join((str(_HARNESS_JAR), str(override)))
        return XMageInstall(mage_tests_dir=override.parent, classpath=classpath, java=_resolve_java())

    home_env = os.environ.get(ENV_XMAGE_HOME)
    if home_env:
        return _resolve_reactor(home_env)

    dist_jar = _dist_dir(data_dir) / _DIST_JAR_NAME
    # Require a NON-EMPTY jar: a 0-byte file (an interrupted `open('wb')` that wrote
    # nothing, a bad manual copy, a full-disk truncation) must be treated as "not
    # installed" so `ensure` re-fetches it, rather than resolving as available and
    # deferring to an opaque JVM classpath crash. (`ensure` publishes atomically, so
    # the fetch path itself never leaves a partial here — this guards external damage.)
    if dist_jar.is_file() and dist_jar.stat().st_size > 0:
        _verify_cached_jar_integrity(dist_jar)
        return XMageInstall(mage_tests_dir=dist_jar.parent, classpath=str(dist_jar), java=_resolve_java())

    raise XMageUnavailableError(
        f'no XMage install. Run `simulate doctor --provision` to auto-download the shaded XMage '
        f'jar (~76 MB, one-time, cached), or set {ENV_XMAGE_HOME} to a BUILT XMage {XMAGE_VERSION} '
        f'reactor (a clone where `{_REACTOR_BUILD_CMD}` has run).'
    )


def _verify_cached_jar_integrity(dist_jar: Path) -> None:
    """Re-verify a CACHE-HIT jar against the pinned SHA — not only at download time.

    ``ensure``'s download-time gate cannot protect a jar that was swapped AFTER it was
    cached (an attacker with write access to ``<data_dir>/xmage/``, or on-disk
    corruption). A sidecar marker would not help — the same write access defeats it — so
    the only real check is re-hashing against the CODE-pinned :data:`XMAGE_DIST_SHA256`
    (which a jar-swapping attacker cannot alter without editing the installed source).

    On a mismatch, raise :class:`XMageUnavailableError`: ``resolve`` refuses the swapped
    jar rather than launching it, and ``ensure``'s ``except XMageUnavailableError`` then
    re-fetches a verified jar over it (self-healing). Hashing ~76 MB is ~0.1-0.3 s —
    negligible against a multi-minute sim, and it runs only on the fetched-jar path.
    When the pin is ``None`` (the transient release-cut window) integrity is
    unverifiable, so the existence + size guard stands alone.
    """
    if XMAGE_DIST_SHA256 is None:
        return
    from pipeline.sim.forge_runtime import _verify_sha256

    try:
        _verify_sha256(dist_jar, XMAGE_DIST_SHA256)
    except ValueError as exc:
        raise XMageUnavailableError(
            f'cached XMage jar {dist_jar} failed its integrity check ({exc}) — refusing to run a '
            'swapped/corrupted jar; it will be re-fetched on the next provisioning run.'
        ) from exc


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
    and re-resolves. ``on_fetch`` fires ONCE, right before the download, only when a
    download will actually be ATTEMPTED (the CLI uses it for a "downloading XMage…"
    notice). Any fetch/verify failure re-raises as :class:`XMageUnavailableError`.
    """
    try:
        return resolve(data_dir=data_dir)
    except XMageUnavailableError:
        pass

    # Fail closed BEFORE announcing a fetch: with no pinned checksum we refuse to
    # download at all, so firing the "downloading…" notice first would be misleading
    # (the reviewer's on_fetch-before-abort finding). Check the gate up front.
    if XMAGE_DIST_SHA256 is None:
        raise XMageUnavailableError(
            f'refusing to install {XMAGE_DIST_URL!r} without a pinned SHA256 checksum '
            f'(fail-closed integrity gate). (Or set {ENV_XMAGE_HOME} to a built reactor '
            f'to skip the download.)'
        )

    if on_fetch is not None:
        on_fetch()

    from pipeline.sim.forge_runtime import _download_verified

    dist_dir = _dist_dir(data_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)
    dest = dist_dir / _DIST_JAR_NAME
    # Download + SHA-verify into a sibling staging path, then atomically publish. A
    # crash (SIGKILL / power loss / disk-full) between the partial write and the verify
    # must never leave a truncated jar at `dest` — resolve() trusts the cached jar by
    # existence alone. Mirrors forge_runtime._fetch_and_extract's staging discipline.
    staging = dist_dir / f'{_DIST_JAR_NAME}.incomplete'
    try:
        _download_verified(XMAGE_DIST_URL, staging, sha256=XMAGE_DIST_SHA256)
        os.replace(staging, dest)
    except Exception as exc:
        staging.unlink(missing_ok=True)  # never leave a partial/unverified jar cached.
        raise XMageUnavailableError(
            f'could not fetch the shaded XMage jar from {XMAGE_DIST_URL}: {exc}. '
            f'(Or set {ENV_XMAGE_HOME} to a built reactor to skip the download.)'
        ) from exc
    return resolve(data_dir=data_dir)
