"""Strategy engine — the consolidated, validated strategy-naming corrections.

A standalone, additive subpackage that turns a resolved decklist into a
deterministic strategy record: a FACTS graph (every attribute present + weighted
count, every archetype strategy + member cards, convergent engines, support /
degree quantities, answered-by + core-combo facts) and a thin derived NAMING
layer (density-rank label, runtime typal tribe, confidence, win-combo override).

This module consolidates the validated T1→T5.5 prototype chain and folds in the
two carried fixes:

* **F4** — the win-combo override's dense-primary guard (:mod:`.naming`), so a
  soft mana-sink promotion never displaces a dense proactive plan (Ozai → burn;
  Kess/Urza/K'rrik still → combo).
* **C3** — the facts-sidecar tribe nits (:mod:`.tribes`): the reported tribe is
  the dominant tribe where it diverges from the raw vote winner, and
  pseudo-subtypes (``Clue`` / ``Equipment`` …) are filtered out of the vote.

It does NOT import ``crosswalk.buckets_for``: :func:`~.assets.StrategyAssets.archetype_of_attr`
plus the committed v4 vocab are a PARALLEL reimplementation of bucket routing —
acknowledged duplication the Phase-4 migration will reconcile, not a reuse of
``buckets_for``. The only crosswalk reuse is a read-only import of
``crosswalk.BUCKET_ROOTS['protection']`` (:mod:`.degree`), so the protection
degree-axis can't drift from the canonical root. crosswalk is never edited; the
CRISPI / Airtable / factsheet consumers are a separate migration.

Public API::

    from pipeline.transforms.strategy import resolve, StrategyConfig, load_assets
    rec = resolve(deck, combos)            # full record (facts + label + confidence)
    ctx = compute_facts(deck, combos)      # facts layer only
    named = name_deck(ctx)                 # naming layer only
"""

from __future__ import annotations

from pipeline.transforms.strategy.assets import StrategyAssets, load_assets
from pipeline.transforms.strategy.config import DEFAULT_CONFIG, StrategyConfig
from pipeline.transforms.strategy.facts import FactsContext, compute_facts
from pipeline.transforms.strategy.naming import name_deck
from pipeline.transforms.strategy.resolve import resolve

__all__ = (
    'DEFAULT_CONFIG',
    'FactsContext',
    'StrategyAssets',
    'StrategyConfig',
    'compute_facts',
    'load_assets',
    'name_deck',
    'resolve',
)
