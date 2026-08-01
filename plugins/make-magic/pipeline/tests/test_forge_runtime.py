"""OFFLINE tests for the Forge runtime locator (:mod:`pipeline.sim.forge_runtime`).

Covers the override-first resolution ladder, the mocked fetch path of
``ensure()`` (NEVER a real download), and the loud ``ForgeUnavailableError``
when nothing is resolvable. Every filesystem artifact is a tmp-dir stub — no
real Forge install, jar, or JRE is touched here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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

    def _fake_fetch(*, forge_dir: Path, jre_dir: Path, forge_url: str, jre_url: str, forge_sha256: str) -> None:
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
