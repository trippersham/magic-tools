"""OFFLINE tests for the Forge runtime locator (:mod:`pipeline.sim.forge_runtime`).

Covers the override-first resolution ladder, the mocked fetch path of
``ensure()`` (NEVER a real download), and the loud ``ForgeUnavailableError``
when nothing is resolvable. Every filesystem artifact is a tmp-dir stub — no
real Forge install, jar, or JRE is touched here.
"""

from __future__ import annotations

import io
import urllib.error
from pathlib import Path

import pytest

from pipeline.sim import forge_runtime as fr
from pipeline.sim.forge_runtime import (
    ENV_FORGE_HOME,
    ENV_JAVA,
    FORGE_TARBALL_SHA256,
    FORGE_VERSION,
    ForgeInstall,
    ForgeUnavailableError,
    ensure,
    resolve,
)


@pytest.fixture(autouse=True)
def _clear_forge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the resolution-ladder tests from ambient overrides.

    These tests inject Forge/Java paths explicitly, so a developer with
    ``MAKE_MAGIC_FORGE_HOME``/``MAKE_MAGIC_JAVA`` exported in their shell must
    not perturb the assertions (they otherwise short-circuit the ladder).
    """
    monkeypatch.delenv(ENV_FORGE_HOME, raising=False)
    monkeypatch.delenv(ENV_JAVA, raising=False)


# --------------------------------------------------------------------------- #
# helpers — build a minimal on-disk Forge home / java stub
# --------------------------------------------------------------------------- #


def _make_forge_home(root: Path) -> Path:
    """A dir that LOOKS like a Forge install: the desktop jar + a ``res/`` dir."""
    root.mkdir(parents=True, exist_ok=True)
    (root / f'forge-gui-desktop-{FORGE_VERSION}-jar-with-dependencies.jar').write_text('jar')
    (root / 'res').mkdir(exist_ok=True)
    return root


def _make_java(path: Path) -> Path:
    """An executable stub that stands in for a ``java`` binary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o755)
    return path


# --------------------------------------------------------------------------- #
# override-first resolution
# --------------------------------------------------------------------------- #


def test_resolve_honors_env_overrides_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    forge_home = _make_forge_home(tmp_path / 'forge')
    java = _make_java(tmp_path / 'jre' / 'bin' / 'java')
    monkeypatch.setenv(ENV_FORGE_HOME, str(forge_home))
    monkeypatch.setenv(ENV_JAVA, str(java))

    install = resolve(data_dir=tmp_path / 'unused-cache')

    assert isinstance(install, ForgeInstall)
    assert install.forge_dir == forge_home
    assert install.java == java
    assert install.jar.name.startswith('forge-gui-desktop-')
    assert install.jar.exists()


def test_resolve_override_wins_over_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override_home = _make_forge_home(tmp_path / 'override')
    java = _make_java(tmp_path / 'override' / 'java')
    # A cached install ALSO exists but the override must win.
    cache = tmp_path / 'cache'
    _make_forge_home(cache / 'forge')
    _make_java(cache / 'forge' / 'jre' / 'bin' / 'java')

    monkeypatch.setenv(ENV_FORGE_HOME, str(override_home))
    monkeypatch.setenv(ENV_JAVA, str(java))

    install = resolve(data_dir=cache)
    assert install.forge_dir == override_home


def test_resolve_missing_forge_home_override_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Points at a dir with no jar -> not a valid Forge home.
    empty = tmp_path / 'empty'
    empty.mkdir()
    monkeypatch.setenv(ENV_FORGE_HOME, str(empty))
    monkeypatch.setenv(ENV_JAVA, str(_make_java(tmp_path / 'java')))
    with pytest.raises(ForgeUnavailableError, match='forge-gui-desktop'):
        resolve(data_dir=tmp_path / 'cache')


