"""Compile a per-deck Driver ``.java`` on a plain JRE, with a fetch-pinned ECJ + a cache.

The ``authoring-drivers`` loop needs to compile ONE small ``ComputerPlayer7`` subclass
against the SHA-pinned shaded dist jar into a loadable ``.class`` — **on the end user's
machine, which only has a JRE** (Temurin 21), never a JDK/``javac``. So this module ships
the **Eclipse Compiler for Java (ECJ)** standalone jar (fetch-and-pinned exactly like the
dist jar) and shells out to it with the same ``java`` the sim engine already uses:

    <jre>/bin/java -jar <ecj.jar> --release <lvl> -proc:none -cp <dist.jar> -d <out> Driver.java

See ``research/compile-toolchain-research.md`` for why ECJ (JRE-only, full-language, ~3.2 MB)
over a JDK upgrade / Janino / JEP-330.

**Release level (load-bearing, empirically pinned — see :data:`DRIVER_RELEASE_LEVEL`).**

**Cache.** The compiled artifact is keyed by **(effective dist SHA, source hash)** — a dist
bump (or a local-dist swap) changes the key and forces a recompile, since a dist API change
can silently invalidate a Driver compiled against the old classpath. A key hit skips ECJ.

**Diagnostics.** On failure, ECJ's stderr is parsed into structured
:class:`Diagnostic` records (not a raw dump) so the authoring loop can act on them; the raw
stderr is retained for escalation. A Driver that does not compile is never cached.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pipeline.sim import xmage_runtime as xr

__all__ = (
    'DRIVER_RELEASE_LEVEL',
    'ECJ_SHA256',
    'ECJ_URL',
    'ECJ_VERSION',
    'CompileResult',
    'Diagnostic',
    'DriverCompileError',
    'DriverCompileToolError',
    'compile_driver',
    'compile_for_injection',
    'ensure_ecj',
)

_log = logging.getLogger(__name__)

#: The pinned ECJ artifact. Eclipse Compiler for Java standalone batch compiler — a pure-Java,
#: zero-runtime-dep jar that runs on a plain JRE 17+ (we provision Temurin 21). EPL 2.0;
#: redistributed/fetched verbatim, never folded into the shaded dist.
ECJ_VERSION = '3.46.0'
#: Maven Central publishes immutable, checksummed artifacts — reuse the dist-jar fetch model.
ECJ_URL = f'https://repo1.maven.org/maven2/org/eclipse/jdt/ecj/{ECJ_VERSION}/ecj-{ECJ_VERSION}.jar'
#: SHA256 of ``ecj-3.46.0.jar`` from Maven Central (cross-checked against the published
#: ``.sha1`` ``e962128c…`` at fetch time). The fail-closed integrity gate: ``None`` refuses
#: to fetch; a cache-hit that no longer matches is refused, mirroring the dist jar.
ECJ_SHA256: str | None = 'd0d43f8e2d7003e5efed612e2cbb5f01870043397d8f1bbe536fd9128f4fcbf7'
_ECJ_JAR_NAME = f'ecj-{ECJ_VERSION}.jar'

#: The ``--release`` level ECJ compiles a Driver at. **21**, derived empirically, NOT blindly:
#:
#:  * The dist jar's runtime FLOOR is Java 17 — its highest class-file major is 61 (the seam
#:    classes ``mage.collectors.*``); the XMage engine incl. ``ComputerPlayer7`` a Driver
#:    extends is major 52 (Java 8). So the JVM that runs the dist must be ≥ 17.
#:  * We provision a Temurin **21** JRE, and — critically — ECJ's ``--release N`` for any N
#:    below the *running* JVM's own major needs the JDK ``lib/ct.sym`` system-description
#:    file, which a JRE does NOT ship: ``--release 8`` / ``--release 17`` on our JRE NPE
#:    ("Cannot invoke … because this.fs is null"). Only ``--release 21`` (== the JRE's own
#:    major) resolves the system API from the live runtime and compiles.
#:  * A Driver compiled at 21 emits class-file major 65, which loads on the same Java-21 JVM
#:    that runs the dist — so 21 is both the only level ECJ can honor on this JRE AND ≥ the
#:    dist's floor. Bind it to the provisioned JRE major if that ever changes.
DRIVER_RELEASE_LEVEL = 21

#: ECJ is fast on one small file; a stuck JVM must not hang the authoring loop forever.
_ECJ_TIMEOUT_S = 120.0

#: ECJ stderr diagnostic header, e.g. ``1. ERROR in /x/Bad.java (at line 2)``.
_DIAG_HEADER = re.compile(r'^\d+\.\s+(ERROR|WARNING|INFO)\s+in\s+(.+?)\s+\(at line (\d+)\)$')
#: The ``----------`` rules ECJ brackets each diagnostic block with.
_DIAG_DIVIDER = re.compile(r'^-{3,}$', re.MULTILINE)


class DriverCompileToolError(RuntimeError):
    """The ECJ toolchain could not be provisioned (fetch/pin/verify failure) — actionable."""


class DriverCompileError(RuntimeError):
    """ECJ rejected the Driver ``.java`` — carries the parsed diagnostics for the repair loop."""

    def __init__(self, source: Path, result: CompileResult) -> None:
        self.source = source
        self.result = result
        detail = '; '.join(f'{d.file}:{d.line} {d.severity} {d.message}' for d in result.diagnostics)
        super().__init__(
            f'Driver {source} did not compile ({len(result.diagnostics)} diagnostic(s))'
            + (f': {detail}' if detail else f'.\n{result.raw_stderr}')
        )


@dataclass(frozen=True)
class Diagnostic:
    """One structured ECJ diagnostic parsed from stderr."""

    severity: str  #: ``ERROR`` | ``WARNING`` | ``INFO``.
    file: str
    line: int
    message: str


@dataclass(frozen=True)
class CompileResult:
    """The typed outcome of a Driver compile.

    ``ok`` True → ``class_dir`` holds the compiled ``.class`` (``cache_hit`` says whether ECJ
    actually ran). ``ok`` False → ``class_dir`` is ``None``, ``diagnostics`` carries the
    parsed ECJ errors, and ``raw_stderr`` the verbatim output for escalation.
    """

    ok: bool
    class_dir: Path | None
    diagnostics: tuple[Diagnostic, ...]
    raw_stderr: str
    cache_hit: bool


def _tools_dir(data_dir: str | os.PathLike[str] | None) -> Path:
    """``<data_dir>/xmage/tools/`` — where the fetched ECJ jar is cached."""
    return xr._dist_dir(data_dir) / 'tools'


def _verify_ecj_integrity(jar: Path) -> None:
    """Re-hash a cached ECJ jar against the pin; raise on mismatch (or fetch-refuse on None).

    Mirrors the dist jar's cache-hit re-verification: an ECJ jar swapped after caching (or
    on-disk corruption) is refused rather than executed. When the pin is ``None`` integrity is
    unverifiable, so existence stands alone (matches the dist-jar contract).
    """
    if ECJ_SHA256 is None:
        return
    from pipeline.sim.forge_runtime import _verify_sha256

    try:
        _verify_sha256(jar, ECJ_SHA256)
    except ValueError as exc:
        raise DriverCompileToolError(
            f'cached ECJ jar {jar} failed its integrity check ({exc}) — refusing to run a '
            'swapped/corrupted compiler; delete it to re-fetch a verified jar.'
        ) from exc


def ensure_ecj(data_dir: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the pinned ECJ jar, FETCHING it into the cache on a miss (fail-closed).

    A cached jar is re-hashed against :data:`ECJ_SHA256` and returned (no download). On a
    miss the jar is downloaded from :data:`ECJ_URL`, SHA-verified, and atomically published
    into ``<data_dir>/xmage/tools/`` (mirrors :func:`xmage_runtime.ensure`). A ``None`` pin
    refuses to fetch. Arch-independent — one jar serves every platform.
    """
    jar = _tools_dir(data_dir) / _ECJ_JAR_NAME
    if jar.is_file() and jar.stat().st_size > 0:
        _verify_ecj_integrity(jar)
        return jar

    if ECJ_SHA256 is None:
        raise DriverCompileToolError(f'refusing to fetch {ECJ_URL!r} without a pinned SHA256 checksum (fail-closed).')

    from pipeline.sim.forge_runtime import _download_verified

    tools = _tools_dir(data_dir)
    tools.mkdir(parents=True, exist_ok=True)
    staging = tools / f'{_ECJ_JAR_NAME}.incomplete'
    try:
        _download_verified(ECJ_URL, staging, sha256=ECJ_SHA256)
        os.replace(staging, jar)
    except Exception as exc:
        staging.unlink(missing_ok=True)  # never leave a partial/unverified compiler cached.
        raise DriverCompileToolError(f'could not fetch the ECJ jar from {ECJ_URL}: {exc}.') from exc
    return jar


