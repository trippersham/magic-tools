"""Concrete `CollectionStore` adapters (local YAML + Airtable records)."""

from __future__ import annotations

from pipeline.collection.adapters.airtable_collection import (
    AirtableCollectionStore,
    ReadOnlyStoreError,
)
from pipeline.collection.adapters.local_yaml import LocalYamlStore

__all__ = (
    'AirtableCollectionStore',
    'LocalYamlStore',
    'ReadOnlyStoreError',
)
