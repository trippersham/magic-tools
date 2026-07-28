"""The collection layer — the `CollectionStore` port + backend resolution.

The single, backend-agnostic data surface the skills bind to (in both local and
Airtable modes). Task 1 ships the port, `app_state`, and the local YAML adapter;
the Airtable adapter + onboarding/copy land in Tasks 2/3.
"""

from __future__ import annotations

from pipeline.collection.store import (
    ENV_BACKEND,
    AppState,
    CardResolver,
    CollectionStore,
    get_store,
    read_app_state,
    resolve_backend,
    write_app_state,
)

__all__ = (
    'ENV_BACKEND',
    'AppState',
    'CardResolver',
    'CollectionStore',
    'get_store',
    'read_app_state',
    'resolve_backend',
    'write_app_state',
)
