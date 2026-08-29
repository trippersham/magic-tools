"""Tests for the ECJ fetch/pin + Driver compile-and-cache surface (Phase 2, 2.1-2.3).

The fetch/pin unit tests are fully offline (download mocked). The real-compile tests
run the ECJ jar on the provisioned JRE; they SKIP unless the pinned ECJ jar can be
fetched (``ensure_ecj`` → :data:`pipeline.sim.driver_compile.ECJ_URL`) and a runnable
``java`` is found (``MAKE_MAGIC_JAVA`` / PATH), so CI without the toolchain stays green.
The ``@pytest.mark.integration`` variant additionally compiles against the REAL local dist jar.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from pipeline.sim import driver_compile as dc
from pipeline.sim import xmage_runtime as xr

_LOCAL_DIST = (
    Path(__file__).resolve().parents[1]
    / 'pipeline'
    / 'sim'
    / 'java'
    / 'xmage-dist'
    / 'target'
    / 'make-magic-xmage-dist.jar'
)


def _runnable_java() -> str | None:
    """A ``java`` launcher that actually runs, or ``None`` (tests skip)."""
    java = os.environ.get('MAKE_MAGIC_JAVA') or shutil.which('java')
    if not java:
        return None
    try:
        subprocess.run([java, '-version'], capture_output=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return java


def _stage_real_ecj(data_dir: Path) -> None:
    """Fetch the pinned REAL ECJ jar into the tools cache, or skip if it can't be reached."""
    try:
        dc.ensure_ecj(data_dir=data_dir)
    except dc.DriverCompileToolError as exc:
        pytest.skip(f'pinned ECJ jar unreachable (offline?): {exc}')


def _empty_jar(path: Path, marker: bytes) -> Path:
    """A VALID (ECJ-acceptable) empty-ish jar whose bytes vary with ``marker`` — so two
    calls yield jars with distinct sha256 (drives the cache-invalidation test)."""
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('marker.txt', marker)
    return path


# --------------------------------------------------------------------------- 2.1


def _stage_cached_ecj(monkeypatch: pytest.MonkeyPatch, data_dir: Path, body: bytes = b'fake-ecj') -> Path:
    tools = data_dir / 'xmage' / 'tools'
    tools.mkdir(parents=True, exist_ok=True)
    jar = tools / dc._ECJ_JAR_NAME
    jar.write_bytes(body)
    monkeypatch.setattr(dc, 'ECJ_SHA256', hashlib.sha256(body).hexdigest())
    return jar


def test_ecj_cache_hit_rehashes_and_returns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A cached ECJ jar is re-hashed against the pin and returned WITHOUT a download."""
    jar = _stage_cached_ecj(monkeypatch, tmp_path)
    monkeypatch.setattr(
        'pipeline.sim.forge_runtime._download_verified',
        lambda *a, **k: pytest.fail('must not download on a cache hit'),
    )
    assert dc.ensure_ecj(data_dir=tmp_path) == jar


def test_ecj_none_pin_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A ``None`` ECJ pin refuses to fetch (fail-closed, mirrors the dist-jar gate)."""
    monkeypatch.setattr(dc, 'ECJ_SHA256', None)
    monkeypatch.setattr(
        'pipeline.sim.forge_runtime._download_verified',
        lambda *a, **k: pytest.fail('must not download without a pinned SHA'),
    )
    with pytest.raises(dc.DriverCompileToolError, match='SHA256'):
        dc.ensure_ecj(data_dir=tmp_path)


