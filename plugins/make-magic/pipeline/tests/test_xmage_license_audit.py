"""Tests for the shaded-XMage license audit (2.3b Phase B).

Pins the audit's failure modes — an UNMAPPED dep and a GPL-3.0-INCOMPATIBLE dep both
fail, shade-excluded deps are skipped, and a jar image is caught — so a future
dependency drift can't silently ship an incompatible license or card art.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from pipeline.sim import xmage_license_audit as la


def test_real_bundled_set_is_clean() -> None:
    """A representative XMage 1.4.60 resolved dep list audits CLEAN (the shipped set)."""
    coords = [
        'org.mage:mage',
        'org.mage:mage-sets',
        'com.h2database:h2',
        'com.j256.ormlite:ormlite-jdbc',
        'com.google.guava:guava',
        'org.slf4j:slf4j-api',
        'org.jboss.logging:jboss-logging-spi',  # LGPL-2.1 — compatible
        'trove:trove',
        'jboss:jboss-serialization',  # shade-excluded → skipped
    ]
    assert la.audit_dependencies(coords) == []


def test_unmapped_dep_fails() -> None:
    """A bundled dep with no LICENSE_MAP entry fails — forces a review on drift."""
    viol = la.audit_dependencies(['com.example:mystery-lib'])
    assert len(viol) == 1
    assert viol[0].coordinate == 'com.example:mystery-lib'
    assert 'UNMAPPED' in viol[0].reason


def test_gpl_incompatible_dep_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bundled dep mapped to a GPL-3.0-incompatible license (CDDL/EPL) fails hard."""
    monkeypatch.setitem(la.LICENSE_MAP, 'com.sun.bad:cddl-lib', la.DepLicense('CDDL-1.1', False))
    viol = la.audit_dependencies(['com.sun.bad:cddl-lib'])
    assert len(viol) == 1
    assert 'INCOMPATIBLE' in viol[0].reason and 'CDDL-1.1' in viol[0].reason


def test_shade_excluded_deps_are_skipped() -> None:
    """Deps the pom's shade drops (CDDL server jars) are NOT audited even though absent
    from the map — they never enter the jar."""
    excluded = ['com.sun.jersey:jersey-core', 'javax.mail:mail', 'org.apache.shiro:shiro-core', 'junit:junit']
    assert la.audit_dependencies(excluded) == []
    assert all(la._excluded(c) for c in excluded)


def test_h2_ships_under_mpl_arm() -> None:
    """H2's dual EPL/MPL is pinned to the MPL-2.0 (GPL-compatible) arm, not EPL."""
    h2 = la.LICENSE_MAP['com.h2database:h2']
    assert h2.license == 'MPL-2.0' and h2.gpl3_compatible


def test_image_in_jar_detected(tmp_path: Path) -> None:
    """audit_jar_no_images catches card art (the acceptance requires 0 images)."""
    jar = tmp_path / 'x.jar'
    with zipfile.ZipFile(jar, 'w') as zf:
        zf.writestr('mage/cards/Foo.class', b'\xca\xfe\xba\xbe')
        zf.writestr('images/cards/Foo.png', b'PNG')
    imgs = la.audit_jar_no_images(jar)
    assert imgs == ['images/cards/Foo.png']


def test_clean_jar_has_no_images(tmp_path: Path) -> None:
    jar = tmp_path / 'clean.jar'
    with zipfile.ZipFile(jar, 'w') as zf:
        zf.writestr('mage/cards/Foo.class', b'\xca\xfe\xba\xbe')
    assert la.audit_jar_no_images(jar) == []


def test_image_audit_catches_vector_and_extra_raster_formats(tmp_path: Path) -> None:
    """Card art could ship as webp/svg/bmp/tiff/ico, not just png/jpg/gif — the audit
    must catch those too (a narrow check would pass a jar bundling them)."""
    jar = tmp_path / 'art.jar'
    with zipfile.ZipFile(jar, 'w') as zf:
        zf.writestr('mage/cards/Foo.class', b'\xca\xfe\xba\xbe')
        for name in ('a.webp', 'b.svg', 'c.bmp', 'd.tiff', 'e.ico'):
            zf.writestr(f'images/{name}', b'X')
    assert sorted(Path(n).name for n in la.audit_jar_no_images(jar)) == [
        'a.webp',
        'b.svg',
        'c.bmp',
        'd.tiff',
        'e.ico',
    ]


def test_shade_excludes_match_the_pom_artifactset() -> None:
    """Drift guard: the audit's ``_SHADE_EXCLUDES`` (what it treats as NOT bundled) must
    equal the shade plugin's ``<artifactSet><excludes>`` in the pom. If a maintainer
    removes an exclude from the pom (a dep becomes bundled) without updating the audit,
    the audit would skip a now-bundled — possibly incompatible — dep (a false clean)."""
    import re

    pom = Path(la.__file__).parent / 'java' / 'xmage-dist' / 'pom.xml'
    text = pom.read_text(encoding='utf-8')
    artifact_set = re.search(r'<artifactSet>(.*?)</artifactSet>', text, re.DOTALL)
    assert artifact_set is not None, 'pom has no <artifactSet> block'
    pom_excludes = set(re.findall(r'<exclude>([^<]+)</exclude>', artifact_set.group(1)))
    assert pom_excludes == set(la._SHADE_EXCLUDES), (
        f'shade-exclude drift: pom-only={pom_excludes - set(la._SHADE_EXCLUDES)}, '
        f'audit-only={set(la._SHADE_EXCLUDES) - pom_excludes}'
    )


def test_parse_maven_dep_list() -> None:
    """Parses the `mvn dependency:list` line format to groupId:artifactId."""
    text = (
        'The following files have been resolved:\n'
        '   org.slf4j:slf4j-api:jar:1.7.36:runtime\n'
        '   com.h2database:h2:jar:1.4.197:runtime\n'
        '   org.mage:mage:jar:1.4.60:compile\n'
        '   (some non-dep line)\n'
    )
    assert la.parse_maven_dep_list(text) == ['com.h2database:h2', 'org.mage:mage', 'org.slf4j:slf4j-api']
