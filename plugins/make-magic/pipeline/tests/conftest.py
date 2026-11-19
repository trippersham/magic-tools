"""Shared fixtures for the decks behavior suite.

The offline harness the decks hardening tests share, defined ONCE:

- ``data_dir`` — an isolated tmp data root (``MAKE_MAGIC_DATA_DIR``), forced onto
  the LOCAL backend with no live Airtable creds;
- ``cli`` — the in-process CLI runner (``run_cli``) with the CLI's card resolver
  wired to the REAL ``CanonicalizingResolver`` (the hazard is exercised, not
  stubbed).

Test modules that need a DIFFERENT ``data_dir`` (e.g. no backend pin) define their
own local fixture, which shadows this one — so pre-existing suites are unaffected.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from _decks_helpers import CanonicalizingResolver, run_cli

from pipeline import store

#: Markers whose tests are ALLOWED to reach the real network (they are deselected
#: from the default offline suite by the pyproject addopts). Any test NOT carrying
#: one of these is held to the network-free contract by ``_block_source_network``.
_NETWORKED_MARKERS = frozenset({'live', 'canary', 'integration', 'forge'})


@pytest.fixture(autouse=True)
def _block_source_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the default (offline) suite provably network-free.

    The scoring bridge (``scripts/deck_factsheet._load_tags_via_puller``) and the
    ingest pullers self-refresh from live Scryfall / Commander Spellbook /
    MythicSpoiler by calling the per-source network entry points. Those entry
    points are cursor-gated + FAIL-OPEN, so with network reachable a bare default
    run makes courtesy probes (and, against the real lake, can overwrite
    ``raw/oracle_tags.parquet``). This autouse guard monkeypatches each source's
    network boundary to raise :class:`httpx.ConnectError`, which forces every
    puller down its fail-open branch (reuse cached raw / bundled snapshot) with
    ZERO egress — exactly the path an offline CI runner takes.

    A test that legitimately needs the network carries one of
    :data:`_NETWORKED_MARKERS` (all deselected by default) and is left untouched.
    A test that sets its OWN ``monkeypatch.setattr`` on these functions (e.g.
    ``tests/test_sources_pullers.py``) stacks on top of this guard and wins for
    its body, so canned-payload coverage is unaffected.
    """
    if _NETWORKED_MARKERS & {m.name for m in request.node.iter_markers()}:
        return

    def _blocked(*_args: object, **_kwargs: object):
        raise httpx.ConnectError('offline test suite: source network is blocked (network-free contract)')

    from pipeline.sources import oracle_tags, scryfall_bulk, spellbook, spoilers

    monkeypatch.setattr(oracle_tags, '_fetch_meta', _blocked)
    monkeypatch.setattr(scryfall_bulk, '_fetch_meta', _blocked)
    monkeypatch.setattr(spellbook, '_remote_cursor', _blocked)
    monkeypatch.setattr(spoilers, 'scrape_set', _blocked)


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated tmp data root; force the local backend; no live Airtable creds."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
    return root


@pytest.fixture()
def cli(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Wire the CLI's card resolver to the REAL canonicalizing resolver, then hand
    back the ``run_cli`` runner. The local backend reads ``MAKE_MAGIC_DATA_DIR``."""
    from pipeline.collection import resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, 'default_card_resolver', lambda: CanonicalizingResolver())
    return run_cli
