"""Sidecar role registry for curated within-card EventEffect attributes (P1).

The 6 curated edge attrs registered in :data:`.vocab.SYNTH_ALWAYS_ATTRIBUTES`
(``draw-to-drain``, ``draw-to-gain``, ``gain-to-drain``, ``drain-to-gain``,
``gain-to-draw``, ``drain-to-draw``) are bare ``(name, query)`` tuples with no
metadata slot for event roles. This module is the **sidecar** that attaches
``trigger_event`` / ``produced_event`` roles and ``source='curated'`` provenance
WITHOUT disturbing the held tuples in ``vocab.py``.

Why a sidecar (not editing the tuples)
--------------------------------------
The curated base must stay byte-identical across P0-P2 (the parity oracle gate).
The held ``SYNTH_ALWAYS_ATTRIBUTES`` are ``list[tuple[str, str]]`` — plain
``(name, query)`` pairs that the synth evaluator compiles as-is. Rather than
change that data shape (which would ripple into ``compile_synth``, ``facts.py``,
and the parity fixture), this module **maps attribute names to their roles** in a
separate registry, read only by the P2 relay-node derivation that injects
within-card edges into the event graph.

Event alignment
---------------
Each trigger/produced role is an event slug from ``data/event_catalog_v4.json``.
The 6 curated edges use exactly 3 events:

    ``draw-a-card``     — CR 120: drawing cards
    ``lifegain``        — CR 119.3: gaining life
    ``life-loss-drain`` — CR 119.3: opponent losing life / drain

All 3 exist in the catalog. No new events are needed.

Types reused
------------
- :class:`.machine_vocab.Provenance` — shared provenance dataclass.
- :class:`EventEffect` — a frozen dataclass defined here that composes the above.
  (Renamed from ``EdgeRole`` in Phase 2A — the design-3 ontology name.)

P2 reads this registry via :func:`get_role` to decide which event a card
consumes (trigger) and produces (produced_event) for the relay-node injection.
"""

from __future__ import annotations

from dataclasses import dataclass

from pipeline.transforms.strategy.machine_vocab import Provenance

# ---------------------------------------------------------------------------
# EventEffect: the guarded directed edge (trigger_event → produced_event)
#
# Renamed from EdgeRole in Phase 2A (design-3 §3.2).
# Guard scaffolding fields are ADDITIVE and DEFAULTED — no evaluation logic
# here (that lands in Phase 2B).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventEffect:
    """A guarded directed edge in the event multigraph (design-3 §3.2).

    ``trigger_event → produced_event`` with provenance and guard scaffolding.
    Renamed from ``EdgeRole`` in Phase 2A; ``effect_event`` → ``produced_event``.

    Parameters
    ----------
    trigger_event
        The event this edge fires on (e.g. ``'draw-a-card'``).
        Must be a slug in ``data/event_catalog_v4.json``.
    produced_event
        The event this edge produces (e.g. ``'life-loss-drain'``).
        Must be a slug in ``data/event_catalog_v4.json``.
        Renamed from ``effect_event`` in Phase 2A.
    provenance
        Origin metadata. For curated attrs: ``source='curated'``.

    Guard scaffolding (Phase 2A — INERT defaults, evaluation in 2B)
    ---------------------------------------------------------------
    provider
        ``'card'`` | ``'CardAttribute'`` | ``'rule'``. Default ``'card'``.
    guard_characteristic_ref
        CardAttribute slug the guard references (nullable).
    guard_trigger_scope
        ``'self'`` | ``'opponent'`` | ``'any'``. Default ``'any'``.
    guard_identity
        ``'any'`` | ``'another'`` | ``'self'``. Default ``'any'``.
    guard_event_reachable_ref
        Another Event that must be in the deck's reached set (nullable).
    guard_unresolved
        Deferred/warn bucket for unbounded counting / sim-only. Default ``False``.
    magnitude
        Local contribution: ``'infinite'`` | ``'near-infinite'`` | ``'scaled'``
        | ``'finite'`` | ``None``. Default ``None``.
    ability_key
        Per trigger-ability identifier (nullable). Default ``None``.
    """

    trigger_event: str
    produced_event: str
    provenance: Provenance

    # --- Guard scaffolding (additive, defaulted, inert) --------------------
    provider: str = 'card'
    guard_characteristic_ref: str | None = None
    guard_trigger_scope: str = 'any'
    guard_identity: str = 'any'
    guard_event_reachable_ref: str | None = None
    guard_unresolved: bool = False
    magnitude: str | None = None
    ability_key: str | None = None


# Backward-compat alias so downstream code that still says EdgeRole works
# during the transition. Tests should import EventEffect directly.
EdgeRole = EventEffect


# ---------------------------------------------------------------------------
# The singleton curated provenance (shared by all 6 entries)
# ---------------------------------------------------------------------------

_CURATED_PROVENANCE = Provenance(source='curated')


# ---------------------------------------------------------------------------
# The registry: attribute name -> EventEffect
# ---------------------------------------------------------------------------

#: Curated edge-role registry. Keys are the 6 edge-category attribute names
#: from ``SYNTH_ALWAYS_ATTRIBUTES``; values carry the event-catalog-aligned
#: trigger/produced roles and curated provenance. P2's relay-node derivation
#: reads this to inject cards as consumer(trigger) + producer(produced_event)
#: into the event graph.
CURATED_EDGE_ROLES: dict[str, EventEffect] = {
    'draw-to-drain': EventEffect(
        trigger_event='draw-a-card',
        produced_event='life-loss-drain',
        provenance=_CURATED_PROVENANCE,
    ),
    'draw-to-gain': EventEffect(
        trigger_event='draw-a-card',
        produced_event='lifegain',
        provenance=_CURATED_PROVENANCE,
    ),
    'gain-to-drain': EventEffect(
        trigger_event='lifegain',
        produced_event='life-loss-drain',
        provenance=_CURATED_PROVENANCE,
    ),
    'drain-to-gain': EventEffect(
        trigger_event='life-loss-drain',
        produced_event='lifegain',
        provenance=_CURATED_PROVENANCE,
    ),
    'gain-to-draw': EventEffect(
        trigger_event='lifegain',
        produced_event='draw-a-card',
        provenance=_CURATED_PROVENANCE,
    ),
    'drain-to-draw': EventEffect(
        trigger_event='life-loss-drain',
        produced_event='draw-a-card',
        provenance=_CURATED_PROVENANCE,
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_role(attr_name: str) -> EventEffect | None:
    """Look up the edge role for an attribute name.

    Returns ``None`` for non-edge attrs (``morph``, ``rad-counter``, any
    identity/archetype attribute).  P2 calls this on every synth attr to decide
    whether to inject a relay node.
    """
    return CURATED_EDGE_ROLES.get(attr_name)


def all_edge_attr_names() -> frozenset[str]:
    """The frozenset of all curated edge-category attribute names.

    Useful for the incompleteness detector (P3) and any assertion that wants
    to confirm the registry is in sync with ``SYNTH_ALWAYS_ATTRIBUTES``.
    """
    return frozenset(CURATED_EDGE_ROLES.keys())