def test_resolve_falls_back_to_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_FORGE_HOME, raising=False)
    monkeypatch.delenv(ENV_JAVA, raising=False)
    cache = tmp_path / 'data'
    _make_forge_home(cache / 'forge')
    _make_java(cache / 'forge' / 'jre' / 'bin' / 'java')

    install = resolve(data_dir=cache)
    assert install.forge_dir == cache / 'forge'
    assert install.java == cache / 'forge' / 'jre' / 'bin' / 'java'


def test_resolve_cache_jar_plus_system_java(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_FORGE_HOME, raising=False)
    monkeypatch.delenv(ENV_JAVA, raising=False)
    cache = tmp_path / 'data'
    _make_forge_home(cache / 'forge')  # cached jar, but NO bundled jre
    system_java = _make_java(tmp_path / 'usr' / 'bin' / 'java')
    monkeypatch.setattr('pipeline.sim.forge_runtime.shutil.which', lambda _cmd: str(system_java))

    install = resolve(data_dir=cache)
    assert install.forge_dir == cache / 'forge'
    assert install.java == system_java


def test_resolve_nothing_available_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_FORGE_HOME, raising=False)
    monkeypatch.delenv(ENV_JAVA, raising=False)
    monkeypatch.setattr('pipeline.sim.forge_runtime.shutil.which', lambda _cmd: None)
    with pytest.raises(ForgeUnavailableError, match='ensure'):
        resolve(data_dir=tmp_path / 'empty-cache')


# --------------------------------------------------------------------------- #
# ensure() — MOCKED fetch (never downloads)
# --------------------------------------------------------------------------- #


def test_ensure_returns_existing_without_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If a valid install already resolves, ensure() must NOT fetch."""
    cache = tmp_path / 'data'
    _make_forge_home(cache / 'forge')
    _make_java(cache / 'forge' / 'jre' / 'bin' / 'java')

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError('ensure() downloaded when a cached install was present')

    monkeypatch.setattr('pipeline.sim.forge_runtime._fetch_and_extract', _boom)
    install = ensure(data_dir=cache)
    assert install.forge_dir == cache / 'forge'


def test_ensure_fetches_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing cached + no override -> ensure() invokes the (mocked) fetch, then
    the freshly-extracted install resolves."""
    monkeypatch.delenv(ENV_FORGE_HOME, raising=False)
    monkeypatch.delenv(ENV_JAVA, raising=False)
    monkeypatch.setattr('pipeline.sim.forge_runtime.shutil.which', lambda _cmd: None)
    cache = tmp_path / 'data'

    calls: list[tuple[str, str]] = []

    def _fake_fetch(*, forge_dir: Path, jre_dir: Path, forge_url: str, forge_sha256: str) -> None:
        calls.append((forge_url, forge_sha256))
        # Simulate a successful extract: lay down the jar + res/ and a java bin.
        _make_forge_home(forge_dir)
        _make_java(jre_dir / 'bin' / 'java')

    monkeypatch.setattr('pipeline.sim.forge_runtime._fetch_and_extract', _fake_fetch)

    install = ensure(data_dir=cache)
    assert calls, 'ensure() did not call the fetch hook'
    assert calls[0][1] == FORGE_TARBALL_SHA256
    assert install.forge_dir == cache / 'forge'
    assert install.java.exists()


def test_ensure_calls_on_fetch_before_fetching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``on_fetch`` fires exactly once, BEFORE the download, only on the fetch path."""
    monkeypatch.delenv(ENV_FORGE_HOME, raising=False)
    monkeypatch.delenv(ENV_JAVA, raising=False)
    monkeypatch.setattr('pipeline.sim.forge_runtime.shutil.which', lambda _cmd: None)
    cache = tmp_path / 'data'
    events: list[str] = []

    def _fake_fetch(*, forge_dir: Path, jre_dir: Path, **_k: object) -> None:
        events.append('fetch')
        _make_forge_home(forge_dir)
        _make_java(jre_dir / 'bin' / 'java')

    monkeypatch.setattr('pipeline.sim.forge_runtime._fetch_and_extract', _fake_fetch)

    ensure(data_dir=cache, on_fetch=lambda: events.append('notice'))

    assert events == ['notice', 'fetch']  # notice fires first, then the download.


def test_ensure_skips_on_fetch_when_already_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When a cached install already resolves, ``on_fetch`` is NEVER called."""
    cache = tmp_path / 'data'
    _make_forge_home(cache / 'forge')
    _make_java(cache / 'forge' / 'jre' / 'bin' / 'java')
    called: list[str] = []

    monkeypatch.setattr(
        'pipeline.sim.forge_runtime._fetch_and_extract',
        lambda **_k: pytest.fail('must not fetch when cached'),
    )
    ensure(data_dir=cache, on_fetch=lambda: called.append('notice'))

    assert called == []  # no download -> no notice.


