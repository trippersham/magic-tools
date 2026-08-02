"""Locate (or fetch) a headless MTG Forge install for AI-vs-AI simulation.

A Forge ``sim`` run needs two things on disk: the ``forge-gui-desktop-*.jar``
(alongside its ``res/`` card DB) and a ``java`` binary to launch it. This module
resolves both via an OVERRIDE-FIRST ladder so tests and a pre-provisioned box
can reuse an existing install WITHOUT a download:

    1. env overrides — ``MAKE_MAGIC_FORGE_HOME`` (a Forge dir with the desktop
       jar + ``res/``) and ``MAKE_MAGIC_JAVA`` (a java binary). Both win first.
    2. the cached install under ``<data_dir>/forge/`` (jar) +
       ``<data_dir>/forge/jre/`` (bundled JRE).
    3. a cached jar + a system ``java`` found on ``PATH``.

:func:`resolve` performs the ladder read-only and raises
:class:`ForgeUnavailableError` (never crashes the caller) when nothing resolves.
:func:`ensure` is :func:`resolve` plus a one-time fetch-at-runtime fallback:
download the pinned Forge 2.0.13 tarball (SHA256-verified against
:data:`FORGE_TARBALL_SHA256`) + a Temurin 21 JRE and extract into the cache.

Install location, headless flags, and the pinned SHA are all empirically
validated against Forge 2.0.13 + Temurin 21.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline.store.paths import StorePaths

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = (
    'ENV_FORGE_HOME',
    'ENV_JAVA',
    'FORGE_JAR_GLOB',
    'FORGE_TARBALL_SHA256',
    'FORGE_TARBALL_URL',
    'FORGE_VERSION',
    'ForgeInstall',
    'ForgeUnavailableError',
    'ensure',
    'resolve',
)

#: Env var: a Forge home dir (contains ``forge-gui-desktop-*.jar`` + ``res/``).
ENV_FORGE_HOME = 'MAKE_MAGIC_FORGE_HOME'
#: Env var: path to a ``java`` binary to launch Forge with.
ENV_JAVA = 'MAKE_MAGIC_JAVA'

#: Pinned Forge release (the empirically-validated install target).
FORGE_VERSION = '2.0.13'
#: The desktop jar's glob inside a Forge home (version-tolerant match).
FORGE_JAR_GLOB = 'forge-gui-desktop-*-jar-with-dependencies.jar'
#: Fetch-at-runtime source for the pinned Forge installer tarball.
FORGE_TARBALL_URL = (
    f'https://github.com/Card-Forge/forge/releases/download/'
    f'forge-{FORGE_VERSION}/forge-installer-{FORGE_VERSION}.tar.bz2'
)
#: SHA256 of the Forge installer tarball (``shasum -a 256`` of the pinned
#: release's ``.tar.bz2``), pinned here so a fetch-at-runtime download is
#: verified before extraction.
FORGE_TARBALL_SHA256 = 'df23b237095cfc5ff97a4711946b25ff852da9ff43b916c40783f6b5a41ce855'

#: Adoptium ASSETS metadata endpoint — returns the JRE asset's download ``link``
#: AND its published ``checksum`` (SHA256) in one call, so a fetched JRE is
#: integrity-verified (SHA-pinned) before its ``java`` binary is executed (see
#: :func:`_temurin_asset`). The ``link`` 307-redirects to a GitHub release asset;
#: the HTTPS-only opener (:func:`_urlopen`) follows it without allowing a
#: downgrade. There is intentionally NO checksum-less binary-redirect fallback —
#: an unverifiable JRE fails closed.
_TEMURIN_ASSETS_API = 'https://api.adoptium.net/v3/assets/latest/21/ga'

#: User-Agent for downloads. GitHub release-asset / Adoptium endpoints 403 or
#: rate-limit requests that send no (or a bare ``Python-urllib``) UA under load,
#: so identify with a real UA string.
_USER_AGENT = 'make-magic-sim/1.0 (+https://github.com/Card-Forge/forge)'
#: Download retry budget (linear backoff) for TRANSIENT failures only.
_DOWNLOAD_ATTEMPTS = 3
#: HTTP statuses worth retrying (rate-limit + transient server errors). A 404
#: (URL rot) or any non-HTTP ``OSError`` (e.g. disk-full) is permanent -> fail
#: fast with no backoff.
_TRANSIENT_HTTP = frozenset({403, 429, 500, 502, 503, 504})
#: Socket timeout (s) for every download / metadata request. Without one,
#: ``urlopen`` inherits the default of NO timeout and a stalled connection hangs
#: the first-run provision forever (the retry loop never even gets to fire).
#: This is a per-socket-op inactivity timeout, so a slow-but-moving ~350 MB
#: stream is unaffected.
_HTTP_TIMEOUT_S = 60.0


class _HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A redirect handler that REFUSES to follow a redirect to a non-HTTPS target.

    Both fetch targets are redirect-based (GitHub releases 302, Adoptium
    ``binary/latest`` 307), and ``urllib``'s default handler silently follows an
    ``https -> http`` DOWNGRADE. Since the downloaded ``java`` is then executed, a
    network attacker who controls the redirect hop could serve a swapped binary
    over plain HTTP — so a redirect ``Location`` that is not ``https://`` is
    rejected here, closing the downgrade.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not newurl.lower().startswith('https://'):
            raise urllib.error.URLError(f'refusing non-HTTPS redirect target: {newurl!r}')
        return super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]


def _urlopen(req: urllib.request.Request, *, timeout: float):
    """Open ``req`` through an opener that rejects ``https -> http`` redirects.

    The single network entry point for both metadata and download fetches: builds
    a fresh opener with :class:`_HTTPSOnlyRedirectHandler` so a downgrade on the
    (always redirect-based) fetch hops cannot slip an unverified binary through.
    """
    opener = urllib.request.build_opener(_HTTPSOnlyRedirectHandler())
    return opener.open(req, timeout=timeout)


class ForgeUnavailableError(RuntimeError):
    """No Forge install could be resolved and (for :func:`resolve`) none was fetched.

    The message is actionable: it names the env overrides and the ``ensure()``
    fetch path so a caller knows how to make Forge available.
    """


@dataclass(frozen=True)
class ForgeInstall:
    """A resolved, launchable Forge install.

    ``forge_dir`` is the cwd every ``sim`` invocation runs from (so ``res/``
    resolves); ``jar`` is the desktop jar within it; ``java`` is the binary to
    launch it with; ``decks_dir`` is where ``.dck`` files are staged for ``-d``.
    """

    forge_dir: Path
    jar: Path
    java: Path

    @property
    def decks_dir(self) -> Path:
        """The Forge profile decks root the runner stages ``.dck`` files under.

        Forge resolves ``-d`` filenames against ``<profile>/decks/<fmt-dir>/``;
        the profile dir is PER-PLATFORM (macOS ``~/Library/Application Support/
        Forge``, Linux ``~/.forge``), so decks staged here are exactly where Forge
        looks. The runner appends the per-format subdir (``constructed/`` or
        ``commander/``).
        """
        return _forge_profile_dir() / 'decks'


def _forge_profile_dir() -> Path:
    """Forge's per-platform user-profile dir (the parent of ``decks/``).

    Forge stores its profile under the OS-native app-data location: macOS uses
    ``~/Library/Application Support/Forge``, Linux uses ``~/.forge``. Staging a
    ``.dck`` anywhere else means Forge silently never finds it (it exits 0 on a
    deck-load miss), so this MUST track Forge's own resolution. Windows is
    rejected up-front (see :func:`_guard_supported_os`).
    """
    if platform.system() == 'Darwin':
        return Path.home() / 'Library' / 'Application Support' / 'Forge'
    return Path.home() / '.forge'


def _find_jar(forge_dir: Path) -> Path | None:
    """Return the desktop jar inside ``forge_dir``, or None if absent."""
    matches = sorted(forge_dir.glob(FORGE_JAR_GLOB))
    return matches[0] if matches else None


def _validate_forge_home(forge_dir: Path) -> Path:
    """Return the desktop jar in ``forge_dir`` or raise a clear error.

    A valid Forge home MUST contain the ``forge-gui-desktop-*.jar`` (``res/`` is
    shipped beside it in the same tarball, so the jar is the reliable tell).
    """
    jar = _find_jar(forge_dir)
    if jar is None:
        raise ForgeUnavailableError(
            f'{forge_dir} is not a Forge home: no {FORGE_JAR_GLOB!r} found. '
            f'Point {ENV_FORGE_HOME} at a dir containing the desktop jar + res/.'
        )
    return jar


def _cached_java(forge_dir: Path) -> Path | None:
    """A bundled JRE's ``java`` under ``<forge_dir>/jre/`` (recursive), if present."""
    jre_dir = forge_dir / 'jre'
    if not jre_dir.is_dir():
        return None
    matches = sorted(jre_dir.rglob('bin/java'))
    return matches[0] if matches else None