def _parse_diagnostics(stderr: str) -> tuple[Diagnostic, ...]:
    """Parse ECJ's ``----------``-bracketed stderr blocks into :class:`Diagnostic` records.

    Each diagnostic block is a header line, the echoed source line, a caret line, then the
    message (last non-blank line of the block). The trailing ``N problem(s)`` summary block
    has no header and is ignored.
    """
    diags: list[Diagnostic] = []
    for block in _DIAG_DIVIDER.split(stderr):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        header = _DIAG_HEADER.match(lines[0].strip())
        if header is None:
            continue
        message = lines[-1].strip() if len(lines) > 1 else ''
        diags.append(
            Diagnostic(
                severity=header.group(1),
                file=header.group(2),
                line=int(header.group(3)),
                message=message,
            )
        )
    return tuple(diags)


def _driver_cache_dir(data_dir: str | os.PathLike[str] | None, dist_sha: str | None, src_hash: str) -> Path:
    """``<data_dir>/xmage/drivers/<dist-sha>-<src-hash>/`` — the (dist SHA, source) key."""
    key = f'{(dist_sha or "unpinned")[:16]}-{src_hash[:16]}'
    return xr._dist_dir(data_dir) / 'drivers' / key


def _compiled_class_file(out_dir: Path, source: Path, fqcn: str | None) -> Path:
    """The on-disk path ECJ writes the compiled ``.class`` to under ``-d out_dir``.

    A **packaged** Driver (the emitter always emits ``package makemagic.driver.d_<uuid>;``)
    lands under ``out_dir/<pkg-subpath>/<Class>.class`` — NOT ``out_dir/<stem>.class`` — so a
    cache-hit probe on ``<stem>.class`` never matches and ECJ recompiles every call
    (P4 discovery #3). When ``fqcn`` is known, resolve the true packaged path; otherwise fall
    back to the bare ``<stem>.class`` (an unpackaged/default-package source).
    """
    if fqcn and '.' in fqcn:
        *pkg_parts, cls = fqcn.split('.')
        return out_dir.joinpath(*pkg_parts, f'{cls}.class')
    return out_dir / f'{source.stem}.class'