def test_ensure_fetch_failure_raises_forge_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_FORGE_HOME, raising=False)
    monkeypatch.delenv(ENV_JAVA, raising=False)
    monkeypatch.setattr('pipeline.sim.forge_runtime.shutil.which', lambda _cmd: None)

    def _fail(**_k: object) -> None:
        raise OSError('network down')

    monkeypatch.setattr('pipeline.sim.forge_runtime._fetch_and_extract', _fail)
    with pytest.raises(ForgeUnavailableError, match=r'network down|download'):
        ensure(data_dir=tmp_path / 'data')


def test_forge_sha256_is_pinned() -> None:
    """The Forge tarball SHA is a hardcoded 64-hex constant (pinned, not blank)."""
    assert isinstance(FORGE_TARBALL_SHA256, str)
    assert len(FORGE_TARBALL_SHA256) == 64
    assert all(c in '0123456789abcdef' for c in FORGE_TARBALL_SHA256)


# --------------------------------------------------------------------------- #
# _download — NARROWED retry (transient only; fail fast on 404 / OSError)
# --------------------------------------------------------------------------- #


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError('http://x/a', code, 'err', None, None)  # type: ignore[arg-type]


def test_download_retries_transient_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('pipeline.sim.forge_runtime.time.sleep', lambda _s: None)
    seq: list[object] = [_http_error(503), _http_error(429), io.BytesIO(b'payload')]

    def _fake_urlopen(_req: object) -> object:
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr('pipeline.sim.forge_runtime.urllib.request.urlopen', _fake_urlopen)
    dest = tmp_path / 'f.bin'
    fr._download('http://x/a', dest, attempts=3)
    assert dest.read_bytes() == b'payload'
    assert seq == []  # all three attempts consumed (2 transient + 1 success)


def test_download_fails_fast_on_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('pipeline.sim.forge_runtime.time.sleep', lambda _s: None)
    calls = {'n': 0}

    def _fake_urlopen(_req: object) -> object:
        calls['n'] += 1
        raise _http_error(404)

    monkeypatch.setattr('pipeline.sim.forge_runtime.urllib.request.urlopen', _fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        fr._download('http://x/a', tmp_path / 'f.bin', attempts=3)
    assert calls['n'] == 1  # a permanent 404 is NOT retried


def test_download_fails_fast_on_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('pipeline.sim.forge_runtime.time.sleep', lambda _s: None)
    calls = {'n': 0}

    def _fake_urlopen(_req: object) -> object:
        calls['n'] += 1
        raise OSError('disk full')

    monkeypatch.setattr('pipeline.sim.forge_runtime.urllib.request.urlopen', _fake_urlopen)
    with pytest.raises(OSError, match='disk full'):
        fr._download('http://x/a', tmp_path / 'f.bin', attempts=3)
    assert calls['n'] == 1  # a non-HTTP OSError is permanent -> no retry


# --------------------------------------------------------------------------- #
# _download_verified + JRE checksum
# --------------------------------------------------------------------------- #


def test_download_verified_raises_on_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        'pipeline.sim.forge_runtime._download',
        lambda _url, dest: dest.write_bytes(b'content'),
    )
    with pytest.raises(ValueError, match='SHA256 mismatch'):
        fr._download_verified('http://x', tmp_path / 'a', sha256='0' * 64)


