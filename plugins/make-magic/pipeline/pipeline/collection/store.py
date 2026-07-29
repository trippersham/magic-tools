"""The `CollectionStore` port + `app_state` + backend resolution.

`CollectionStore` is the narrow, domain-typed Protocol the skills bind to (in
both local and Airtable modes): it returns Pydantic contracts, never generic
table CRUD. `CardResolver` is the hydration seam — a name -> `Card | None`
lookup an adapter uses to fill the base-`Card` enrichment on read.

`app_state` (backend + onboarded + room for watermarks) lives in the existing
`make_magic.duckdb` via `store/io.connect`. `resolve_backend()` applies the full
precedence — explicit `MAKE_MAGIC_BACKEND` env -> an ONBOARDED `app_state.backend`
choice -> creds auto-detect (`AIRTABLE_API_KEY` present -> airtable) -> the safe
`'local'` default. `onboard()` persists the choice so runs never re-prompt, and
`onboarding_status()` tells the CLI whether to nag (creds-present counts as
effectively-onboarded, so an Airtable user is never nagged).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from pipeline import store as _store
from pipeline.collection.errors import CollectionError

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

    from pipeline.contracts import Card, ChaseCard, Deck, OwnedCard, Trade

#: Env var that pins the backend explicitly (highest precedence).
ENV_BACKEND = 'MAKE_MAGIC_BACKEND'

#: Env var whose presence auto-detects Airtable mode (an Airtable PAT).
ENV_AIRTABLE_KEY = 'AIRTABLE_API_KEY'

#: The two backends the collection layer knows how to resolve.
BackendName = Literal['local', 'airtable']
_VALID_BACKENDS: tuple[BackendName, ...] = ('local', 'airtable')

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
    fill base-`Card` enrichment on read, without importing any concrete resolver.
    The package default is `resolver.default_card_resolver` (used by `get_store`
    when none is injected); tests inject a stub, and #5 swaps the default for a
    pipeline-backed (DuckDB over `scryfall_bulk`) resolver.
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


def _ensure_app_state_table(conn: DuckDBPyConnection) -> None:
    """Create the `app_state` table if it does not yet exist (idempotent)."""
    conn.execute(
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
# Backend resolution + onboarding
# --------------------------------------------------------------------------- #


def _creds_present() -> bool:
    """True when an Airtable PAT is exported (the auto-detect signal).

    Backend selection keys on the PRESENCE OF CREDS, never on the base id: the
    turnkey default base id in ``config.Settings`` means a base id is ALWAYS
    resolvable, so it carries no signal about which backend the user wants.
    """
    return bool(os.getenv(ENV_AIRTABLE_KEY))


def resolve_backend() -> str:
    """Resolve the active backend name.

    Full precedence (highest first):
        1. explicit ``MAKE_MAGIC_BACKEND`` env (``local`` | ``airtable``);
        2. a persisted, ONBOARDED ``app_state.backend`` choice;
        3. auto-detect: ``AIRTABLE_API_KEY`` present -> ``airtable`` (do NOT key
           on base id — the turnkey default makes it always present);
        4. the safe default ``local`` (nothing hard-blocks when un-onboarded).
    """
    env = os.getenv(ENV_BACKEND)
    if env:
        # An EXPLICIT env is a deliberate user choice: normalize case/whitespace,
        # then validate — a typo (e.g. 'Airtabel') must fail LOUDLY, never fall
        # through to a silent 'local' that `status` would then mislabel.
        normalized = env.strip().lower()
        if normalized not in _VALID_BACKENDS:
            raise CollectionError(
                f'Invalid {ENV_BACKEND}={env!r}; choose one of {list(_VALID_BACKENDS)}.'
            )
        return normalized
    state = read_app_state()
    if state.onboarded and state.backend:
        # A PERSISTED backend could be corrupt (hand-edited DB / older schema).
        # Normalize + validate; if it's garbage, IGNORE it (fall through to
        # creds/local) rather than trust it — never construct a store we can't.
        persisted = state.backend.strip().lower()
        if persisted in _VALID_BACKENDS:
            return persisted
    if _creds_present():
        return 'airtable'
    return 'local'


class OnboardingStatus(BaseModel):
    """A snapshot of onboarding state used to decide whether to nag.

    ``needs_onboarding`` is the single flag the CLI surfaces: it is True ONLY
    when the user has neither made a persisted choice NOR pinned a backend NOR
    exported creds (creds-present is treated as effectively-onboarded, so an
    Airtable user is never nagged). ``effective_backend`` is whatever
    ``resolve_backend`` would pick right now.
    """

    model_config = ConfigDict(extra='forbid')

    onboarded: bool = Field(description='True once a backend choice is persisted to app_state.')
    effective_backend: str = Field(description="The backend resolve_backend() would pick right now.")
    needs_onboarding: bool = Field(description='True when the CLI should prompt the user to run `onboard`.')


def onboarding_status() -> OnboardingStatus:
    """Compute the current :class:`OnboardingStatus` (no side effects)."""
    state = read_app_state()
    effective = resolve_backend()
    # No nag when: an explicit choice is persisted, a backend env is pinned, or
    # creds are present (an Airtable user auto-detected in — effectively onboarded).
    settled = state.onboarded or bool(os.getenv(ENV_BACKEND)) or _creds_present()
    return OnboardingStatus(
        onboarded=state.onboarded,
        effective_backend=effective,
        needs_onboarding=not settled,
    )


def onboard(backend: BackendName) -> None:
    """Persist the chosen backend + ``onboarded=True`` so runs never re-prompt.

    ``backend`` must be ``'local'`` or ``'airtable'``; anything else raises a
    clear ``ValueError``.
    """
    if backend not in _VALID_BACKENDS:
        raise ValueError(f'Unknown backend {backend!r}; choose one of {list(_VALID_BACKENDS)}.')
    write_app_state(AppState(backend=backend, onboarded=True))


def get_store(resolver: CardResolver | None = None, *, writes_enabled: bool = False) -> CollectionStore:
    """Construct the `CollectionStore` for the resolved backend.

    - ``local`` -> the YAML adapter, which HYDRATES enrichment via ``resolver``.
      When ``resolver`` is omitted, the package default
      (:func:`pipeline.collection.resolver.default_card_resolver`) is used — so no
      caller has to wire one in (the CLI no longer injects one from the script
      edge). Tests pass a stub.
    - ``airtable`` -> the record-CRUD adapter built from env-driven ``Settings``
      (requires ``AIRTABLE_API_KEY``). It ignores this ``resolver`` argument and
      wires its OWN card resolver internally: inventory/chase/trade reads hydrate
      DIRECTLY from the Airtable row (no resolver), while deck reads via
      ``get_deck`` hydrate cards through that internal `CardResolver` and
      ``list_decks`` is name-only. ``writes_enabled`` opts it into mutations.

    The Airtable branch no longer raises ``NotImplementedError`` (Task 2).
    """
    backend = resolve_backend()
    if backend == 'airtable':
        from pipeline.collection.adapters.airtable_collection import AirtableCollectionStore

        token = os.environ.get('AIRTABLE_API_KEY')
        if not token:
            raise CollectionError(
                'Backend is `airtable` but AIRTABLE_API_KEY is not set. Export an Airtable '
                'Personal Access Token, or set MAKE_MAGIC_BACKEND=local for offline mode.'
            )
        return AirtableCollectionStore.from_settings(token, writes_enabled=writes_enabled)

    from pipeline.collection import resolver as resolver_mod
    from pipeline.collection.adapters.local_yaml import LocalYamlStore

    return LocalYamlStore(resolver=resolver or resolver_mod.default_card_resolver())
