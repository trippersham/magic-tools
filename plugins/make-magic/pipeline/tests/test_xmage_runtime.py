"""Tests for the XMage install resolver + auto-provision fetch (2.3b Phase C).

Offline: no reactor, no network. Exercises the TWO install modes (built reactor vs
the fetched shaded jar), the never-crash error contract, and ``ensure``'s
resolve-then-fetch flow with the download mocked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.sim import xmage_runtime as xr
from pipeline.sim.xmage_runtime import XMageUnavailableError


def _fake_reactor(tmp_path: Path) -> Path:
    """A minimal built-reactor layout resolve() accepts (Mage.Tests + classpath cache)."""
    home = tmp_path / 'reactor'
    (home / 'Mage.Tests').mkdir(parents=True)
    (home / xr._CLASSPATH_CACHE).write_text('/fake/dep1.jar:/fake/dep2.jar', encoding='utf-8')
    return home


def test_resolve_reactor_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """MAKE_MAGIC_XMAGE_HOME set + valid → reactor install (harness jar FIRST on cp)."""
    home = _fake_reactor(tmp_path)
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_HOME', str(home))
    install = xr.resolve()
    assert install.mage_tests_dir == home / 'Mage.Tests'
    assert install.classpath.startswith(str(xr._HARNESS_JAR))  # shadow jar wins class-load
    assert '/fake/dep1.jar' in install.classpath


def test_resolve_cached_jar_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No reactor env, but a fetched shaded jar cached under <data_dir>/xmage/ →
    a self-contained install: classpath is the ONE jar, cwd is that dir."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    xmage_dir = tmp_path / 'xmage'
    xmage_dir.mkdir()
    jar = xmage_dir / xr._DIST_JAR_NAME
    jar.write_bytes(b'PK\x03\x04 fake jar')
    install = xr.resolve(data_dir=tmp_path)
    assert install.classpath == str(jar)  # the shaded jar IS the classpath
    assert install.mage_tests_dir == xmage_dir  # cwd where CardScanner builds db/


def test_resolve_neither_names_both_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Neither install present → an error naming BOTH how-to-enable paths."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    with pytest.raises(XMageUnavailableError) as exc:
        xr.resolve(data_dir=tmp_path)
    msg = str(exc.value)
    assert 'doctor --provision' in msg and 'MAKE_MAGIC_XMAGE_HOME' in msg


def test_ensure_fetches_on_miss(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ensure() downloads the shaded jar into <data_dir>/xmage/ on a miss, then resolves."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)

    def _fake_download(url: str, dest: Path, *, sha256: str | None) -> None:
        dest.write_bytes(b'PK\x03\x04 fetched jar')  # simulate a verified download

    monkeypatch.setattr('pipeline.sim.forge_runtime._download_verified', _fake_download)
    fetched: list[bool] = []
    install = xr.ensure(data_dir=tmp_path, on_fetch=lambda: fetched.append(True))
    assert fetched == [True]  # the notice fired once, on the fetch path
    assert install.classpath == str(tmp_path / 'xmage' / xr._DIST_JAR_NAME)


def test_ensure_fetch_failure_cleans_up_and_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A failed/unverified download leaves NO partial jar cached and raises cleanly."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)

    def _boom(url: str, dest: Path, *, sha256: str | None) -> None:
        dest.write_bytes(b'partial')  # a partial write...
        raise ValueError('SHA256 mismatch')  # ...that then fails verification

    monkeypatch.setattr('pipeline.sim.forge_runtime._download_verified', _boom)
    with pytest.raises(XMageUnavailableError, match='could not fetch'):
        xr.ensure(data_dir=tmp_path)
    assert not (tmp_path / 'xmage' / xr._DIST_JAR_NAME).exists()  # partial cleaned up


def test_ensure_prefers_existing_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ensure() resolves an already-cached jar WITHOUT downloading."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    (tmp_path / 'xmage').mkdir()
    (tmp_path / 'xmage' / xr._DIST_JAR_NAME).write_bytes(b'PK cached')

    def _never(url: str, dest: Path, *, sha256: str | None) -> None:
        pytest.fail('should not download when an install already resolves')

    monkeypatch.setattr('pipeline.sim.forge_runtime._download_verified', _never)
    install = xr.ensure(data_dir=tmp_path)
    assert install.classpath == str(tmp_path / 'xmage' / xr._DIST_JAR_NAME)
