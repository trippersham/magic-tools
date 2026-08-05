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

import pytest
from _decks_helpers import CanonicalizingResolver, run_cli

from pipeline import store


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
