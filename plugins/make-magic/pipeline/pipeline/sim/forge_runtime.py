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
grounded in ``~/mtg-sim-lab/forge_backend.py`` (Forge 2.0.13, Temurin 21).
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

#: Pinned Forge release (matches the proven ~/mtg-sim-lab install).
FORGE_VERSION = '2.0.13'
#: The desktop jar's glob inside a Forge home (version-tolerant match).
FORGE_JAR_GLOB = 'forge-gui-desktop-*-jar-with-dependencies.jar'
#: Fetch-at-runtime source for the pinned Forge installer tarball.
FORGE_TARBALL_URL = (
    f'https://github.com/Card-Forge/forge/releases/download/'
    f'forge-{FORGE_VERSION}/forge-installer-{FORGE_VERSION}.tar.bz2'
)
#: SHA256 of the Forge installer tarball, computed once from
#: ``~/mtg-sim-lab/forge.tar.bz2`` (``shasum -a 256``) and pinned here so a
#: fetch-at-runtime download is verified before extraction.
FORGE_TARBALL_SHA256 = 'df23b237095cfc5ff97a4711946b25ff852da9ff43b916c40783f6b5a41ce855'

#: Adoptium API "latest 21 GA" binary endpoint — a STABLE redirect to the
#: current Temurin JRE asset (whose real filename embeds the exact version, e.g.
#: ``…_21.0.12_8.tar.gz``, so the version-less ``/releases/latest/download/`` path
#: 404s — do NOT use that). Only reached on the fetch fallback; overrides /
#: cached installs never hit it. The endpoint 307-redirects to a GitHub release
#: asset (a ``.tar.gz``); ``urllib`` follows the redirect.
_TEMURIN_API = 'https://api.adoptium.net/v3/binary/latest/21/ga'
#: Adoptium ASSETS metadata endpoint — unlike the ``binary`` redirect it returns
#: the JRE asset's download ``link`` AND its published ``checksum`` (SHA256) in
#: one call, so a fetched JRE can be integrity-verified (see :func:`_temurin_asset`).
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
        under the app-support profile this is the canonical location. The runner
        appends the per-format subdir (``constructed/`` or ``commander/``).
        """
        return Path.home() / 'Library' / 'Application Support' / 'Forge' / 'decks'


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


def _temurin_url() -> str:
    """Adoptium ``binary/latest`` redirect URL for this platform's Temurin 21 JRE.

    Resolves ``mac``/``linux`` x ``aarch64``/``x64`` into the ``binary/latest``
    endpoint (:data:`_TEMURIN_API`), which 307-redirects to the current versioned
    asset — so we never hardcode a JRE point-version that would rot. This is the
    checksum-less FALLBACK for :func:`_temurin_asset` (used only when the assets
    metadata call fails); the download is then gzip-validated on extract but not
    SHA-pinned.
    """
    machine = platform.machine().lower()
    arch = 'aarch64' if machine in ('arm64', 'aarch64') else 'x64'
    os_name = 'mac' if platform.system().lower() == 'darwin' else 'linux'
    return f'{_TEMURIN_API}/{os_name}/{arch}/jre/hotspot/normal/eclipse'


def _parse_temurin_asset(payload: object) -> tuple[str, str | None]:
    """Extract ``(download_url, sha256|None)`` from an Adoptium assets-API payload.

    The ``/v3/assets/latest`` response is a non-empty list; the first entry's
    ``binary.package`` carries ``link`` (the ``.tar.gz`` URL) and ``checksum``
    (its SHA256). Raises on an empty/unexpected shape so :func:`_temurin_asset`
    can fall back to the checksum-less binary redirect.
    """
    if not isinstance(payload, list) or not payload:
        raise ValueError('empty or non-list Adoptium assets payload')
    package = payload[0]['binary']['package']  # type: ignore[index]
    link = package['link']
    checksum = package.get('checksum')
    if not isinstance(link, str):
        raise TypeError('Adoptium asset link is not a string')
    return link, checksum if isinstance(checksum, str) else None


def _temurin_asset() -> tuple[str, str | None]:
    """Resolve the Temurin 21 JRE ``(url, sha256|None)`` for this platform.

    PREFERS the Adoptium assets-metadata endpoint, which returns both the asset
    ``link`` and its published ``checksum`` in one call — so the JRE can be
    integrity-verified like Forge. On ANY failure (endpoint down, schema drift,
    parse error) DEGRADES to the checksum-less ``binary/latest`` redirect
    (:func:`_temurin_url`); a ``None`` checksum signals that fallback, and the
    download is then still gzip-validated on extract, just not SHA-pinned (a
    static pin is impossible against a moving "latest"). Network work — reached
    only on the fetch path (``_fetch_and_extract`` is mocked in tests).
    """
    machine = platform.machine().lower()
    arch = 'aarch64' if machine in ('arm64', 'aarch64') else 'x64'
    os_name = 'mac' if platform.system().lower() == 'darwin' else 'linux'
    query = (
        f'{_TEMURIN_ASSETS_API}?architecture={arch}&heap_size=normal'
        f'&image_type=jre&jvm_impl=hotspot&os={os_name}&vendor=eclipse'
    )
    try:
        req = urllib.request.Request(query, headers={'User-Agent': _USER_AGENT})
        with urllib.request.urlopen(req) as resp:  # Adoptium public metadata API.
            payload = json.load(resp)
        return _parse_temurin_asset(payload)
    except Exception:  # metadata unavailable/changed — degrade to the redirect URL.
        return _temurin_url(), None


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
    """:func:`_download` ``url`` to ``dest``, then SHA256-verify when ``sha256`` is set.

    A ``None`` checksum (the Adoptium fallback) skips verification — the caller
    still bz2/gzip-validates the archive on extract, which catches a truncated
    download.
    """
    _download(url, dest)
    if sha256 is not None:
        _verify_sha256(dest, sha256)


def _download(url: str, dest: Path, *, attempts: int = _DOWNLOAD_ATTEMPTS) -> None:
    """Stream ``url`` to ``dest`` with a real User-Agent + linear-backoff retry.

    Retries ONLY transient failures — an HTTP status in :data:`_TRANSIENT_HTTP`
    (rate-limit / 5xx) or a connection-level ``URLError`` / timeout — up to
    ``attempts`` times. A PERMANENT error (a 404 URL-rot, or a non-HTTP
    ``OSError`` such as disk-full) FAILS FAST with no retry, so it surfaces
    immediately instead of burning the full backoff. The final error propagates
    so :func:`ensure` can wrap it in an actionable
    :class:`ForgeUnavailableError`. Network work (mocked in tests).
    """
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': _USER_AGENT})
            with urllib.request.urlopen(req) as resp, dest.open('wb') as fh:  # pinned URL + UA.
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
    """Raise if ``path``'s SHA256 does not match ``expected`` (integrity gate)."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise ValueError(f'SHA256 mismatch for {path.name}: got {digest}, expected {expected}')
