"""The collection layer — the `CollectionStore` port + backend resolution.

The single, backend-agnostic data surface the skills bind to (in both local and
Airtable modes). Task 1 ships the port, `app_state`, and the local YAML adapter;
the Airtable adapter + onboarding/copy land in Tasks 2/3.
"""

from __future__ import annotations

from pipeline.collection.copy import copy_collection
from pipeline.collection.errors import CollectionError
from pipeline.collection.store import (
    ENV_BACKEND,
    AppState,
    BackendName,
    CardResolver,
    CollectionStore,
    OnboardingStatus,
    get_store,
    onboard,
    onboarding_status,
    read_app_state,
    resolve_backend,
    write_app_state,
)

__all__ = (
    'ENV_BACKEND',
    'AppState',
    'BackendName',
    'CardResolver',
    'CollectionError',
    'CollectionStore',
    'OnboardingStatus',
    'copy_collection',
    'get_store',
    'onboard',
    'onboarding_status',
    'read_app_state',
    'resolve_backend',
    'write_app_state',
)
