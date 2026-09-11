"""Tests for the XMage install resolver + auto-provision fetch (2.3b Phase C).

Offline: no reactor, no network. Exercises the TWO install modes (built reactor vs
the fetched shaded jar), the never-crash error contract, and ``ensure``'s
resolve-then-fetch flow with the download mocked.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from pipeline.sim import xmage_runtime as xr
from pipeline.sim.xmage_runtime import XMageUnavailableError


def _stage_cached_jar(monkeypatch: pytest.MonkeyPatch, data_dir: Path, content: bytes = b'PK\x03\x04 jar') -> Path:
    """Write a cached dist jar under ``<data_dir>/xmage/`` and PIN its real SHA, so a
    cache-hit ``resolve`` passes the integrity re-check. Returns the jar path."""
    xmage = data_dir / 'xmage'
    xmage.mkdir(exist_ok=True)
    jar = xmage / xr._DIST_JAR_NAME
    jar.write_bytes(content)
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', hashlib.sha256(content).hexdigest())
    return jar


def _fake_reactor(tmp_path: Path) -> Path:
    """A minimal built-reactor layout resolve() accepts (Mage.Tests + classpath cache)."""
    home = tmp_path / 'reactor'
    (home / 'Mage.Tests').mkdir(parents=True)
    (home / xr._CLASSPATH_CACHE).write_text('/fake/dep1.jar:/fake/dep2.jar', encoding='utf-8')
    return home


def test_resolve_rejects_zero_byte_cached_jar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A 0-byte cached jar (interrupted write / bad copy / disk-full truncation) must
    resolve as NOT installed — not as available, deferring to an opaque JVM crash."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    (tmp_path / 'xmage').mkdir()
    (tmp_path / 'xmage' / xr._DIST_JAR_NAME).write_bytes(b'')  # zero bytes
    with pytest.raises(XMageUnavailableError):
        xr.resolve(data_dir=tmp_path)


def test_ensure_none_sha_refuses_before_on_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no pinned SHA the fetch is refused (fail-closed) BEFORE the on_fetch notice
    fires — so the user never sees a misleading 'downloading…' then an instant abort."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', None)
    fired: list[bool] = []

    def _boom(*_a: object, **_k: object) -> None:
        pytest.fail('must not attempt a download when the SHA gate is closed')

    monkeypatch.setattr('pipeline.sim.forge_runtime._download_verified', _boom)
    with pytest.raises(XMageUnavailableError, match='without a pinned SHA256'):
        xr.ensure(data_dir=tmp_path, on_fetch=lambda: fired.append(True))
    assert fired == []  # the notice never fired


def test_dist_url_couples_to_the_dist_tag() -> None:
    """The runtime fetch URL must target :data:`_DIST_TAG` — the exact tag the workflow
    publishes to — and that tag must stay prefixed with the built XMage version, so a
    hardcoded-URL drift / version skew is caught."""
    assert xr.XMAGE_DIST_URL.endswith(f'{xr._DIST_TAG}/{xr._DIST_JAR_NAME}')
    assert xr._DIST_TAG.startswith(f'xmage-dist-{xr.XMAGE_VERSION}')


def test_resolve_reactor_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """MAKE_MAGIC_XMAGE_HOME set + valid → reactor install (harness jar FIRST on cp)."""
    home = _fake_reactor(tmp_path)
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_HOME', str(home))
    install = xr.resolve()
    assert install.mage_tests_dir == home / 'Mage.Tests'
    assert install.classpath.startswith(str(xr._HARNESS_JAR))  # shadow jar wins class-load
    assert '/fake/dep1.jar' in install.classpath


def test_resolve_dist_override_prepends_harness_jar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """MAKE_MAGIC_XMAGE_DIST_JAR set → classpath is [harness jar, override jar] in that
    order. The committed harness jar MUST sort FIRST so its fresh XMageBatch shadows the
    STALE shaded XMageBatch bundled in the dist (else the run silently degrades to bare
    CP7, ignoring -Dmakemagic.driver, and exits 0 — defeating the driver gate)."""
    override = tmp_path / 'make-magic-xmage-dist.jar'
    override.write_bytes(b'PK\x03\x04 dist jar with a STALE shaded XMageBatch')
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_DIST_JAR', str(override))
    install = xr.resolve(data_dir=tmp_path)
    parts = install.classpath.split(os.pathsep)
    assert parts[0] == str(xr._HARNESS_JAR)  # committed harness shadows the stale dist class
    assert parts[1] == str(override)
    assert len(parts) == 2


def test_resolve_cached_jar_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No reactor env, but a fetched shaded jar cached under <data_dir>/xmage/ →
    a self-contained install: classpath is the ONE jar, cwd is that dir."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    jar = _stage_cached_jar(monkeypatch, tmp_path, b'PK\x03\x04 fake jar')
    install = xr.resolve(data_dir=tmp_path)
    assert install.classpath == str(jar)  # the shaded jar IS the classpath
    assert install.mage_tests_dir == tmp_path / 'xmage'  # cwd where CardScanner builds db/


def test_resolve_rejects_tampered_cached_jar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A cached jar whose bytes no longer match the pinned SHA (swapped/corrupted after
    caching) is REFUSED on cache-hit — not launched — so `ensure` re-fetches over it."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    xmage = tmp_path / 'xmage'
    xmage.mkdir()
    (xmage / xr._DIST_JAR_NAME).write_bytes(b'swapped malicious bytes')
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'a' * 64)  # pin does NOT match the bytes
    with pytest.raises(XMageUnavailableError, match='integrity check'):
        xr.resolve(data_dir=tmp_path)


def test_resolve_tolerates_cached_jar_when_sha_unpinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """In the transient release-cut window (pin=None) integrity is unverifiable, so a
    cached jar resolves on existence + size alone (no re-hash to compare against)."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    xmage = tmp_path / 'xmage'
    xmage.mkdir()
    jar = xmage / xr._DIST_JAR_NAME
    jar.write_bytes(b'PK\x03\x04 unverifiable but present')
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', None)
    assert xr.resolve(data_dir=tmp_path).classpath == str(jar)


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
    body = b'PK\x03\x04 fetched jar'
    # Pin the fetched bytes' real SHA: passes the fetch gate AND the cache-hit re-check
    # resolve() now runs after the (mocked) download publishes the jar.
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', hashlib.sha256(body).hexdigest())

    def _fake_download(url: str, dest: Path, *, sha256: str | None) -> None:
        dest.write_bytes(body)  # simulate a verified download

    monkeypatch.setattr('pipeline.sim.forge_runtime._download_verified', _fake_download)
    fetched: list[bool] = []
    install = xr.ensure(data_dir=tmp_path, on_fetch=lambda: fetched.append(True))
    assert fetched == [True]  # the notice fired once, on the fetch path
    assert install.classpath == str(tmp_path / 'xmage' / xr._DIST_JAR_NAME)


