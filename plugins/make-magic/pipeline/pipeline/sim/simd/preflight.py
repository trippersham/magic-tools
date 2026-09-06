"""Phase A2.4 — boot/env self-check: resolve + version-gate the JVM ONCE, fail LOUD.

A missing / non-executable / wrong-major-version ``java`` is a DETERMINISTIC config failure, not
a transient worker death — so it must be caught ONCE, up front, BEFORE any staging dir is made or
any worker is spawned. The old silent ``Path('java')`` fallback (``xmage_runtime._resolve_java``)
deferred the failure to an opaque per-worker JVM crash: every spawned worker died the same way,
the crash-loop respawner kept relaunching, and each relaunch staged a fresh card-DB copy — the
exact path that flooded 33k staging dirs to 99% disk on a real corpus. Preflight converts that
into one :class:`BootFailure` with zero workers and zero staging.

The version probe is injectable (``probe``) so the gate is unit-testable without a real JVM.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ('BootFailure', 'default_java_probe', 'java_major_version', 'preflight_java')


class BootFailure(RuntimeError):
    """A boot-time preflight failed DETERMINISTICALLY (bad/missing runtime, crash-loop breaker).

    Distinct from a transient mid-game worker death (which is a task retry): a ``BootFailure``
    means the run can NEVER make progress as configured, so it aborts LOUD with zero workers
    rather than respawning into an unbounded crash-loop.
    """


#: ``java -version`` prints e.g. ``openjdk version "21.0.3" 2024-04-16`` (modern) or
#: ``java version "1.8.0_401"`` (legacy 8). Capture the version token either way.
_VERSION_RE = re.compile(r'version "(\d+)(?:\.(\d+))?[^"]*"')


def java_major_version(version_output: str) -> int | None:
    """Parse the major version from ``java -version`` output, or ``None`` if unrecognized.

    Handles both the modern scheme (``"21.0.3"`` → 21) and the legacy ``1.x`` scheme
    (``"1.8.0_401"`` → 8), where the major lives in the SECOND dotted field.
    """
    m = _VERSION_RE.search(version_output)
    if m is None:
        return None
    first = int(m.group(1))
    if first == 1 and m.group(2) is not None:
        return int(m.group(2))  # legacy 1.8 → 8.
    return first


def default_java_probe(java: Path) -> str:
    """Run ``java -version`` and return its combined output (the version banner is on stderr).

    Raises :class:`FileNotFoundError` if the binary does not exist / is not executable — the
    caller (:func:`preflight_java`) converts that into a :class:`BootFailure`.
    """
    proc = subprocess.run([str(java), '-version'], capture_output=True, text=True, timeout=30, check=False)
    return (proc.stderr or '') + (proc.stdout or '')


def preflight_java(
    java: Path | str,
    *,
    min_major: int = 21,
    probe: Callable[[Path], str] | None = None,
) -> Path:
    """Validate ``java`` is a real launcher of at least ``min_major``; return it, or raise.

    Raises :class:`BootFailure` when the path is missing / not executable, when the probe cannot
    be run, when its output does not parse as a Java version, or when the major version is below
    ``min_major``. ``probe`` is injectable (returns the ``java -version`` banner text) so the
    version gate is testable without a real JVM.
    """
    path = Path(java).expanduser()
    run_probe = probe or default_java_probe
    try:
        output = run_probe(path)
    except FileNotFoundError as exc:
        raise BootFailure(
            f'java launcher not found or not executable: {path!r}. Set MAKE_MAGIC_JAVA to a JRE {min_major}+ launcher.'
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise BootFailure(f'could not run `{path} -version` for the boot preflight: {exc}') from exc

    major = java_major_version(output)
    if major is None:
        raise BootFailure(
            f'could not parse a Java version from `{path} -version` output — refusing to boot '
            f'against an unidentifiable runtime. Output:\n{output.strip()}'
        )
    if major < min_major:
        raise BootFailure(
            f'Java {major} is too old: the sim engine needs Java {min_major}+ (found via {path}). '
            f'Set MAKE_MAGIC_JAVA to a {min_major}+ launcher.'
        )
    return path
