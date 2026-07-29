"""OFFLINE tests for env-driven Airtable identity + the name->id meta resolver.

No network: the meta client is a spy that returns a canned meta payload and
counts how many times it is called (to prove per-base caching). These tests
prove the PR-review requirement — that the Airtable base/table/field identity is
env-var driven and resolved by NAME at runtime, so the pipeline is not locked to
one Airtable instance.
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline import config


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Each test reads env fresh (the settings singleton is lru_cached)."""
    config.get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Settings: env-driven base id + table NAMES (overridable), turnkey defaults.
# --------------------------------------------------------------------------- #


def test_base_id_defaults_to_current_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('AIRTABLE_BASE_ID', raising=False)
    assert config.get_settings().airtable_base_id == 'appw7QPMoqktrgDc1'


def test_airtable_mode_resolves_default_base_id_with_only_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 3 guard: the turnkey default base id must NOT be dropped.

    The user's ``.env`` carries ONLY ``AIRTABLE_API_KEY`` (no ``AIRTABLE_BASE_ID``)
    and relies on the hardcoded default as their base. Dropping it would break
    Airtable mode; assert the default still resolves when only the key is set.
    """
    monkeypatch.setenv('AIRTABLE_API_KEY', 'fake-token')
    monkeypatch.delenv('AIRTABLE_BASE_ID', raising=False)
    assert config.get_settings().airtable_base_id == 'appw7QPMoqktrgDc1'


def test_base_id_reads_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('AIRTABLE_BASE_ID', 'appOTHERINSTANCE1')
    assert config.get_settings().airtable_base_id == 'appOTHERINSTANCE1'


def test_table_names_have_turnkey_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ('AIRTABLE_CARDS_TABLE', 'AIRTABLE_DECKS_TABLE', 'AIRTABLE_TRADES_TABLE', 'AIRTABLE_CHASE_TABLE'):
        monkeypatch.delenv(var, raising=False)
    s = config.get_settings()
    assert s.cards_table == 'Inventory Cards'
    assert s.decks_table == 'Decks'
    assert s.trades_table == 'Trades'
    assert s.chase_table == 'Chase Cards'


def test_table_names_are_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('AIRTABLE_CARDS_TABLE', 'Custom Cards')
    monkeypatch.setenv('AIRTABLE_CHASE_TABLE', 'Wanted Cards')
    s = config.get_settings()
    assert s.cards_table == 'Custom Cards'
    assert s.chase_table == 'Wanted Cards'
    # untouched ones keep defaults
    assert s.decks_table == 'Decks'


def test_settings_is_a_cached_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('AIRTABLE_BASE_ID', 'appCACHED')
    first = config.get_settings()
    # A later env change is NOT observed until the cache is cleared (reads once).
    monkeypatch.setenv('AIRTABLE_BASE_ID', 'appCHANGED')
    assert config.get_settings() is first
    assert config.get_settings().airtable_base_id == 'appCACHED'


# --------------------------------------------------------------------------- #
# Resolver: name->id for tables and fields, cached per base, clear errors.
# --------------------------------------------------------------------------- #

_META_PAYLOAD: dict[str, Any] = {
    'tables': [
        {
            'id': 'tblCARDSxxxxxxxxx',
            'name': 'Cards',
            'fields': [
                {'id': 'fldCardName', 'name': 'Card Name'},
                {'id': 'fldSets', 'name': 'Sets'},
            ],
        },
        {
            'id': 'tblCHASExxxxxxxxx',
            'name': 'Chase Cards',
            'fields': [
                {'id': 'fldChaseName', 'name': 'Card Name'},
                {'id': 'fldLastMod', 'name': 'Last Modified'},
            ],
        },
    ]
}


class MetaSpy:
    """A GET-only meta client double: returns the canned payload, counts calls."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.calls = 0
        self._payload = payload if payload is not None else _META_PAYLOAD

    def get_meta_tables(self, base_id: str) -> dict[str, Any]:
        self.calls += 1
        return self._payload


def test_resolver_maps_table_name_to_id() -> None:
    spy = MetaSpy()
    resolver = config.AirtableResolver(spy, base_id='appTEST')
    assert resolver.table_id('Cards') == 'tblCARDSxxxxxxxxx'
    assert resolver.table_id('Chase Cards') == 'tblCHASExxxxxxxxx'


def test_resolver_maps_field_name_to_id_within_a_table() -> None:
    spy = MetaSpy()
    resolver = config.AirtableResolver(spy, base_id='appTEST')
    assert resolver.field_id('Cards', 'Card Name') == 'fldCardName'
    assert resolver.field_id('Chase Cards', 'Last Modified') == 'fldLastMod'


def test_resolver_hits_meta_endpoint_only_once_per_base() -> None:
    spy = MetaSpy()
    resolver = config.AirtableResolver(spy, base_id='appTEST')
    resolver.table_id('Cards')
    resolver.table_id('Chase Cards')
    resolver.field_id('Cards', 'Card Name')
    resolver.field_id('Chase Cards', 'Last Modified')
    assert spy.calls == 1  # cached per base — one meta call for all lookups


def test_resolver_raises_clear_error_on_unknown_table() -> None:
    spy = MetaSpy()
    resolver = config.AirtableResolver(spy, base_id='appTEST')
    with pytest.raises(config.AirtableConfigError, match="table 'No Such Table' not found in base 'appTEST'"):
        resolver.table_id('No Such Table')


def test_resolver_raises_clear_error_on_unknown_field() -> None:
    spy = MetaSpy()
    resolver = config.AirtableResolver(spy, base_id='appTEST')
    with pytest.raises(config.AirtableConfigError, match="field 'Nope' not found in table 'Cards'"):
        resolver.field_id('Cards', 'Nope')