def test_ensure_fetch_failure_cleans_up_and_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A failed/unverified download leaves NO partial jar cached and raises cleanly."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'dead' * 16)  # pin so the fail-closed gate lets the mocked fetch run

    def _boom(url: str, dest: Path, *, sha256: str | None) -> None:
        dest.write_bytes(b'partial')  # a partial write...
        raise ValueError('SHA256 mismatch')  # ...that then fails verification

    monkeypatch.setattr('pipeline.sim.forge_runtime._download_verified', _boom)
    with pytest.raises(XMageUnavailableError, match='could not fetch'):
        xr.ensure(data_dir=tmp_path)
    assert not (tmp_path / 'xmage' / xr._DIST_JAR_NAME).exists()  # partial cleaned up


def test_ensure_prefers_existing_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ensure() resolves an already-cached (integrity-verified) jar WITHOUT downloading."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    _stage_cached_jar(monkeypatch, tmp_path, b'PK cached')  # pins its matching SHA

    def _never(url: str, dest: Path, *, sha256: str | None) -> None:
        pytest.fail('should not download when an install already resolves')

    monkeypatch.setattr('pipeline.sim.forge_runtime._download_verified', _never)
    install = xr.ensure(data_dir=tmp_path)
    assert install.classpath == str(tmp_path / 'xmage' / xr._DIST_JAR_NAME)


def test_ensure_stages_then_atomically_publishes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The download targets a `.incomplete` staging path; the final jar appears only via
    an atomic os.replace — so resolve() never sees a half-written jar at the trusted path."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    body = b'PK\x03\x04 verified jar'
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', hashlib.sha256(body).hexdigest())  # matches the published bytes
    final = tmp_path / 'xmage' / xr._DIST_JAR_NAME
    seen: dict[str, Path] = {}

    def _fake_download(url: str, dest: Path, *, sha256: str | None) -> None:
        seen['dest'] = Path(dest)
        assert not final.exists()  # the final (trusted) path must not exist mid-download
        Path(dest).write_bytes(body)

    monkeypatch.setattr('pipeline.sim.forge_runtime._download_verified', _fake_download)
    install = xr.ensure(data_dir=tmp_path)
    assert seen['dest'].name.endswith('.incomplete')  # staged, not written in place
    assert final.is_file()  # atomically published
    assert not seen['dest'].exists()  # staging consumed by os.replace
    assert install.classpath == str(final)


