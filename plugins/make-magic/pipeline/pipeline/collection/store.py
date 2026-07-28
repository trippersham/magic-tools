"""The `CollectionStore` port + `app_state` + backend resolution.

`CollectionStore` is the narrow, domain-typed Protocol the skills bind to (in
both local and Airtable modes): it returns Pydantic contracts, never generic
table CRUD. `CardResolver` is the hydration seam — a name -> `Card | None`
lookup an adapter uses to fill the base-`Card` enrichment on read.

`app_state` (backend + onboarded + room for watermarks) lives in the existing
`make_magic.duckdb` via `store/io.connect`. Backend resolution here is the
Task-1 skeleton: full precedence (env -> app_state -> creds -> onboarding) lands
in Task 3; for now `resolve_backend()` honors the persisted `app_state` and
defaults to `'local'` when nothing is configured.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from pipeline import store as _store

if TYPE_CHECKING:
    from pipeline.contracts import Card, ChaseCard, Deck, OwnedCard, Trade

#: Env var that pins the backend explicitly (highest precedence — wired in Task 3).
ENV_BACKEND = 'MAKE_MAGIC_BACKEND'

#: The DuckDB table that persists onboarding / backend / watermark state.
_APP_STATE_TABLE = 'app_state'

#: The single row id — app_state is a singleton row keyed on a constant.
_APP_STATE_KEY = 'singleton'


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #


@runtime_checkable
class CardResolver(Protocol):
    """The hydration seam: resolve a card name to an enriched `Card` (or None).

    Kept narrow on purpose — an adapter takes a `CardResolver` and uses it to
    fill base-`Card` enrichment on read, without importing any concrete resolver
    (the interim Scryfall-cache impl lives at the script edge).
    """

    def get_card(self, name: str) -> Card | None:
        """Return the enriched `Card` for `name`, or None if unresolved."""
        ...


@runtime_checkable
class CollectionStore(Protocol):
    """The domain-typed collection port — returns Pydantic contracts.

    The single data surface the skills use in both modes. Reads come back fully
    hydrated (base-`Card` enrichment joined from the `CardResolver`); writes are
    card-ref-centric verbs (the store persists only the owned/intent/membership
    facts and hydrates the rest on read).
    """

    # --- Decks --------------------------------------------------------------- #
    def get_deck(self, name: str) -> Deck: ...
    def list_decks(self) -> list[Deck]: ...
    def save_deck(self, deck: Deck) -> None: ...
    def set_strategy(self, name: str, text: str) -> None: ...
    def set_assessment(self, name: str, text: str) -> None: ...
    def set_focus_otags(self, name: str, otags: list[str]) -> None: ...

    # --- Inventory ----------------------------------------------------------- #
    def list_inventory(self) -> list[OwnedCard]: ...
    def add_card(
        self,
        ref: str,
        qty: int = 1,
        *,
        condition: list[str] | None = None,
        foil: int = 0,
        sets: list[str] | None = None,
        sources: list[str] | None = None,
    ) -> None: ...
    def set_quantity(self, ref: str, qty: int) -> None: ...
    def remove_card(self, ref: str) -> None: ...

    # --- Chase --------------------------------------------------------------- #
    def list_chase(self) -> list[ChaseCard]: ...
    def add_chase(
        self,
        ref: str,
        *,
        priority: int | None = None,
        for_deck: str | None = None,
        status: str | None = None,
        target_price: float | None = None,
    ) -> str | None:
        """Add/update a chase card. May return a human-readable note (e.g. an
        adapter that cannot persist some fields), or ``None``."""
        ...
    def remove_chase(self, ref: str) -> None: ...

    # --- Trades -------------------------------------------------------------- #
    def list_trades(self) -> list[Trade]: ...
    def log_trade(self, trade: Trade) -> None: ...

    # --- Meta ---------------------------------------------------------------- #
    @property
    def backend_name(self) -> str: ...


# --------------------------------------------------------------------------- #
# app_state
# --------------------------------------------------------------------------- #


class AppState(BaseModel):
    """Persisted collection state: chosen backend + onboarding flag.

    Room is left for watermarks (a future one-shot `copy` cursor); they are added
    as columns without breaking this shape.
    """

    model_config = ConfigDict(extra='forbid')

    backend: str | None = Field(default=None, description="Chosen backend: 'local' | 'airtable' | None (unset).")
    onboarded: bool = Field(default=False, description='True once the user has made a backend choice.')


def _ensure_app_state_table(conn: object) -> None:
    """Create the `app_state` table if it does not yet exist (idempotent)."""
    conn.execute(  # type: ignore[attr-defined]
        f'CREATE TABLE IF NOT EXISTS {_APP_STATE_TABLE} (key VARCHAR PRIMARY KEY, backend VARCHAR, onboarded BOOLEAN)'
    )


def read_app_state() -> AppState:
    """Read the singleton `app_state` row; defaults when the row/table is absent."""
    with _store.connect() as conn:
        _ensure_app_state_table(conn)
        row = conn.execute(
            f'SELECT backend, onboarded FROM {_APP_STATE_TABLE} WHERE key = ?',
            [_APP_STATE_KEY],
        ).fetchone()
    if row is None:
        return AppState()
    backend, onboarded = row
    return AppState(backend=backend, onboarded=bool(onboarded))


def write_app_state(state: AppState) -> None:
    """Upsert the singleton `app_state` row."""
    with _store.connect() as conn:
        _ensure_app_state_table(conn)
        conn.execute(
            f'INSERT INTO {_APP_STATE_TABLE} (key, backend, onboarded) VALUES (?, ?, ?) '
            'ON CONFLICT (key) DO UPDATE SET backend = excluded.backend, onboarded = excluded.onboarded',
            [_APP_STATE_KEY, state.backend, state.onboarded],
        )


# --------------------------------------------------------------------------- #
# Backend resolution (Task-1 skeleton)
# --------------------------------------------------------------------------- #


def resolve_backend() -> str:
    """Resolve the active backend name.

    Task-1 skeleton precedence: explicit `MAKE_MAGIC_BACKEND` env -> persisted
    `app_state.backend` -> `'local'`. The creds auto-detect + onboarding prompt
    branches land in Task 3.
    """
    env = os.getenv(ENV_BACKEND)
    if env:
        return env
    state = read_app_state()
    if state.backend:
        return state.backend
    return 'local'


def get_store(resolver: CardResolver, *, writes_enabled: bool = False) -> CollectionStore:
    """Construct the `CollectionStore` for the resolved backend.

    - ``local`` -> the YAML adapter, which HYDRATES enrichment via ``resolver``.
    - ``airtable`` -> the record-CRUD adapter built from env-driven ``Settings``
      (requires ``AIRTABLE_API_KEY``); it hydrates enrichment DIRECTLY from the
      Airtable row, so it ignores ``resolver`` (the asymmetry documented in the
      design). ``writes_enabled`` opts the Airtable store into mutations.

    The Airtable branch no longer raises ``NotImplementedError`` (Task 2).
    """
    backend = resolve_backend()
    if backend == 'airtable':
        from pipeline.collection.adapters.airtable_collection import AirtableCollectionStore

        token = os.environ.get('AIRTABLE_API_KEY')
        if not token:
            raise RuntimeError(
                'Backend is `airtable` but AIRTABLE_API_KEY is not set. Export an Airtable '
                'Personal Access Token, or set MAKE_MAGIC_BACKEND=local for offline mode.'
            )
        return AirtableCollectionStore.from_settings(token, writes_enabled=writes_enabled)

    from pipeline.collection.adapters.local_yaml import LocalYamlStore

    return LocalYamlStore(resolver=resolver)
