"""Transforms — the normalized-layer marts (Phase 4a: pilot marts).

Turns ingested oracle-tags into deck-level functional categorization:

    - ``otag_rollup``  — roll each card's leaf tags up the tag DAG to all
      ancestors and land the exploded long-form ``normalized/card_otag``.
    - ``crosswalk``    — the committed slug -> bucket map + ``buckets_for``.
    - ``combo_detect`` — land ``normalized/combo`` + exact named-card matching.
    - ``deck_factsheet`` — the pipeline ``factsheet_for`` that emits a
      ``contracts.FactSheet``-valid dict (multi-label otag_buckets +
      susceptibility + the structured facts).

PURE ADDITION: this package touches only ``transforms/`` and ``tests/`` — it
never modifies ``contracts/``, ``store/``, ``ingest/``, or any skill.
"""

from __future__ import annotations

from pipeline.transforms.crosswalk import BUCKETS, buckets_for
from pipeline.transforms.deck_factsheet import factsheet_for

__all__ = ("BUCKETS", "buckets_for", "factsheet_for")
