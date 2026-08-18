"""License audit for the shaded XMage distributable (2.3b).

The repo is GPL-3.0; the shaded jar (:mod:`pipeline.sim.java.xmage-dist`) bundles
XMage 1.4.60 + its transitive deps, so EVERY bundled dep must be GPL-3.0-COMPATIBLE.
This module is the auditable source of truth: an explicit ``LICENSE_MAP`` of each
pinned coordinate → its license + a GPL-3.0-compatibility verdict, plus the checks.

Two gates (run in CI on the built jar + its resolved dep list, and locally):
  * :func:`audit_dependencies` — every BUNDLED dep (resolved minus the pom's shade
    excludes) must be in the map AND GPL-3.0-compatible. An UNKNOWN dep fails loudly
    (forces a human license review on any dependency drift), and a
    GPL-INCOMPATIBLE one (CDDL, EPL-only, proprietary) fails hard.
  * :func:`audit_jar_no_images` — the acceptance's "no card art": 0 image files.

Design note — pinned, not scanned: XMage 1.4.60 is a FIXED release, so a curated map
is more robust + reviewable than a license SCANNER (which mis-detects dual/again
licenses like H2's EPL/MPL). H2 is the one dep that matters: it is dual EPL-1.0 /
MPL-2.0, and we distribute under the **MPL-2.0** arm (GPL-3.0-compatible, unlike the
EPL arm) — encoded explicitly below.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

#: Shade ``artifactSet`` excludes from xmage-dist/pom.xml — deps RESOLVED by Maven but
#: NOT bundled (dropped by the shade), so the audit must ignore them. Kept in sync with
#: the pom; a ``*`` suffix means "any artifact under this groupId".
_SHADE_EXCLUDES: tuple[str, ...] = (
    'junit:junit',
    'org.junit.jupiter:*',
    'org.junit.platform:*',
    'org.junit.vintage:*',
    'org.hamcrest:*',
    'org.opentest4j:*',
    'org.apiguardian:*',
    'org.assertj:*',
    'com.sun.jersey:*',
    'com.sun.jersey.contribs:*',
    'javax.ws.rs:*',
    'javax.mail:*',
    'javax.activation:*',
    'com.sun.activation:*',
    'javax.xml.bind:*',
    'com.sun.xml.bind:*',
    'org.glassfish.jaxb:*',
    'org.jboss.remoting:*',
    'jboss:*',
    'org.jboss:*',
    'org.apache.shiro:*',
    'org.bouncycastle:*',
)

#: License names considered GPL-3.0-INCOMPATIBLE — a bundled dep under any of these
#: FAILS the audit. CDDL and EPL(-only) are the FSF-recognized incompatibilities that
#: XMage's server deps carry (already shade-excluded); listed so a future drift that
#: reintroduces one fails loudly rather than silently shipping.
_GPL3_INCOMPATIBLE = frozenset({'CDDL-1.0', 'CDDL-1.1', 'EPL-1.0', 'EPL-2.0', 'proprietary'})


@dataclass(frozen=True)
class DepLicense:
    """A bundled dependency's license + its GPL-3.0-compatibility verdict."""

    license: str
    gpl3_compatible: bool
    note: str = ''


#: ``groupId:artifactId`` -> license. THE auditable source of truth. Every dep the
#: shaded jar bundles must appear here; adding a dep to the distributable REQUIRES a
#: reviewed entry (the audit fails on any unmapped bundled dep). Verified against
#: XMage 1.4.60's resolved runtime tree.
LICENSE_MAP: dict[str, DepLicense] = {
    # XMage itself — MIT (the whole org.mage:* reactor).
    'org.mage': DepLicense('MIT', True),
    # The one license-sensitive REQUIRED dep: H2 is dual EPL-1.0 / MPL-2.0; we ship
    # under the MPL-2.0 arm, which is GPL-3.0-compatible (the EPL arm is NOT).
    'com.h2database:h2': DepLicense('MPL-2.0', True, 'dual EPL-1.0/MPL-2.0; distributed under the MPL-2.0 arm'),
    # Apache-2.0 (GPL-3.0-compatible).
    'ch.qos.reload4j:reload4j': DepLicense('Apache-2.0', True),
    'com.google.code.gson:gson': DepLicense('Apache-2.0', True),
    'com.google.errorprone:error_prone_annotations': DepLicense('Apache-2.0', True),
    'com.google.guava:guava': DepLicense('Apache-2.0', True),
    'com.google.guava:failureaccess': DepLicense('Apache-2.0', True),
    'com.google.guava:listenablefuture': DepLicense('Apache-2.0', True),
    'com.google.j2objc:j2objc-annotations': DepLicense('Apache-2.0', True),
    'org.apache.commons:commons-lang3': DepLicense('Apache-2.0', True),
    'org.jspecify:jspecify': DepLicense('Apache-2.0', True),
    'org.ocpsoft.prettytime:prettytime': DepLicense('Apache-2.0', True),
    'com.googlecode.jspf:jspf-core': DepLicense('BSD-2-Clause', True),
    'com.google.protobuf:protobuf-java': DepLicense('BSD-3-Clause', True),
    # MIT.
    'org.jsoup:jsoup': DepLicense('MIT', True),
    'org.slf4j:slf4j-api': DepLicense('MIT', True),
    'org.slf4j:slf4j-reload4j': DepLicense('MIT', True),
    # ISC (permissive, GPL-compatible).
    'com.j256.ormlite:ormlite-core': DepLicense('ISC', True),
    'com.j256.ormlite:ormlite-jdbc': DepLicense('ISC', True),
    # LGPL-2.1 — GPL-3.0-compatible (combinable under GPLv3).
    'org.jboss.logging:jboss-logging-spi': DepLicense('LGPL-2.1', True),
    'trove:trove': DepLicense('LGPL-2.1', True),
    # Public domain (Doug Lea's util.concurrent).
    'concurrent:concurrent': DepLicense('Public-Domain', True),
}


