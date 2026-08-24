"""Upstream-canary tests — network checks that runtime-fetched deps still resolve.

The sim engines fetch two things at runtime that are NOT under our control: the Adoptium
Temurin JRE (resolved via the Adoptium API) and the upstream Forge tarball. When one of
those breaks upstream, a fresh box fails to onboard — and nothing in the offline suite
notices. That is exactly how ``/v3/assets/latest/21/ga`` (a 404) shipped and blocked ALL
fresh Forge installs: no CI ever exercised the live JRE resolver.

These are ``canary``-marked (deselected by the default offline suite) and run on a
schedule by ``.github/workflows/upstream-canary.yml`` — a red canary means an upstream
dep moved, before a user hits it.
"""

from __future__ import annotations

import urllib.request

import pytest

from pipeline.sim import forge_runtime as fr


@pytest.mark.canary
def test_temurin_jre_resolves_live() -> None:
    """The Adoptium JRE resolver still returns a verifiable asset from the live API.

    Exercises the exact endpoint + query params + parse that shipped broken as
    ``/21/ga`` (404). A break here means fresh Forge onboarding is down (the JRE step),
    for BOTH engines' shared JRE provisioning.
    """
    url, sha256 = fr._temurin_asset()
    assert url.startswith('https://') and 'jre' in url.lower(), f'implausible JRE url: {url!r}'
    assert sha256 is not None and len(sha256) == 64, 'Adoptium returned no usable checksum'


@pytest.mark.canary
def test_forge_tarball_reachable_live() -> None:
    """The pinned Forge tarball URL still resolves upstream (2xx after redirects).

    Catches an upstream re-tag / asset deletion. The tarball BYTES are verified against
    ``FORGE_TARBALL_SHA256`` at fetch time (and GitHub release assets are immutable), so
    reachability is the real risk a canary must watch — not byte drift.
    """
    req = urllib.request.Request(fr.FORGE_TARBALL_URL, method='HEAD', headers={'User-Agent': fr._USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        assert 200 <= resp.status < 300, f'Forge tarball not reachable: HTTP {resp.status}'