def resolve(*, data_dir: Path | None = None) -> ForgeInstall:
    """Resolve a launchable Forge install via the override-first ladder.

    ``data_dir`` is the cache root (defaults to :class:`StorePaths` resolved
    ``data_dir``); the cached install lives at ``<data_dir>/forge/``. Read-only:
    never downloads. Raises :class:`ForgeUnavailableError` if nothing resolves
    (the message points at :func:`ensure` for the fetch path).
    """
    root = data_dir if data_dir is not None else StorePaths.resolve().data_dir

    # 1. env overrides win first (reuse an existing install without a download).
    env_home = os.getenv(ENV_FORGE_HOME)
    env_java = os.getenv(ENV_JAVA)
    if env_home:
        forge_dir = Path(env_home).expanduser()
        jar = _validate_forge_home(forge_dir)
        java = _resolve_java(env_java, forge_dir)
        return ForgeInstall(forge_dir=forge_dir, jar=jar, java=java)

    # 2/3. the cached install (bundled JRE, else system java + cached jar).
    cache_forge = root / 'forge'
    jar = _find_jar(cache_forge)
    if jar is not None:
        java = _resolve_java(env_java, cache_forge)
        return ForgeInstall(forge_dir=cache_forge, jar=jar, java=java)

    raise ForgeUnavailableError(
        'No Forge install found. Set '
        f'{ENV_FORGE_HOME} (+ {ENV_JAVA}) to reuse an existing install, or call '
        'ensure() to fetch Forge into the data cache.'
    )