def test_ecj_cached_mismatch_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A cached ECJ jar whose bytes no longer match the pin is REFUSED, not returned."""
    tools = tmp_path / 'xmage' / 'tools'
    tools.mkdir(parents=True)
    (tools / dc._ECJ_JAR_NAME).write_bytes(b'swapped ecj bytes')
    monkeypatch.setattr(dc, 'ECJ_SHA256', 'a' * 64)  # does not match
    with pytest.raises(dc.DriverCompileToolError, match='integrity'):
        dc.ensure_ecj(data_dir=tmp_path)


def test_ecj_fetch_on_miss_verifies_and_publishes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """On a miss with a pinned SHA, ensure_ecj downloads, verifies, atomically publishes."""
    body = b'downloaded ecj bytes'
    monkeypatch.setattr(dc, 'ECJ_SHA256', hashlib.sha256(body).hexdigest())

    def _fake_download(url: str, dest: Path, *, sha256: str | None) -> None:
        assert url == dc.ECJ_URL
        Path(dest).write_bytes(body)

    monkeypatch.setattr('pipeline.sim.forge_runtime._download_verified', _fake_download)
    jar = dc.ensure_ecj(data_dir=tmp_path)
    assert jar == tmp_path / 'xmage' / 'tools' / dc._ECJ_JAR_NAME
    assert jar.read_bytes() == body


def test_ecj_url_pins_the_version() -> None:
    """The fetch URL must target the pinned :data:`ECJ_VERSION` on Maven Central."""
    assert dc.ECJ_URL.endswith(f'ecj-{dc.ECJ_VERSION}.jar')
    assert dc.ECJ_VERSION in dc.ECJ_URL


# --------------------------------------------------------------------------- 2.2


def test_parse_diagnostics_structures_ecj_stderr() -> None:
    """ECJ's stderr block format is parsed into structured Diagnostic records."""
    stderr = (
        '----------\n'
        '1. ERROR in /x/Bad.java (at line 2)\n'
        '\tpublic int oops() { return notdefined + ; }\n'
        '\t                                      ^\n'
        'Syntax error on token "+", ++ expected\n'
        '----------\n'
        '1 problem (1 error)\n'
    )
    diags = dc._parse_diagnostics(stderr)
    assert len(diags) == 1
    assert diags[0].severity == 'ERROR'
    assert diags[0].file == '/x/Bad.java'
    assert diags[0].line == 2
    assert 'Syntax error' in diags[0].message


def test_compile_driver_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A tiny java.lang-only Driver compiles to a .class (real ECJ on the JRE)."""
    java = _runnable_java()
    if java is None:
        pytest.skip('no runnable java (set MAKE_MAGIC_JAVA)')
    monkeypatch.setenv('MAKE_MAGIC_JAVA', java)
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    _stage_real_ecj(tmp_path)
    # An empty-but-valid jar stands in for the dist classpath (source needs no dist API).
    override = _empty_jar(tmp_path / 'fake-dist.jar', b'A')
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_DIST_JAR', str(override))

    src = tmp_path / 'Tiny.java'
    src.write_text(
        'import java.util.List;\n'
        'public class Tiny {\n'
        '    public List<String> names() { return List.of("a", "b"); }\n'
        '}\n',
        encoding='utf-8',
    )
    result = dc.compile_driver(src, data_dir=tmp_path)
    assert result.ok, result.raw_stderr
    assert result.class_dir is not None
    assert (result.class_dir / 'Tiny.class').is_file()
    assert result.cache_hit is False

    # Second call: identical (dist SHA, source hash) → cache hit, no recompile.
    again = dc.compile_driver(src, data_dir=tmp_path)
    assert again.ok and again.cache_hit is True


def test_packaged_driver_cache_hits_on_second_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P4 discovery #3: a PACKAGED driver's .class lands under <out>/<pkg>/<Class>.class, not
    <out>/<stem>.class — so the cache-hit probe must use the fqcn or it recompiles every call.
    With the fqcn threaded, the second identical call is a cache HIT."""
    java = _runnable_java()
    if java is None:
        pytest.skip('no runnable java (set MAKE_MAGIC_JAVA)')
    monkeypatch.setenv('MAKE_MAGIC_JAVA', java)
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    _stage_real_ecj(tmp_path)
    override = _empty_jar(tmp_path / 'fake-dist.jar', b'A')
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_DIST_JAR', str(override))

    fqcn = 'makemagic.driver.d_abc123.Driver'
    src = tmp_path / 'Driver.java'
    src.write_text(
        'package makemagic.driver.d_abc123;\npublic final class Driver { }\n', encoding='utf-8'
    )
    first = dc.compile_driver(src, fqcn=fqcn, data_dir=tmp_path)
    assert first.ok and first.cache_hit is False
    assert first.class_dir is not None
    # the .class really is under the package subpath, NOT the bare stem.
    assert (first.class_dir / 'makemagic' / 'driver' / 'd_abc123' / 'Driver.class').is_file()
    assert not (first.class_dir / 'Driver.class').is_file()

    # Second identical call → the fqcn-aware probe finds the packaged .class → cache HIT.
    again = dc.compile_driver(src, fqcn=fqcn, data_dir=tmp_path)
    assert again.ok and again.cache_hit is True


