"""TDD tests for the CollectionStore port + app_state (Phase 1.2).

Everything is OFFLINE: a tmp data dir (via MAKE_MAGIC_DATA_DIR) backs the
DuckDB `app_state` table; no network. Covers:
    - app_state create/read/update round-trips against the tmp store.
    - resolve_backend() returns 'local' when nothing is configured.
    - CollectionStore / CardResolver Protocols are runtime-checkable and the
      local adapter satisfies CollectionStore (structural conformance).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import config, store
from pipeline.collection import (
    AppState,
    CardResolver,
    CollectionStore,
    get_store,
    read_app_state,
    resolve_backend,
    write_app_state,
)


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at an isolated tmp data root via the env override."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    # Ensure no explicit backend env leaks in from the host.
    monkeypatch.delenv('MAKE_MAGIC_BACKEND', raising=False)
    return root


# --------------------------------------------------------------------------- #
# app_state
# --------------------------------------------------------------------------- #


def test_read_app_state_defaults_when_absent(data_dir: Path) -> None:
    state = read_app_state()
    assert state.backend is None
    assert state.onboarded is False


def test_write_then_read_app_state(data_dir: Path) -> None:
    write_app_state(AppState(backend='local', onboarded=True))
    state = read_app_state()
    assert state.backend == 'local'
    assert state.onboarded is True


def test_write_app_state_is_upsert(data_dir: Path) -> None:
    write_app_state(AppState(backend='local', onboarded=False))
    write_app_state(AppState(backend='airtable', onboarded=True))
    state = read_app_state()
    assert state.backend == 'airtable'
    assert state.onboarded is True


def test_app_state_persists_across_connections(data_dir: Path) -> None:
    write_app_state(AppState(backend='local', onboarded=True))
    # A fresh read opens a new connection; the row must survive.
    assert read_app_state().backend == 'local'


# --------------------------------------------------------------------------- #
# resolve_backend
# --------------------------------------------------------------------------- #


def test_resolve_backend_defaults_to_local(data_dir: Path) -> None:
    assert resolve_backend() == 'local'


def test_resolve_backend_honors_persisted_state(data_dir: Path) -> None:
    write_app_state(AppState(backend='local', onboarded=True))
    assert resolve_backend() == 'local'


# --------------------------------------------------------------------------- #
# Protocol conformance
# --------------------------------------------------------------------------- #


def test_local_adapter_satisfies_collection_store(data_dir: Path) -> None:
    from pipeline.collection.adapters.local_yaml import LocalYamlStore

    class _StubResolver:
        def get_card(self, name: str):
            return None

    adapter = LocalYamlStore(resolver=_StubResolver())
    assert isinstance(adapter, CollectionStore)


def test_card_resolver_is_runtime_checkable() -> None:
    class _Stub:
        def get_card(self, name: str):
            return None

    assert isinstance(_Stub(), CardResolver)


# --------------------------------------------------------------------------- #
# get_store factory — Airtable branch (Task 2: no longer NotImplementedError)
# --------------------------------------------------------------------------- #


class _NullResolver:
    def get_card(self, name: str):
        return None


def test_get_store_airtable_constructs_adapter_no_notimplemented(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_store('airtable') builds the record adapter (Task 2 wiring)."""
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'airtable')
    monkeypatch.setenv('AIRTABLE_API_KEY', 'fake-token')
    config.get_settings.cache_clear()
    from pipeline.collection.adapters.airtable_collection import AirtableCollectionStore

    adapter = get_store(_NullResolver())
    assert isinstance(adapter, AirtableCollectionStore)
    assert adapter.backend_name == 'airtable'
    # structural conformance to the port.
    assert isinstance(adapter, CollectionStore)


def test_get_store_airtable_requires_api_key(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'airtable')
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
    config.get_settings.cache_clear()
    with pytest.raises(RuntimeError, match='AIRTABLE_API_KEY'):
        get_store(_NullResolver())


def test_get_store_local_uses_resolver(data_dir: Path) -> None:
    from pipeline.collection.adapters.local_yaml import LocalYamlStore

    adapter = get_store(_NullResolver())
    assert isinstance(adapter, LocalYamlStore)