def compile_driver(
    source: str | os.PathLike[str],
    *,
    fqcn: str | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> CompileResult:
    """Compile a single Driver ``.java`` against the effective dist jar, with caching.

    Resolves the JRE launcher + the effective dist classpath (honouring the
    ``MAKE_MAGIC_XMAGE_DIST_JAR`` local-dist override) via :mod:`xmage_runtime`, ensures the
    pinned ECJ jar, and — on a (dist SHA, source hash) cache MISS — runs
    ``java -jar ecj.jar --release <lvl> -proc:none -cp <dist> -d <out> <source>``. A cache
    hit returns immediately. A compile failure returns ``ok=False`` with parsed diagnostics
    and is NEVER cached.

    ``fqcn`` (the Driver's fully-qualified class name) lets the cache-hit probe find the
    **packaged** ``.class`` ECJ actually writes (``<out>/<pkg>/<Class>.class``); without it the
    probe falls back to ``<stem>.class`` and a packaged driver never cache-hits.
    """
    src = Path(source)
    install = xr.resolve(data_dir=data_dir)
    dist_sha = xr.effective_dist_sha256(data_dir=data_dir)
    src_hash = xr._sha256_of(src)
    out_dir = _driver_cache_dir(data_dir, dist_sha, src_hash)

    class_file = _compiled_class_file(out_dir, src, fqcn)
    if class_file.is_file():
        return CompileResult(ok=True, class_dir=out_dir, diagnostics=(), raw_stderr='', cache_hit=True)

    ecj = ensure_ecj(data_dir=data_dir)
    if out_dir.exists():  # a stale partial (e.g. a prior interrupted compile) — start clean.
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    cmd = [
        str(install.java),
        '-jar',
        str(ecj),
        '--release',
        str(DRIVER_RELEASE_LEVEL),
        '-proc:none',
        '-cp',
        install.classpath,
        '-d',
        str(out_dir),
        str(src),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_ECJ_TIMEOUT_S, check=False)
    diagnostics = _parse_diagnostics(proc.stderr)

    if proc.returncode != 0:
        shutil.rmtree(out_dir, ignore_errors=True)  # never cache a failed compile.
        _log.info('Driver compile failed for %s (exit %d): %d diagnostic(s).', src, proc.returncode, len(diagnostics))
        return CompileResult(ok=False, class_dir=None, diagnostics=diagnostics, raw_stderr=proc.stderr, cache_hit=False)

    return CompileResult(ok=True, class_dir=out_dir, diagnostics=diagnostics, raw_stderr=proc.stderr, cache_hit=False)


def compile_for_injection(
    source: str | os.PathLike[str],
    fqcn: str,
    *,
    data_dir: str | os.PathLike[str] | None = None,
) -> tuple[str, str]:
    """Compile a Driver ``.java`` and return the ``(classes_dir, fqcn)`` injection tuple.

    The one glue step the Phase-3 launch path needs between *authoring* and *running*: it
    runs :func:`compile_driver` (ECJ against the effective dist jar, cached) and, on success,
    returns exactly the tuple the XMage engine's ``driver=`` parameter takes — the compiled
    ``.class`` dir (prepended onto the run classpath so the Driver wins class-loading) plus the
    ``fqcn`` (threaded as ``-Dmakemagic.driver=<fqcn>``, which ``XMageBatch`` loads and whose
    ``static register(UUID)`` it reflectively invokes on PlayerA).

    ``fqcn`` is the caller's responsibility (it must match the Driver's declared
    package + class name); this helper does not parse it out of the source. A compile FAILURE
    raises :class:`DriverCompileError` with the parsed diagnostics (fail-loud — never returns a
    tuple pointing at an empty/partial class dir).
    """
    result = compile_driver(source, fqcn=fqcn, data_dir=data_dir)
    if not result.ok or result.class_dir is None:
        raise DriverCompileError(Path(source), result)
    return str(result.class_dir), fqcn