def test_download_verified_skips_when_no_checksum(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        'pipeline.sim.forge_runtime._download',
        lambda _url, dest: dest.write_bytes(b'content'),
    )
    fr._download_verified('http://x', tmp_path / 'a', sha256=None)  # no verify, no raise
    assert (tmp_path / 'a').read_bytes() == b'content'


def test_parse_temurin_asset_extracts_link_and_checksum() -> None:
    payload = [{'binary': {'package': {'link': 'https://x/jre.tar.gz', 'checksum': 'abc123'}}}]
    assert fr._parse_temurin_asset(payload) == ('https://x/jre.tar.gz', 'abc123')


def test_parse_temurin_asset_missing_checksum_is_none() -> None:
    payload = [{'binary': {'package': {'link': 'https://x/jre.tar.gz'}}}]
    assert fr._parse_temurin_asset(payload) == ('https://x/jre.tar.gz', None)


def test_temurin_asset_falls_back_when_api_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_req: object) -> object:
        raise urllib.error.URLError('down')

    monkeypatch.setattr('pipeline.sim.forge_runtime.urllib.request.urlopen', _boom)
    url, sha = fr._temurin_asset()
    assert sha is None  # no checksum available -> fall back
    assert url.startswith('https://api.adoptium.net/v3/binary/latest/21/ga')


# --------------------------------------------------------------------------- #
# _fetch_and_extract — JRE integrity + ATOMIC publish
# --------------------------------------------------------------------------- #


class _FakeTar:
    """A no-op tarfile context manager (extract does nothing)."""

    def __enter__(self) -> _FakeTar:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def extractall(self, *_a: object, **_k: object) -> None:
        pass


def test_fetch_verifies_jre_and_publishes_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verified: list[tuple[str, str | None]] = []

    def _fake_dv(_url: str, dest: Path, *, sha256: str | None) -> None:
        verified.append((dest.name, sha256))
        dest.write_bytes(b'')

    monkeypatch.setattr('pipeline.sim.forge_runtime._download_verified', _fake_dv)
    monkeypatch.setattr('pipeline.sim.forge_runtime._temurin_asset', lambda: ('http://jre', 'jresha'))
    monkeypatch.setattr('pipeline.sim.forge_runtime.tarfile.open', lambda *_a, **_k: _FakeTar())

    forge_dir = tmp_path / 'forge'
    fr._fetch_and_extract(
        forge_dir=forge_dir,
        jre_dir=forge_dir / 'jre',
        forge_url='http://forge',
        forge_sha256='fsha',
    )
    shas = dict(verified)
    assert shas['jre.tar.gz'] == 'jresha'  # JRE integrity-checked against Adoptium's checksum
    assert shas[f'forge-installer-{FORGE_VERSION}.tar.bz2'] == 'fsha'  # Forge verified too
    assert forge_dir.is_dir()  # atomically published
    assert not (tmp_path / '.forge.incomplete').exists()  # staging renamed away


def test_partial_fetch_does_not_publish_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An extract failure must leave NO resolvable install (no truncated jar)."""
    monkeypatch.setattr(
        'pipeline.sim.forge_runtime._download_verified',
        lambda _url, dest, *, sha256: dest.write_bytes(b''),
    )
    monkeypatch.setattr('pipeline.sim.forge_runtime._temurin_asset', lambda: ('http://jre', None))

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError('extract exploded')

    monkeypatch.setattr('pipeline.sim.forge_runtime.tarfile.open', _boom)

    forge_dir = tmp_path / 'forge'
    with pytest.raises(RuntimeError, match='extract exploded'):
        fr._fetch_and_extract(
            forge_dir=forge_dir,
            jre_dir=forge_dir / 'jre',
            forge_url='http://forge',
            forge_sha256='fsha',
        )
    assert not forge_dir.exists()  # no partial install published
    assert not (tmp_path / '.forge.incomplete').exists()  # staging cleaned up
    with pytest.raises(ForgeUnavailableError):  # and it does not resolve as 'available'
        resolve(data_dir=tmp_path)
