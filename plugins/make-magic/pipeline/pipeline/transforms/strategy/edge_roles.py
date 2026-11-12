"""Sidecar role registry for curated within-card edge-category attributes (P1).

The 6 curated edge attrs registered in :data:`.vocab.SYNTH_ALWAYS_ATTRIBUTES`
(``draw-to-drain``, ``draw-to-gain``, ``gain-to-drain``, ``drain-to-gain``,
``gain-to-draw``, ``drain-to-draw``) are bare ``(name, query)`` tuples with no
metadata slot for event roles. This module is the **sidecar** that attaches
``trigger_event`` / ``effect_event`` roles and ``source='curated'`` provenance
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
Each trigger/effect role is an event slug from ``data/event_catalog_v4.json``.
The 6 curated edges use exactly 3 events:

    ``draw-a-card``     — CR 120: drawing cards
    ``lifegain``        — CR 119.3: gaining life
    ``life-loss-drain`` — CR 119.3: opponent losing life / drain

All 3 exist in the catalog. No new events are needed.

Types reused
------------
- :class:`.machine_vocab.Provenance` — shared provenance dataclass.
- :class:`EdgeRole` — a frozen dataclass defined here that composes the above.

P2 reads this registry via :func:`get_role` to decide which event a card
consumes (trigger) and produces (effect) for the relay-node injection.
"""

from __future__ import annotations

from dataclasses import dataclass

from pipeline.transforms.strategy.machine_vocab import Provenance


# ---------------------------------------------------------------------------
# EdgeRole: the (trigger_event, effect_event, provenance) record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EdgeRole:
    """Within-card edge role for a single curated edge-category attribute.

    Parameters
    ----------
    trigger_event
        The event this card's trigger ability CONSUMES (e.g. ``'draw-a-card'``).
        Must be a slug in ``data/event_catalog_v4.json``.
    effect_event
        The event this card's effect ability PRODUCES (e.g. ``'life-loss-drain'``).
        Must be a slug in ``data/event_catalog_v4.json``.
    provenance
        Origin metadata. For curated attrs: ``source='curated'``.
    """

    trigger_event: str
    effect_event: str
    provenance: Provenance


# ---------------------------------------------------------------------------
# The singleton curated provenance (shared by all 6 entries)
# ---------------------------------------------------------------------------

_CURATED_PROVENANCE = Provenance(source='curated')


# ---------------------------------------------------------------------------
# The registry: attribute name -> EdgeRole
# ---------------------------------------------------------------------------

#: Curated edge-role registry. Keys are the 6 edge-category attribute names
#: from ``SYNTH_ALWAYS_ATTRIBUTES``; values carry the event-catalog-aligned
#: trigger/effect roles and curated provenance. P2's relay-node derivation
#: reads this to inject cards as consumer(trigger) + producer(effect) into the
#: event graph.
CURATED_EDGE_ROLES: dict[str, EdgeRole] = {
    'draw-to-drain': EdgeRole(
        trigger_event='draw-a-card',
        effect_event='life-loss-drain',
        provenance=_CURATED_PROVENANCE,
    ),
    'draw-to-gain': EdgeRole(
        trigger_event='draw-a-card',
        effect_event='lifegain',
        provenance=_CURATED_PROVENANCE,
    ),
    'gain-to-drain': EdgeRole(
        trigger_event='lifegain',
        effect_event='life-loss-drain',
        provenance=_CURATED_PROVENANCE,
    ),
    'drain-to-gain': EdgeRole(
        trigger_event='life-loss-drain',
        effect_event='lifegain',
        provenance=_CURATED_PROVENANCE,
    ),
    'gain-to-draw': EdgeRole(
        trigger_event='lifegain',
        effect_event='draw-a-card',
        provenance=_CURATED_PROVENANCE,
    ),
    'drain-to-draw': EdgeRole(
        trigger_event='life-loss-drain',
        effect_event='draw-a-card',
        provenance=_CURATED_PROVENANCE,
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_role(attr_name: str) -> EdgeRole | None:
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