def _resolve_java(env_java: str | None, forge_dir: Path) -> Path:
    """Pick a ``java`` binary: env override, then bundled JRE, then system PATH."""
    if env_java:
        return Path(env_java).expanduser()
    bundled = _cached_java(forge_dir)
    if bundled is not None:
        return bundled
    system = shutil.which('java')
    if system:
        return Path(system)
    raise ForgeUnavailableError(
        f'No java found for Forge. Set {ENV_JAVA}, bundle a JRE under {forge_dir / "jre"}, or put java on PATH.'
    )


def ensure(
    *,
    data_dir: Path | None = None,
    on_fetch: Callable[[], None] | None = None,
) -> ForgeInstall:
    """Resolve Forge, fetching the pinned release into the cache if nothing resolves.

    Tries :func:`resolve` first; on :class:`ForgeUnavailableError` downloads the
    pinned Forge tarball (SHA256-verified) + a Temurin 21 JRE into
    ``<data_dir>/forge/`` and re-resolves. Any fetch/verify/extract failure is
    re-raised as :class:`ForgeUnavailableError` with an actionable message.

    ``on_fetch`` (if given) is called ONCE, right before the download begins —
    only on the fetch path, never when Forge already resolves. Callers use it to
    surface a one-time "downloading Forge…" notice so a first run doesn't appear
    to hang on the ~350 MB pull.
    """
    root = data_dir if data_dir is not None else StorePaths.resolve().data_dir
    try:
        return resolve(data_dir=root)
    except ForgeUnavailableError:
        pass

    # Fail Windows CLEANLY before burning a ~350 MB wrong-OS download (S3).
    _guard_supported_os()

    if on_fetch is not None:
        on_fetch()

    forge_dir = root / 'forge'
    jre_dir = forge_dir / 'jre'
    try:
        _fetch_and_extract(
            forge_dir=forge_dir,
            jre_dir=jre_dir,
            forge_url=FORGE_TARBALL_URL,
            forge_sha256=FORGE_TARBALL_SHA256,
        )
    except Exception as exc:  # any fetch/extract failure is fatal-but-graceful.
        raise ForgeUnavailableError(
            f'Could not download/extract Forge {FORGE_VERSION} into {forge_dir}: {exc}. '
            f'Provide an existing install via {ENV_FORGE_HOME} + {ENV_JAVA} instead.'
        ) from exc

    return resolve(data_dir=root)