def test_local_dist_override_resolves_that_jar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """MAKE_MAGIC_XMAGE_DIST_JAR set → resolve() uses THAT jar directly (no fetch), and
    effective_dist_sha256() returns the jar's REAL hash (the compile-cache / classpath key)."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    body = b'PK\x03\x04 local override jar'
    local = tmp_path / 'somewhere' / 'my-dist.jar'
    local.parent.mkdir()
    local.write_bytes(body)
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_DIST_JAR', str(local))
    # pin is irrelevant under the override — set it to a NON-matching value to prove the
    # override neither fetches nor SHA-verifies against the code pin.
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'a' * 64)

    install = xr.resolve(data_dir=tmp_path)
    # classpath = [committed harness jar, override jar]: the harness shadows the STALE
    # shaded XMageBatch in the dist so -Dmakemagic.driver is honored (not silent CP7).
    assert install.classpath == os.pathsep.join((str(xr._HARNESS_JAR), str(local)))
    assert xr.effective_dist_sha256(data_dir=tmp_path) == hashlib.sha256(body).hexdigest()


def test_local_dist_override_takes_precedence_over_reactor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The explicit dist-jar override wins over a set MAKE_MAGIC_XMAGE_HOME reactor."""
    home = _fake_reactor(tmp_path)
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_HOME', str(home))
    local = tmp_path / 'my-dist.jar'
    local.write_bytes(b'PK override')
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_DIST_JAR', str(local))
    # override wins over the reactor; harness jar still prepended to shadow the stale dist class.
    assert xr.resolve().classpath == os.pathsep.join((str(xr._HARNESS_JAR), str(local)))


def test_local_dist_override_missing_fails_loudly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A set-but-missing override must raise, not silently fall through to the fetch path."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_DIST_JAR', str(tmp_path / 'nope.jar'))
    with pytest.raises(XMageUnavailableError, match='MAKE_MAGIC_XMAGE_DIST_JAR'):
        xr.resolve(data_dir=tmp_path)


def test_effective_sha_unset_override_is_the_code_pin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Override UNSET → effective_dist_sha256() is the code-pinned XMAGE_DIST_SHA256
    verbatim (fail-closed None in the release-cut window); the production path is unchanged."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_DIST_JAR', raising=False)
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', None)
    assert xr.effective_dist_sha256(data_dir=tmp_path) is None
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'b' * 64)
    assert xr.effective_dist_sha256(data_dir=tmp_path) == 'b' * 64


def test_ensure_none_sha_still_fails_closed_without_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With NO override and a None pin, ensure() still refuses to fetch (production path
    byte-for-byte unchanged by the override feature being present)."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_DIST_JAR', raising=False)
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', None)
    monkeypatch.setattr(
        'pipeline.sim.forge_runtime._download_verified',
        lambda *a, **k: pytest.fail('must not download when the SHA gate is closed'),
    )
    with pytest.raises(XMageUnavailableError, match='without a pinned SHA256'):
        xr.ensure(data_dir=tmp_path)


def test_ensure_crash_after_partial_write_leaves_no_trusted_jar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A crash AFTER a partial write (SIGKILL/disk-full analogue) must leave neither a
    truncated jar at the final path nor a leftover staging file."""
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'dead' * 16)  # pin so the fail-closed gate lets the mocked fetch run
    final = tmp_path / 'xmage' / xr._DIST_JAR_NAME
    staging = tmp_path / 'xmage' / f'{xr._DIST_JAR_NAME}.incomplete'

    def _boom(url: str, dest: Path, *, sha256: str | None) -> None:
        Path(dest).write_bytes(b'truncated')  # partial bytes land in staging...
        raise ValueError('SHA256 mismatch')  # ...then verification fails

    monkeypatch.setattr('pipeline.sim.forge_runtime._download_verified', _boom)
    with pytest.raises(XMageUnavailableError, match='could not fetch'):
        xr.ensure(data_dir=tmp_path)
    assert not final.exists()  # never a truncated jar at the trusted path
    assert not staging.exists()  # staging cleaned up too


# --------------------------------------------------------------------------- #
# JRE version gate — fail fast when the run JRE is older than the driver       #
# bytecode target (Java 21). Prevents the opaque UnsupportedClassVersionError  #
# deep in the harness that only surfaces as "driver never registered".         #
# --------------------------------------------------------------------------- #


def _java_banner(version: str) -> str:
    return f'openjdk version "{version}" 2026-01-01\nOpenJDK Runtime Environment (build {version})'


def test_jre_gate_rejects_older_than_driver_target() -> None:
    with pytest.raises(XMageUnavailableError, match='Java 21'):
        xr.gate_jre_major(Path('/x/java'), probe=lambda _p: _java_banner('17.0.9'))


def test_jre_gate_accepts_the_driver_target() -> None:
    xr.gate_jre_major(Path('/x/java'), probe=lambda _p: _java_banner('21.0.12'))  # no raise


def test_jre_gate_accepts_a_newer_runtime() -> None:
    xr.gate_jre_major(Path('/x/java'), probe=lambda _p: _java_banner('26.0.2'))  # 65.0 loads on >=21


def test_jre_gate_refuses_an_unidentifiable_runtime() -> None:
    with pytest.raises(XMageUnavailableError, match=r'parse|unidentifiable'):
        xr.gate_jre_major(Path('/x/java'), probe=lambda _p: 'gibberish, no version here')