@dataclass(frozen=True)
class Violation:
    """A dependency that fails the audit — unmapped, or GPL-3.0-incompatible."""

    coordinate: str
    reason: str


def _excluded(coord: str) -> bool:
    """True if ``groupId:artifactId`` matches a pom shade-exclude (so it is NOT bundled)."""
    group = coord.split(':', 1)[0]
    return any(pattern == coord or (pattern.endswith(':*') and pattern[:-2] == group) for pattern in _SHADE_EXCLUDES)


def _lookup(coord: str) -> DepLicense | None:
    """License for a ``groupId:artifactId`` — exact, then a ``groupId``-wide entry
    (``org.mage`` covers every reactor module)."""
    if coord in LICENSE_MAP:
        return LICENSE_MAP[coord]
    return LICENSE_MAP.get(coord.split(':', 1)[0])


def audit_dependencies(coordinates: list[str]) -> list[Violation]:
    """Audit resolved dep coordinates (``groupId:artifactId`` each); return violations.

    Shade-excluded deps are skipped (not bundled). Each BUNDLED dep must be in
    :data:`LICENSE_MAP` (else UNMAPPED — forces a review on drift) and
    ``gpl3_compatible`` / not in :data:`_GPL3_INCOMPATIBLE` (else a hard fail). An
    empty return means the bundled set is clean.
    """
    violations: list[Violation] = []
    for coord in coordinates:
        if _excluded(coord):
            continue
        lic = _lookup(coord)
        if lic is None:
            violations.append(Violation(coord, 'UNMAPPED — add a reviewed LICENSE_MAP entry before bundling'))
        elif not lic.gpl3_compatible or lic.license in _GPL3_INCOMPATIBLE:
            violations.append(Violation(coord, f'GPL-3.0-INCOMPATIBLE license: {lic.license}'))
    return violations


def audit_jar_no_images(jar_path: Path) -> list[str]:
    """Return image entries in the jar (png/jpg/jpeg/gif) — the acceptance requires 0
    (no card art shipped). Empty return == clean."""
    with zipfile.ZipFile(jar_path) as zf:
        return [n for n in zf.namelist() if n.lower().endswith(('.png', '.jpg', '.jpeg', '.gif'))]


def parse_maven_dep_list(text: str) -> list[str]:
    """Extract ``groupId:artifactId`` coordinates from ``mvn dependency:list`` output
    (lines like ``   org.slf4j:slf4j-api:jar:1.7.36:runtime``)."""
    coords: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        parts = line.split(':')
        if len(parts) >= 4 and parts[2] in ('jar', 'bundle'):
            coords.append(f'{parts[0]}:{parts[1]}')
    return sorted(set(coords))


def render_third_party() -> str:
    """Render the THIRD-PARTY attribution from :data:`LICENSE_MAP` (the source of truth).

    Grouped by license so a reviewer sees the compatibility story at a glance; H2's
    dual-license note is surfaced. This is what ships as NOTICE alongside the jar.
    """
    by_license: dict[str, list[tuple[str, str]]] = {}
    for coord, lic in sorted(LICENSE_MAP.items()):
        by_license.setdefault(lic.license, []).append((coord, lic.note))
    lines = [
        'THIRD-PARTY LICENSES — make-magic-xmage-dist',
        '',
        'The shaded XMage sim distributable bundles XMage 1.4.60 + its runtime deps.',
        'The repository is GPL-3.0; every bundled dependency below is GPL-3.0-COMPATIBLE.',
        'Server/client/test deps that would carry incompatible licenses (Jersey/JAX-RS/',
        'JAXB/javax.mail = CDDL, JUnit = EPL) are NOT bundled — the dist depends on the',
        'XMage ENGINE modules only, not mage-server.',
        '',
    ]
    for license_name in sorted(by_license):
        lines.append(f'== {license_name} ==')
        for coord, note in by_license[license_name]:
            lines.append(f'  - {coord}' + (f'   ({note})' if note else ''))
        lines.append('')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m pipeline.sim.xmage_license_audit <deps.txt> <jar>``.

    Audits the ``mvn dependency:list`` output + the built jar; prints violations and
    returns non-zero on any (CI gate). ``--third-party`` instead prints the NOTICE.
    """
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == '--third-party':
        print(render_third_party())
        return 0
    if len(args) != 2:
        print('usage: xmage_license_audit <mvn-dependency-list.txt> <shaded.jar>', file=sys.stderr)
        print('       xmage_license_audit --third-party', file=sys.stderr)
        return 2
    deps_file, jar = Path(args[0]), Path(args[1])
    coords = parse_maven_dep_list(deps_file.read_text())
    violations = audit_dependencies(coords)
    images = audit_jar_no_images(jar)
    bundled = [c for c in coords if not _excluded(c)]
    print(f'audited {len(bundled)} bundled deps ({len(coords) - len(bundled)} shade-excluded); jar={jar.name}')
    for v in violations:
        print(f'  LICENSE VIOLATION: {v.coordinate} — {v.reason}', file=sys.stderr)
    for img in images:
        print(f'  IMAGE IN JAR (no art allowed): {img}', file=sys.stderr)
    if violations or images:
        print(f'FAIL: {len(violations)} license violation(s), {len(images)} image(s).', file=sys.stderr)
        return 1
    print('PASS: all bundled deps GPL-3.0-compatible; 0 images.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