def _guard_supported_os() -> None:
    """Reject Windows up-front with an actionable message (macOS / Linux only).

    Forge staging (:func:`_forge_profile_dir`), the ``_launch_prefix`` xvfb
    wrapping, and the Temurin OS mapping (:func:`_temurin_os`) are all built for
    macOS + Linux. On Windows they would silently mis-resolve (wrong JRE, wrong
    profile dir) and burn a ~350 MB download before an opaque JVM failure — so
    fail CLEANLY before any of that, pointing at WSL2.
    """
    if platform.system() == 'Windows':
        raise ForgeUnavailableError('Windows is not supported; use WSL2 (a Linux env) to run Forge simulations.')


def _temurin_os() -> str:
    """Adoptium ``os`` slug for this platform (``mac`` on macOS, else ``linux``).

    Windows is rejected earlier (:func:`_guard_supported_os`), so the only two
    reachable values are ``mac`` and ``linux`` — a non-Darwin system is Linux.
    """
    return 'mac' if platform.system() == 'Darwin' else 'linux'


def _parse_temurin_asset(payload: object) -> tuple[str, str | None]:
    """Extract ``(download_url, sha256|None)`` from an Adoptium assets-API payload.

    The ``/v3/assets/latest`` response is a non-empty list; the first entry's
    ``binary.package`` carries ``link`` (the ``.tar.gz`` URL) and ``checksum``
    (its SHA256). Raises on an empty/unexpected shape; a missing ``checksum``
    yields a ``None`` that :func:`_temurin_asset` then treats as fail-closed.
    """
    if not isinstance(payload, list) or not payload:
        raise ValueError('empty or non-list Adoptium assets payload')
    package = payload[0]['binary']['package']  # type: ignore[index]
    link = package['link']
    checksum = package.get('checksum')
    if not isinstance(link, str):
        raise TypeError('Adoptium asset link is not a string')
    return link, checksum if isinstance(checksum, str) else None


def _temurin_asset() -> tuple[str, str]:
    """Resolve the Temurin 21 JRE ``(url, sha256)`` for this platform — FAIL CLOSED.

    Uses the Adoptium assets-metadata endpoint, which returns both the asset
    ``link`` and its published ``checksum`` in one call — so the JRE is
    integrity-verified like Forge before its ``java`` binary is ever executed.
    A missing checksum (or any endpoint/schema/parse failure) RAISES rather than
    degrading to an unverified download: running an integrity-unchecked binary
    fetched on a hostile network is the exact RCE gap this closes. Network work —
    reached only on the fetch path (``_fetch_and_extract`` is mocked in tests).
    """
    machine = platform.machine().lower()
    arch = 'aarch64' if machine in ('arm64', 'aarch64') else 'x64'
    os_name = _temurin_os()
    query = (
        f'{_TEMURIN_ASSETS_API}?architecture={arch}&heap_size=normal'
        f'&image_type=jre&jvm_impl=hotspot&os={os_name}&vendor=eclipse'
    )
    req = urllib.request.Request(query, headers={'User-Agent': _USER_AGENT})
    with _urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # Adoptium public metadata API.
        payload = json.load(resp)
    url, checksum = _parse_temurin_asset(payload)
    if checksum is None:
        raise ValueError('Adoptium asset carries no published SHA256 checksum; refusing an unverifiable JRE')
    return url, checksum


