"""Within-card edge relay-node derivation (Phase 2).

For each curated edge-category attribute in the deck's ``nodes`` set, this
module injects the attribute as a **relay node** in the event graph:

- **consumer** of its ``trigger_event`` (the event the card's trigger ability
  fires on)
- **producer** of its ``effect_event`` (the event the card's effect ability
  generates)

This reuses the EXISTING ``produced_by x triggered_by -> event_edges``
construction from :func:`.facts.compute_facts` — no new edge type.  Within-card
edges now let loops compose across cards through the event layer
``_isolate_engines`` already walks.

Separation invariant
--------------------
The relay injection is a **SEPARATE code path** from the default
``compute_facts`` / ``resolve()`` path.  The default path stays byte-identical
(the P0 oracle's engine SHA and resolve SHA are unchanged).  The augmented path
is invoked only by the forthcoming on-demand engine-identification flow.

Example: Queza + Lich's Mastery
-------------------------------
Queza matches two curated edge attrs:

- ``draw-to-drain``: trigger=``draw-a-card``, effect=``life-loss-drain``
- ``draw-to-gain``:  trigger=``draw-a-card``, effect=``lifegain``

Lich's Mastery matches:

- ``gain-to-draw``: trigger=``lifegain``, effect=``draw-a-card``

After relay injection the event graph contains:

- ``draw-a-card``:     producer=``gain-to-draw``,
                       consumers={``draw-to-drain``, ``draw-to-gain``}
- ``lifegain``:        producer=``draw-to-gain``,  consumer=``gain-to-draw``
- ``life-loss-drain``: producer=``draw-to-drain``   (payoff endpoint, no consumers)

The cycle ``gain-to-draw -> draw-to-gain -> gain-to-draw`` (through events
``draw-a-card`` and ``lifegain``) closes — this is the Queza engine.
"""

from __future__ import annotations

from collections import defaultdict

from pipeline.transforms.strategy.assets import StrategyAssets
from pipeline.transforms.strategy.edge_roles import EdgeRole, get_role
from pipeline.transforms.strategy.facts import _isolate_engines
from pipeline.transforms.strategy.machine_vocab import MachineStore


def _get_role_with_store(
    attr_name: str,
    store: MachineStore | None,
) -> EdgeRole | None:
    """Look up an edge role from curated roles first, then the machine store.

    The curated registry (:func:`get_role`) is checked first.  If it returns
    ``None`` AND a non-empty *store* is provided, the store's attributes are
    checked for a matching name with ``trigger_event``/``effect_event`` set,
    and a synthetic :class:`EdgeRole` is constructed on the fly.

    With an empty or ``None`` store this is identical to ``get_role(attr_name)``.
    """
    curated = get_role(attr_name)
    if curated is not None:
        return curated
    if store is None or store.is_empty:
        return None
    machine_attr = store.get(attr_name)
    if machine_attr is None:
        return None
    if not machine_attr.trigger_event or not machine_attr.effect_event:
        return None
    return EdgeRole(
        trigger_event=machine_attr.trigger_event,
        effect_event=machine_attr.effect_event,
        provenance=machine_attr.provenance,
    )


def build_relay_augmented_event_edges(
    nodes: set[str],
    events: dict[str, dict],
    *,
    store: MachineStore | None = None,
) -> list[tuple[str, str, str]]:
    """Build event-edges with relay-node injection from within-card edge attrs.

    Identical to the ``compute_facts`` event-edge construction, EXCEPT: for each
    curated edge-category attribute in *nodes*, the attribute is injected as:

    - a **consumer** of its ``trigger_event`` (appended to ``triggered_by``)
    - a **producer** of its ``effect_event`` (appended to ``produced_by``)

    This makes within-card edges visible to the event graph so loops can compose
    across cards (Queza consumes draw, produces drain+gain; a gain-to-draw card
    consumes gain, produces draw -> cycle closes).

    Parameters
    ----------
    nodes
        The active attribute nodes from ``compute_facts`` (attrs with >= 3
        support cards or commander attrs).
    events
        ``assets.events`` — the event catalog dict (keys = event slugs,
        values have ``produced_by`` and ``triggered_by`` lists).

    Returns
    -------
    list[tuple[str, str, str]]
        Event edges as ``(producer_attr, consumer_attr, event_name)`` triples,
        same shape as the default ``event_edges`` in ``compute_facts``.
    """
    # Copy the catalog's producer/consumer lists so we never mutate the originals.
    augmented_produced: dict[str, list[str]] = {}
    augmented_triggered: dict[str, list[str]] = {}
    for evname, ev in events.items():
        augmented_produced[evname] = list(ev['produced_by'])
        augmented_triggered[evname] = list(ev['triggered_by'])

    # Inject relay nodes from edge-category attrs present in nodes.
    # Sorted iteration for determinism (the injection order does not affect the
    # cross-product, but the produced_by/triggered_by append order is stable).
    for attr in sorted(nodes):
        role = _get_role_with_store(attr, store)
        if role is None:
            continue
        tev = role.trigger_event
        eev = role.effect_event
        # Guard: both events must exist in the catalog (they always do for the
        # 6 curated attrs, but the guard keeps the function safe for future
        # machine-authored attrs whose events might be novel).
        if tev not in augmented_triggered or eev not in augmented_produced:
            continue
        # Add attr as consumer of its trigger event.
        if attr not in augmented_triggered[tev]:
            augmented_triggered[tev].append(attr)
        # Add attr as producer of its effect event.
        if attr not in augmented_produced[eev]:
            augmented_produced[eev].append(attr)

    # Build event_edges from augmented sets — same cross-product as compute_facts.
    def refs_to_nodes(refs: list[str]) -> list[str]:
        return [r for r in refs if r in nodes]

    event_edges: list[tuple[str, str, str]] = []
    for evname in events:
        for p in refs_to_nodes(augmented_produced[evname]):
            for c_ in refs_to_nodes(augmented_triggered[evname]):
                if p != c_:
                    event_edges.append((p, c_, evname))

    return event_edges


def isolate_engines_with_relay(
    nodes: set[str],
    events: dict[str, dict],
    adens: defaultdict[str, float],
    assets: StrategyAssets,
    cards: list[dict],
    *,
    store: MachineStore | None = None,
) -> dict:
    """Run ``_isolate_engines`` over the relay-augmented event graph.

    This is the on-demand engine-identification entry point.  It builds event
    edges **with** relay-node injection (within-card edges visible) and feeds
    them to the SAME ``_isolate_engines`` function the default path uses.

    The default ``compute_facts`` path is NOT affected — it builds event edges
    from the catalog alone, without relay injection.

    Parameters
    ----------
    nodes, adens, assets, cards
        Same as passed to ``_isolate_engines`` by ``compute_facts``.
    events
        ``assets.events`` — the event catalog dict.
    store
        Optional :class:`MachineStore` for machine-authored edge roles.
        When non-empty, machine-authored attributes' trigger/effect roles
        are injected into the relay graph alongside curated roles.

    Returns
    -------
    dict
        The engine-isolation dict, same shape as ``_isolate_engines`` output.
    """
    augmented_edges = build_relay_augmented_event_edges(nodes, events, store=store)
    return _isolate_engines(nodes, augmented_edges, adens, assets, cards)
