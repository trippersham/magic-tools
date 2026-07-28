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
    CollectionError,
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
    # Ensure no explicit backend env / creds leak in from the host.
    monkeypatch.delenv('MAKE_MAGIC_BACKEND', raising=False)
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
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


# --- full precedence (Task 3) --------------------------------------------- #


def test_resolve_backend_env_wins_over_everything(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit MAKE_MAGIC_BACKEND beats persisted state AND creds auto-detect."""
    write_app_state(AppState(backend='local', onboarded=True))
    monkeypatch.setenv('AIRTABLE_API_KEY', 'fake-token')
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    assert resolve_backend() == 'local'
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'airtable')
    assert resolve_backend() == 'airtable'


def test_resolve_backend_persisted_state_beats_creds(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An onboarded 'local' choice sticks even when creds are present."""
    write_app_state(AppState(backend='local', onboarded=True))
    monkeypatch.setenv('AIRTABLE_API_KEY', 'fake-token')
    assert resolve_backend() == 'local'


def test_resolve_backend_autodetects_airtable_from_creds(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No env, not onboarded, but AIRTABLE_API_KEY present -> 'airtable'."""
    monkeypatch.setenv('AIRTABLE_API_KEY', 'fake-token')
    assert resolve_backend() == 'airtable'


def test_resolve_backend_autodetect_ignores_unonboarded_state_backend(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend written WITHOUT onboarding is ignored (only onboarded choices stick)."""
    write_app_state(AppState(backend='airtable', onboarded=False))
    # No creds, not onboarded -> safe default local (the stray backend is ignored).
    assert resolve_backend() == 'local'


def test_resolve_backend_defaults_local_without_creds(data_dir: Path) -> None:
    """Nothing explicit, not onboarded, no creds -> safe default 'local'."""
    assert resolve_backend() == 'local'


def test_resolve_backend_normalizes_env_case(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`MAKE_MAGIC_BACKEND=Airtable` (wrong case) normalizes to 'airtable' (Fix 3)."""
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', '  Airtable ')
    assert resolve_backend() == 'airtable'


def test_resolve_backend_invalid_env_raises(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit but invalid env value fails LOUDLY, never silently local (Fix 3)."""
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'garbage')
    with pytest.raises(CollectionError, match='garbage'):
        resolve_backend()


def test_resolve_backend_ignores_corrupt_persisted_backend(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt onboarded app_state.backend is IGNORED -> falls through to local (Fix 3)."""
    write_app_state(AppState(backend='garbage', onboarded=True))
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
    assert resolve_backend() == 'local'


def test_status_label_matches_get_store_branch(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`effective_backend` can never disagree with the branch get_store constructs.

    A wrong-case env normalizes to 'airtable' in BOTH resolve_backend (status
    label) and get_store (which requires creds) — no lie where status says
    airtable while get_store silently builds local (Fix 3).
    """
    from pipeline.collection import onboarding_status

    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'Airtable')
    assert onboarding_status().effective_backend == 'airtable'
    # get_store on the SAME resolution now demands creds (proving it took the
    # airtable branch, matching the label) rather than silently building local.
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
    config.get_settings.cache_clear()
    with pytest.raises(CollectionError, match='AIRTABLE_API_KEY'):
        get_store(_NullResolver())


# --- onboarding ----------------------------------------------------------- #


def test_onboarding_status_defaults(data_dir: Path) -> None:
    from pipeline.collection import onboarding_status

    status = onboarding_status()
    assert status.onboarded is False
    assert status.effective_backend == 'local'
    assert status.needs_onboarding is True


def test_onboarding_status_after_onboard(data_dir: Path) -> None:
    from pipeline.collection import onboard, onboarding_status

    onboard('local')
    status = onboarding_status()
    assert status.onboarded is True
    assert status.effective_backend == 'local'
    assert status.needs_onboarding is False


def test_onboard_persists_choice_and_flag(data_dir: Path) -> None:
    from pipeline.collection import onboard

    onboard('airtable')
    state = read_app_state()
    assert state.backend == 'airtable'
    assert state.onboarded is True
    assert resolve_backend() == 'airtable'


def test_onboarding_status_creds_present_not_nagging(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Creds-present is effectively-onboarded (airtable) — no nag even un-onboarded."""
    from pipeline.collection import onboarding_status

    monkeypatch.setenv('AIRTABLE_API_KEY', 'fake-token')
    status = onboarding_status()
    assert status.needs_onboarding is False
    assert status.effective_backend == 'airtable'


def test_onboard_rejects_unknown_backend(data_dir: Path) -> None:
    from pipeline.collection import onboard

    with pytest.raises(ValueError, match=r'local|airtable'):
        onboard('sqlite')  # type: ignore[arg-type]


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
    # Missing-creds is a user-facing config failure -> CollectionError (which the
    # clean-error CLI wrapper catches).
    with pytest.raises(CollectionError, match='AIRTABLE_API_KEY'):
        get_store(_NullResolver())


def test_get_store_local_uses_resolver(data_dir: Path) -> None:
    from pipeline.collection.adapters.local_yaml import LocalYamlStore

    adapter = get_store(_NullResolver())
    assert isinstance(adapter, LocalYamlStore)