def _fetch_and_extract(
    *,
    forge_dir: Path,
    jre_dir: Path,
    forge_url: str,
    forge_sha256: str,
) -> None:
    """Download + verify + extract Forge and a Temurin JRE into ``forge_dir`` ATOMICALLY.

    Builds the whole install (jar + ``res/`` + ``jre/``) in a sibling staging dir
    and ``os.replace``s it into ``forge_dir`` only after BOTH archives extract —
    so an interrupted fetch never leaves a truncated jar that :func:`resolve`
    (which checks jar existence, not integrity) would report as 'available'. The
    Forge tarball is SHA256-verified against ``forge_sha256``; the JRE against the
    Adoptium-published checksum when available (else gzip-validated on extract;
    see :func:`_temurin_asset`).

    Real network + disk work; tests MONKEYPATCH this function (or the
    ``_download_verified`` / ``_temurin_asset`` / ``tarfile`` helpers) so no
    download runs in the suite.
    """
    parent = forge_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f'.{forge_dir.name}.incomplete'
    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging_jre = staging / jre_dir.name
        staging_jre.mkdir(parents=True)

        forge_archive = staging / f'forge-installer-{FORGE_VERSION}.tar.bz2'
        _download_verified(forge_url, forge_archive, sha256=forge_sha256)
        with tarfile.open(forge_archive, 'r:bz2') as tar:  # r:bz2 also rejects a truncated download.
            tar.extractall(staging, filter='data')
        forge_archive.unlink(missing_ok=True)

        jre_url, jre_sha256 = _temurin_asset()
        jre_archive = staging_jre / 'jre.tar.gz'
        _download_verified(jre_url, jre_archive, sha256=jre_sha256)
        with tarfile.open(jre_archive, 'r:gz') as tar:  # r:gz also rejects a truncated download.
            tar.extractall(staging_jre, filter='data')
        jre_archive.unlink(missing_ok=True)

        # Atomic publish: forge_dir goes absent -> fully-populated in a single rename.
        shutil.rmtree(forge_dir, ignore_errors=True)
        os.replace(staging, forge_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)  # never leave a partial install behind.
        raise


def _download_verified(url: str, dest: Path, *, sha256: str | None) -> None:
    """:func:`_download` ``url`` to ``dest``, then SHA256-verify — FAIL CLOSED.

    A ``None`` checksum is a HARD ERROR, not a skip: the downloaded ``java`` is
    executed, so an unverifiable JRE (Adoptium metadata unreachable or its
    published checksum missing) must abort the provision rather than run an
    integrity-unchecked binary fetched over the network. bz2/gzip validation on
    extract only catches truncation, never substitution — so it is NOT a
    substitute for the SHA gate.
    """
    if sha256 is None:
        raise ValueError(f'refusing to install {url!r} without a published SHA256 checksum (fail-closed integrity gate)')
    _download(url, dest)
    _verify_sha256(dest, sha256)


def _download(url: str, dest: Path, *, attempts: int = _DOWNLOAD_ATTEMPTS) -> None:
    """Stream ``url`` to ``dest`` with a real User-Agent + linear-backoff retry.

    HTTPS-only: a non-``https://`` URL (e.g. an ``http://`` — or ``file://`` —
    link injected via a compromised metadata payload) is refused up front;
    ``urlopen`` would otherwise happily open any scheme it knows.

    Retries ONLY transient failures — an HTTP status in :data:`_TRANSIENT_HTTP`
    (rate-limit / 5xx) or a connection-level ``URLError`` / timeout — up to
    ``attempts`` times. A PERMANENT error (a 404 URL-rot, or a non-HTTP
    ``OSError`` such as disk-full) FAILS FAST with no retry, so it surfaces
    immediately instead of burning the full backoff. Every attempt carries the
    :data:`_HTTP_TIMEOUT_S` socket timeout so a stalled connection cannot hang
    the provision forever. The final error propagates so :func:`ensure` can wrap
    it in an actionable :class:`ForgeUnavailableError`. Network work (mocked in
    tests).
    """
    if not url.lower().startswith('https://'):
        raise ValueError(f'refusing non-HTTPS download URL: {url!r}')
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': _USER_AGENT})
            with (
                _urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp,  # pinned URL + UA, HTTPS-only redirects.
                dest.open('wb') as fh,
            ):
                shutil.copyfileobj(resp, fh)
            return
        except urllib.error.HTTPError as exc:
            # Retry rate-limits / 5xx; a 404 (or any other status) is permanent.
            if exc.code not in _TRANSIENT_HTTP or attempt == attempts:
                raise
        except (urllib.error.URLError, TimeoutError):
            # Connection-level transient (DNS / reset / timeout); give up after the budget.
            if attempt == attempts:
                raise
        time.sleep(2 * attempt)


def _verify_sha256(path: Path, expected: str) -> None:
    """Raise if ``path``'s SHA256 does not match ``expected`` (integrity gate).

    Hashed in 1 MiB chunks — the Forge tarball is ~350 MB, so a whole-file
    ``read_bytes`` would spike RSS by that much for no benefit.
    """
    digest = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError(f'SHA256 mismatch for {path.name}: got {digest.hexdigest()}, expected {expected}')
