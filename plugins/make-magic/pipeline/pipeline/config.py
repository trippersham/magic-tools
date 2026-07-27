"""Env-driven Airtable identity + a runtime name->id resolver.

WHY THIS EXISTS (PR review): the Airtable base id, table ids, and field ids were
hard-coded (``appw7QPMoqktrgDc1`` / ``tbl…`` / ``fld…``), locking the pipeline to
one Airtable instance. This module makes the identity **configuration**:

    - :class:`Settings` (pydantic-settings) reads the base id and the human-facing
      table NAMES from the environment, with turnkey defaults that match the
      current base so nothing breaks out of the box but every value is overridable.
      NAMES (not ids) are the config surface because names are stable across bases
      whereas ``tbl…``/``fld…`` ids are per-base and cannot be shared between
      instances.
    - :class:`AirtableResolver` turns those NAMES into the per-base ``tbl…``/
      ``fld…`` ids AT RUNTIME via the Airtable meta API (``GET
      /v0/meta/bases/{base}/tables``), cached so it hits the endpoint once per base
      per run. This is what removes the fragile hard-coded ids: a different
      instance just sets ``AIRTABLE_BASE_ID`` (and, if its tables are named
      differently, the ``AIRTABLE_*_TABLE`` overrides) and the ids are discovered.

The resolver is deliberately GET-only in spirit (it only ever reads schema),
mirroring the pull-only ethos of the ingest layer; the meta client it is handed
is expected to issue GET requests exclusively.

Environment variables (prefix ``AIRTABLE_``):
    - ``AIRTABLE_BASE_ID``      -> :attr:`Settings.airtable_base_id`
    - ``AIRTABLE_CARDS_TABLE``  -> :attr:`Settings.cards_table`
    - ``AIRTABLE_DECKS_TABLE``  -> :attr:`Settings.decks_table`
    - ``AIRTABLE_TRADES_TABLE`` -> :attr:`Settings.trades_table`
    - ``AIRTABLE_CHASE_TABLE``  -> :attr:`Settings.chase_table`
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Protocol

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AirtableConfigError(RuntimeError):
    """Raised when a configured table/field NAME cannot be resolved to an id.

    This surfaces a misconfigured instance LOUDLY (e.g. a base whose Cards table
    is named differently and whose ``AIRTABLE_CARDS_TABLE`` override was not set),
    rather than silently pulling nothing.
    """


class Settings(BaseSettings):
    """Env-driven Airtable identity (base id + human-facing table NAMES).

    Every field is overridable via its ``AIRTABLE_``-prefixed env var; the
    defaults match the current turnkey base so an unconfigured checkout still
    works. Table NAMES are the config surface (not ids) because names are stable
    across bases while ``tbl…`` ids are per-base — see the module docstring.
    """

    model_config = SettingsConfigDict(env_prefix='AIRTABLE_', extra='ignore')

    #: The Airtable base id. Env: ``AIRTABLE_BASE_ID``. Default = current base.
    airtable_base_id: str = Field(default='appw7QPMoqktrgDc1', alias='AIRTABLE_BASE_ID')
    #: Human-edited inventory Cards table NAME. Env: ``AIRTABLE_CARDS_TABLE``.
    #: Matches the live base (the table is named "Inventory Cards", not "Cards").
    cards_table: str = 'Inventory Cards'
    #: Decks table NAME. Env: ``AIRTABLE_DECKS_TABLE``.
    decks_table: str = 'Decks'
    #: Trades table NAME. Env: ``AIRTABLE_TRADES_TABLE``.
    trades_table: str = 'Trades'
    #: Chase Cards table NAME. Env: ``AIRTABLE_CHASE_TABLE``.
    chase_table: str = 'Chase Cards'


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton (env read ONCE).

    Cached so the environment is read a single time per run. Tests that need to
    vary the environment call ``get_settings.cache_clear()`` between cases.
    """
    return Settings()


class SupportsMetaTables(Protocol):
    """Narrow port the resolver needs: a GET-only fetch of the base's schema.

    The single method returns the raw Airtable meta payload for a base
    (``GET /v0/meta/bases/{base}/tables``): ``{"tables": [{"id", "name",
    "fields": [{"id", "name"}, ...]}, ...]}``. Keeping the port this small means
    the resolver is trivially unit-testable with an in-memory double and never
    touches the network itself.
    """

    def get_meta_tables(self, base_id: str) -> dict[str, Any]: ...


class AirtableResolver:
    """Resolve table/field NAMES to per-base ``tbl…``/``fld…`` ids at runtime.

    Given a GET-only meta client and a base id, this fetches the base schema ONCE
    (lazily, on first lookup) and answers every subsequent name->id query from the
    cached payload — so a whole run costs a single meta call per base. Unknown
    names raise :class:`AirtableConfigError` with the offending name and base, so a
    misconfigured instance fails clearly instead of silently.
    """

    def __init__(self, meta_client: SupportsMetaTables, *, base_id: str) -> None:
        self._client = meta_client
        self._base_id = base_id
        #: Lazily-populated caches, built once from the single meta fetch.
        self._table_ids: dict[str, str] | None = None
        self._field_ids: dict[str, dict[str, str]] | None = None

    def _ensure_loaded(self) -> None:
        """Fetch + index the base schema exactly once (idempotent)."""
        if self._table_ids is not None:
            return
        payload = self._client.get_meta_tables(self._base_id)
        table_ids: dict[str, str] = {}
        field_ids: dict[str, dict[str, str]] = {}
        for table in payload.get('tables', []):
            name = table.get('name')
            tid = table.get('id')
            if not name or not tid:
                continue
            table_ids[name] = tid
            field_ids[name] = {f['name']: f['id'] for f in table.get('fields', []) if f.get('name') and f.get('id')}
        self._table_ids = table_ids
        self._field_ids = field_ids

    def table_id(self, table_name: str) -> str:
        """Resolve a table NAME to its per-base ``tbl…`` id (or raise clearly)."""
        self._ensure_loaded()
        assert self._table_ids is not None
        try:
            return self._table_ids[table_name]
        except KeyError:
            raise AirtableConfigError(
                f'table {table_name!r} not found in base {self._base_id!r}; '
                f'available tables: {sorted(self._table_ids)}. '
                'Set the matching AIRTABLE_*_TABLE env var for this instance.'
            ) from None

    def field_id(self, table_name: str, field_name: str) -> str:
        """Resolve a field NAME within a table to its ``fld…`` id (or raise)."""
        # table_id() both loads the schema and validates the table name.
        self.table_id(table_name)
        assert self._field_ids is not None
        fields = self._field_ids.get(table_name, {})
        try:
            return fields[field_name]
        except KeyError:
            raise AirtableConfigError(
                f'field {field_name!r} not found in table {table_name!r} '
                f'(base {self._base_id!r}); available fields: {sorted(fields)}.'
            ) from None