def test_compile_driver_diagnostics(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A syntactically broken Driver returns ok=False with PARSED diagnostics."""
    java = _runnable_java()
    if java is None:
        pytest.skip('no runnable java (set MAKE_MAGIC_JAVA)')
    monkeypatch.setenv('MAKE_MAGIC_JAVA', java)
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    _stage_real_ecj(tmp_path)
    override = _empty_jar(tmp_path / 'fake-dist.jar', b'A')
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_DIST_JAR', str(override))

    src = tmp_path / 'Bad.java'
    src.write_text('public class Bad { int oops() { return x + ; } }\n', encoding='utf-8')
    result = dc.compile_driver(src, data_dir=tmp_path)
    assert result.ok is False
    assert result.class_dir is None
    assert result.diagnostics  # structured, not a raw dump
    assert any(d.severity == 'ERROR' for d in result.diagnostics)
    assert result.raw_stderr  # raw kept for escalation


# --------------------------------------------------------------------------- 2.3


def test_driver_cache_invalidates_on_dist_bump(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Same source + a CHANGED effective dist SHA ⇒ cache miss ⇒ recompile."""
    java = _runnable_java()
    if java is None:
        pytest.skip('no runnable java (set MAKE_MAGIC_JAVA)')
    monkeypatch.setenv('MAKE_MAGIC_JAVA', java)
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    _stage_real_ecj(tmp_path)

    src = tmp_path / 'Tiny.java'
    src.write_text('public class Tiny { }\n', encoding='utf-8')

    override_a = _empty_jar(tmp_path / 'dist-a.jar', b'AAAA')
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_DIST_JAR', str(override_a))
    first = dc.compile_driver(src, data_dir=tmp_path)
    assert first.ok and first.cache_hit is False

    # Swap to a DIFFERENT dist jar (different sha) → different cache key → recompile.
    override_b = _empty_jar(tmp_path / 'dist-b.jar', b'BBBBBBBB')
    assert xr._sha256_of(override_a) != xr._sha256_of(override_b)
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_DIST_JAR', str(override_b))
    second = dc.compile_driver(src, data_dir=tmp_path)
    assert second.ok and second.cache_hit is False  # NOT a hit — dist bump invalidated it


def test_release_level_matches_dist_floor() -> None:
    """The compile release level is a single named constant at the JRE/dist runtime level."""
    assert dc.DRIVER_RELEASE_LEVEL == 21


@pytest.mark.integration
def test_compile_driver_against_real_dist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A real ComputerPlayer7 subclass compiles against the REAL local dist jar (proves the
    classpath wiring end-to-end). Opt-in: needs the 79 MB local dist + JRE + ECJ."""
    java = _runnable_java()
    if java is None:
        pytest.skip('no runnable java (set MAKE_MAGIC_JAVA)')
    if not _LOCAL_DIST.is_file():
        pytest.skip(f'no local dist jar at {_LOCAL_DIST}')
    monkeypatch.setenv('MAKE_MAGIC_JAVA', java)
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    _stage_real_ecj(tmp_path)
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_DIST_JAR', str(_LOCAL_DIST))

    src = tmp_path / 'Driver.java'
    src.write_text(
        'import mage.player.ai.ComputerPlayer7;\n'
        'import mage.constants.RangeOfInfluence;\n'
        'public class Driver extends ComputerPlayer7 {\n'
        '    public Driver(String name, RangeOfInfluence range, int skill) {\n'
        '        super(name, range, skill);\n'
        '    }\n'
        '}\n',
        encoding='utf-8',
    )
    result = dc.compile_driver(src, data_dir=tmp_path)
    assert result.ok, result.raw_stderr
    assert result.class_dir is not None and (result.class_dir / 'Driver.class').is_file()
