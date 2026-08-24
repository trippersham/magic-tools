"""Version-coherence guards — enforce the release process in CI.

The plugin version and the vendored-install pins are each declared in MORE THAN ONE
file. A bump that updates one place but not another is the exact drift that ships a
broken install (wrong plugin version in the marketplace, an XMage dist tag that no
longer matches the runtime, install-mode fetching an unverifiable jar). These tests run
in the ordinary CI suite (no network, no ``live``/``forge`` markers), so such a drift is
a RED BUILD, not a silent field failure. See CONTRIBUTING.md → "Cutting a release".
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from pipeline.sim import forge_runtime as fr
from pipeline.sim import xmage_runtime as xr

# tests/ -> pipeline/ -> make-magic/ -> plugins/ -> <repo root>
_REPO = Path(__file__).resolve().parents[4]
_PIPELINE = Path(__file__).resolve().parents[1]
_PLUGIN_JSON = _REPO / 'plugins' / 'make-magic' / '.claude-plugin' / 'plugin.json'
_PYPROJECT = _PIPELINE / 'pyproject.toml'
_XMAGE_POM = _PIPELINE / 'pipeline' / 'sim' / 'java' / 'xmage-dist' / 'pom.xml'
_RELEASE_WF = _REPO / '.github' / 'workflows' / 'xmage-dist-release.yml'


def _plugin_version() -> str:
    return json.loads(_PLUGIN_JSON.read_text(encoding='utf-8'))['version']


def _pyproject_version() -> str:
    return tomllib.loads(_PYPROJECT.read_text(encoding='utf-8'))['project']['version']


# --------------------------------------------------------------------------- #
# Plugin / marketplace version — one bump must touch BOTH declaration sites.
# --------------------------------------------------------------------------- #


def test_plugin_version_matches_pyproject() -> None:
    """The plugin manifest (`.claude-plugin/plugin.json`, what the marketplace serves)
    and the Python package (`pyproject.toml`) declare the SAME version — bump both."""
    assert _plugin_version() == _pyproject_version(), (
        f'plugin.json {_plugin_version()!r} != pyproject.toml {_pyproject_version()!r} — '
        'bump both when releasing (CONTRIBUTING.md → Bumping the plugin version).'
    )


# --------------------------------------------------------------------------- #
# Vendored XMage — the built version is declared in three files; keep coherent.
# --------------------------------------------------------------------------- #


def test_xmage_version_coherent_across_runtime_pom_and_workflow() -> None:
    """`XMAGE_VERSION` (runtime) must match the shade pom's `<xmage.version>` and be the
    version the release workflow's upstream `XMAGE_TAG` builds — so the fetched jar, the
    module set it bundles, and the harness are all the same XMage version."""
    pom = _XMAGE_POM.read_text(encoding='utf-8')
    pom_ver = re.search(r'<xmage\.version>([^<]+)</xmage\.version>', pom)
    assert pom_ver is not None, 'no <xmage.version> in the dist pom'
    assert pom_ver.group(1) == xr.XMAGE_VERSION, (
        f'dist pom <xmage.version>={pom_ver.group(1)!r} != XMAGE_VERSION={xr.XMAGE_VERSION!r}'
    )

    wf = _RELEASE_WF.read_text(encoding='utf-8')
    tag = re.search(r"XMAGE_TAG:\s*'([^']+)'", wf)
    assert tag is not None, 'no XMAGE_TAG in the release workflow'
    # e.g. XMAGE_VERSION '1.4.60' must appear in the upstream build tag 'xmage_1.4.60V3'.
    assert xr.XMAGE_VERSION in tag.group(1), (
        f'workflow XMAGE_TAG={tag.group(1)!r} does not build XMAGE_VERSION={xr.XMAGE_VERSION!r}'
    )


def test_xmage_dist_sha_is_pinned() -> None:
    """A merge must not ship install-mode with the fail-closed gate re-armed: after a
    release is cut, `XMAGE_DIST_SHA256` must be pinned (None is a transient release-cut
    state only). Without the pin a fresh box cannot fetch the jar at all."""
    assert xr.XMAGE_DIST_SHA256 is not None, (
        'XMAGE_DIST_SHA256 is None — pin it from the published release .sha256 asset '
        'before merging (CONTRIBUTING.md → Bumping the XMage dist).'
    )
    assert re.fullmatch(r'[0-9a-f]{64}', xr.XMAGE_DIST_SHA256), 'XMAGE_DIST_SHA256 is not a sha256 hex digest'


# --------------------------------------------------------------------------- #
# Vendored Forge — version drives the URL by construction; the sha is independent.
# --------------------------------------------------------------------------- #


def test_forge_url_and_sha_pin_are_coherent() -> None:
    """The Forge tarball URL must embed `FORGE_VERSION` (guards a hand-edited URL drift),
    and the tarball sha must be a pinned digest (bumping Forge means bumping BOTH the
    version and the sha from the new release)."""
    assert fr.FORGE_VERSION in fr.FORGE_TARBALL_URL, (
        f'FORGE_TARBALL_URL does not embed FORGE_VERSION={fr.FORGE_VERSION!r} — re-pin the URL.'
    )
    assert re.fullmatch(r'[0-9a-f]{64}', fr.FORGE_TARBALL_SHA256), 'FORGE_TARBALL_SHA256 is not a sha256 hex digest'
