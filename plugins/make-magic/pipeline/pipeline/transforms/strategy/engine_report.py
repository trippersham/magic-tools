"""On-demand engine identification — the Phase PC deliverable.

For a given deck, produces the deck's **engine(s)** — each named, characterized,
and labeled with honest completeness — via the relay-augmented graph walk.

This is a SEPARATE entry point from the default ``compute_facts`` / ``resolve()``
path.  The default path stays byte-identical (the P0 oracle's engine SHA and
resolve SHA are unchanged).  This module composes the held components:

- :func:`.strategy_graph.compute_facts` for ``nodes``/``attr_weight``/``assets``/``cards``
- :func:`.machine_vocab.all_edge_queries` for the full authored-edge set
- :func:`.relay.elect_engine_units_with_relay` for relay-augmented engine isolation
- :func:`.engine_hydration.unauthored_cards` for the unauthored cards

No LLM in the replay path.  Every engine identification is deterministic given
the same deck + curated vocab + machine store.

Public API::

    from pipeline.transforms.strategy.engine_report import identify_engines
    report = identify_engines(deck)
    # report.engines   — named engines
    # report.state     — RESOLVED or PROVISIONAL
    # report.dark_cards — names pending hydration (empty if RESOLVED)
"""

from __future__ import annotations

import enum
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from pipeline.transforms.strategy.assets import is_land
from pipeline.transforms.strategy.engine_hydration import unauthored_cards
from pipeline.transforms.strategy.machine_vocab import (
    CardAttribute,
    ProvisionalAttributeStore,
    all_edge_queries,
    load_store,
)
from pipeline.transforms.strategy.query import CardView, compile_query
from pipeline.transforms.strategy.relay import elect_engine_units_with_relay
from pipeline.transforms.strategy.strategy_graph import compute_facts, determine_node_admitted

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class CompletenessState(enum.Enum):
    """Honesty label for the engine identification result.

    - **RESOLVED** — ``unauthored_cards(deck)`` is empty: every engine-relevant card is
      covered by an authored edge.  Engine(s) stated as fact.
    - **PROVISIONAL** — unauthored_cards non-empty: unauthored cards remain.  Engine(s) stated
      provisionally, never as final.  The named unauthored cards are listed.
    """

    RESOLVED = 'RESOLVED'
    PROVISIONAL = 'PROVISIONAL'


@dataclass(frozen=True)
class NamedEngine:
    """A single named engine (or engine cycle) in the deck.

    Attributes
    ----------
    name
        Human-readable engine name (e.g. ``'draw↔gain cycle'``).
    hub_attrs
        The relay hub attribute(s) that drive the cycle, weight-ordered.
    kind
        ``'cycle'`` for a closed multi-event loop, ``'recursive-engine'`` for
        a single-event recursive hub, ``'static-payoff'`` for a non-recursive
        event with only pure-source producers.
    loop_members
        All attribute names participating in the engine (producers + consumers),
        sorted alphabetically.
    events
        Event-catalog slugs the engine runs through, sorted.
    forward_chains
        Directed event-feed pairs within this engine (e.g.
        ``[('draw-a-card', 'lifegain'), ('lifegain', 'draw-a-card')]``).
    description
        One-line human-readable summary.
    weight
        Combined density weight of all member attributes.
    """

    name: str
    hub_attrs: list[str]
    kind: str
    loop_members: list[str]
    events: list[str]
    forward_chains: list[tuple[str, str]]
    description: str
    weight: float


@dataclass(frozen=True)
class EngineReport:
    """The deliverable: a deck's engines with an honest completeness label.

    Attributes
    ----------
    engines
        Named engines, ordered by weight descending (heaviest first).
    state
        :attr:`CompletenessState.RESOLVED` if there are no unauthored cards,
        :attr:`CompletenessState.PROVISIONAL` if unauthored cards remain.
    dark_cards
        Sorted names of unauthored cards pending hydration.  Empty if RESOLVED.
    commander_loop
        The commander-anchored loop probe result from ``elect_engine_units``,
        or ``None`` if no commander is present.
    raw_isolation
        The full ``elect_engine_units`` output dict (for debugging/introspection).
    """

    engines: list[NamedEngine]
    state: CompletenessState
    dark_cards: list[str]
    commander_loop: dict | None
    raw_isolation: dict


# ---------------------------------------------------------------------------
# Event-name abbreviation for human-readable engine names
# ---------------------------------------------------------------------------


def _abbrev_event(event: str, events: dict) -> str:
    """Abbreviate an event-catalog slug for human-readable names.

    The display abbreviation is co-located on the catalog event (the
    ``abbrev`` field in ``data/event_catalog.json``); events without one
    fall back to the slug itself.
    """
    return events.get(event, {}).get('abbrev', event)


# ---------------------------------------------------------------------------
# SCC detection (Tarjan's) over the forward-chains graph
# ---------------------------------------------------------------------------


def _find_sccs(chains: list[list[str]]) -> list[frozenset[str]]:
    """Find strongly connected components of size > 1 in the forward-chain graph.

    Each chain is ``[source_event, target_event]``.  A bidirectional pair
    (A->B, B->A) forms a 2-node SCC — the typical engine cycle.

    Returns a deterministic (sorted-input) list of frozensets.
    """
    adj: dict[str, set[str]] = defaultdict(set)
    all_nodes: set[str] = set()
    for chain in chains:
        adj[chain[0]].add(chain[1])
        all_nodes.add(chain[0])
        all_nodes.add(chain[1])

    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    sccs: list[frozenset[str]] = []

    def _strongconnect(v: str) -> None:
        indices[v] = lowlinks[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in sorted(adj.get(v, ())):
            if w not in indices:
                _strongconnect(w)
                lowlinks[v] = min(lowlinks[v], lowlinks[w])
            elif w in on_stack:
                lowlinks[v] = min(lowlinks[v], indices[w])

        if lowlinks[v] == indices[v]:
            scc: set[str] = set()
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.add(w)
                if w == v:
                    break
            if len(scc) > 1:
                sccs.append(frozenset(scc))

    for v in sorted(all_nodes):
        if v not in indices:
            _strongconnect(v)

    return sccs


# ---------------------------------------------------------------------------
# Engine characterization — raw isolation -> named engines
# ---------------------------------------------------------------------------


def _characterize_engines(iso: dict, event_catalog: dict) -> list[NamedEngine]:
    """Build :class:`NamedEngine` list from the raw ``elect_engine_units`` output.

    Groups related per-event engines by cycle membership (from
    ``forward_chains``) and characterizes each with hub attrs, loop members,
    events, and a human-readable description.
    """
    raw_engines: list[dict] = iso['engines']
    forward_chains: list[list[str]] = iso['forward_chains']

    if not raw_engines:
        return []

    # Detect cycles in the forward-chain graph.
    sccs = _find_sccs(forward_chains)

    # Map events to their SCC index.
    event_to_scc: dict[str, int] = {}
    for i, scc in enumerate(sccs):
        for ev in scc:
            event_to_scc[ev] = i

    # Partition raw engines into cycle-grouped and standalone.
    scc_groups: dict[int, list[dict]] = defaultdict(list)
    standalone: list[dict] = []

    for eng in raw_engines:
        ev = eng['event']
        if ev in event_to_scc:
            scc_groups[event_to_scc[ev]].append(eng)
        else:
            standalone.append(eng)

    named: list[NamedEngine] = []

    # --- Cycle groups (the main engines) ---
    for scc_idx in sorted(scc_groups):
        engs = scc_groups[scc_idx]
        cycle_events = sorted(sccs[scc_idx])

        all_members: set[str] = set()
        all_hubs: list[str] = []
        total_weight = 0.0

        for eng in engs:
            all_members.update(eng['producers'])
            all_members.update(eng['consumers'])
            if eng['interior_node'] and eng['interior_node'] not in all_hubs:
                all_hubs.append(eng['interior_node'])
            total_weight += eng['mass']

        # Sort hubs by the relay_hubs weight ordering from elect_engine_units.
        relay_hub_order = {h: i for i, h in enumerate(iso.get('relay_hubs', []))}
        all_hubs.sort(key=lambda h: relay_hub_order.get(h, len(relay_hub_order)))

        cycle_chains = [(c[0], c[1]) for c in forward_chains if c[0] in sccs[scc_idx] and c[1] in sccs[scc_idx]]

        ev_abbrevs = [_abbrev_event(e, event_catalog) for e in cycle_events]
        name = '↔'.join(ev_abbrevs) + ' cycle'
        hub_str = ', '.join(all_hubs) if all_hubs else 'none'
        events_str = ', '.join(cycle_events)
        description = f'{name}; hubs: {hub_str}; via {events_str}'

        named.append(
            NamedEngine(
                name=name,
                hub_attrs=all_hubs,
                kind='cycle',
                loop_members=sorted(all_members),
                events=cycle_events,
                forward_chains=cycle_chains,
                description=description,
                weight=round(total_weight, 1),
            )
        )

    # --- Standalone engines (not part of a cycle) ---
    for eng in standalone:
        hub = eng['interior_node']
        members = sorted(set(eng['producers']) | set(eng['consumers']))
        events = [eng['event']]

        ev_abbrev = _abbrev_event(eng['event'], event_catalog)
        name = f'{ev_abbrev} engine' if eng['kind'] == 'recursive-engine' else f'{ev_abbrev} payoff'

        hub_str = hub or 'none'
        description = f'{name}; hub: {hub_str}; via {eng["event"]}'

        named.append(
            NamedEngine(
                name=name,
                hub_attrs=[hub] if hub else [],
                kind=eng['kind'],
                loop_members=members,
                events=events,
                forward_chains=[],
                description=description,
                weight=eng['mass'],
            )
        )

    # Weight descending, name ascending for ties.
    named.sort(key=lambda e: (-e.weight, e.name))
    return named


# ---------------------------------------------------------------------------
# Phase-B assembly: select present combos' cached edges (ADR 0021 B1)
# ---------------------------------------------------------------------------


def _assemble_present_combo_edges(
    store: ProvisionalAttributeStore,
    detected: list[dict],
) -> ProvisionalAttributeStore:
    """Return a store holding only the edges that apply to THIS deck.

    Phase-B assembly (ADR 0021 B1) -- selection, never re-derivation:

    - Non-combo records (``combo_variant_id=''`` -- curated, haiku) pass through,
      de-duplicated by ``name``.
    - Combo-implied edges (``combo_variant_id`` set) are kept only when their
      originating combo is PRESENT in the deck (``detected``).  Each such edge
      is an existing producer attribute ``a`` carrying an extra consumer role
      ``trigger_event`` the combo proved.  The relay stores one role per node
      name, so for each ``a`` this picks a single, **reciprocal-grounded**
      coupling: a role ``a: t -> p`` is grounded when the present combos also
      imply the mirror role ``_: p -> t`` on some producer of ``t`` (so the
      relay forms the closing 2-cycle ``p <-> t``).  Ungrounded couplings are
      dropped -- they cannot close a loop and would only shift mass.

    An already-empty store short-circuits so the committed default stays
    byte-identical.
    """
    if store.is_empty:
        return store

    present: set[str] = {str(c.get('variant_id', '')) for c in (detected or [])}

    kept: list[CardAttribute] = []
    seen_names: set[str] = set()
    combo_edges: list[CardAttribute] = []
    for attr in store.attributes:
        cvid = attr.provenance.combo_variant_id
        if not cvid:
            if attr.name in seen_names:
                continue
            seen_names.add(attr.name)
            kept.append(attr)
            continue
        if cvid in present:
            combo_edges.append(attr)

    # Directed (produced -> trigger) couplings present, and which are grounded
    # by their mirror (trigger -> produced) on SOME producer.
    directed: set[tuple[str, str]] = {(a.produced_event, a.trigger_event) for a in combo_edges}
    grounded = {(p, t) for (p, t) in directed if (t, p) in directed}

    # One grounded role per producer-attr name (lexicographically smallest).
    best: dict[str, tuple[str, str]] = {}
    for a in combo_edges:
        pair = (a.produced_event, a.trigger_event)
        if pair not in grounded:
            continue
        cur = best.get(a.name)
        if cur is None or (a.trigger_event, a.produced_event) < cur:
            best[a.name] = (a.trigger_event, a.produced_event)
    for a in combo_edges:
        if a.name in seen_names:
            continue
        choice = best.get(a.name)
        if choice is None or (a.trigger_event, a.produced_event) != choice:
            continue
        seen_names.add(a.name)
        kept.append(a)

    return ProvisionalAttributeStore(attributes=kept)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def identify_engines(
    deck: dict,
    *,
    store: ProvisionalAttributeStore | None = None,
    store_dir: Path | None = None,
    combos: list[dict] | None = None,
) -> EngineReport:
    """On-demand engine identification for a deck.

    This is the PC entry point.  It loads the deck's intermediate graph state
    via :func:`.strategy_graph.compute_facts`, runs the relay-augmented engine walk,
    and labels the result with an honest completeness state.

    Parameters
    ----------
    deck
        A deck dict with a ``cards`` key (same shape as ``compute_facts``
        expects).
    store
        A pre-loaded :class:`.machine_vocab.ProvisionalAttributeStore`.  If ``None``,
        loads from *store_dir* (which defaults to
        ``~/.local/share/make-magic/machine_vocab/``).
        Pass ``ProvisionalAttributeStore()`` (empty) for a guaranteed-clean run.
    store_dir
        Directory to load the machine store from.  Ignored if *store* is
        given.  **Pass a** ``pathlib.Path``, **not a** ``str`` — there is a
        known str-coercion gap in :func:`.machine_vocab.load_store`.
    combos
        Combo list for ``compute_facts`` (optional; defaults to ``[]``).

    Returns
    -------
    EngineReport
        The deck's engines named, with an honest completeness label.
    """
    # 1. Obtain nodes / attr_weight / assets / cards via compute_facts.
    ctx = compute_facts(deck, combos or [])

    # 2. Load authored edges = curated edge attrs + machine-store attrs.
    if store is None:
        store = load_store(store_dir)

    # 2a. Phase-B assembly (ADR 0021 B1): keep only the cached combo-implied
    # edges (source='spellbook', provenance.combo_variant_id set) whose combo
    # is actually PRESENT in this deck; non-combo records (curated / haiku /
    # single-card self-couplings, combo_variant_id='') pass through unchanged.
    # This is selection, NOT re-derivation -- the edges were authored once, at
    # the combo-intrinsic grain, in Phase A.
    store = _assemble_present_combo_edges(store, ctx.detected)
    authored_edges = all_edge_queries(store)

    # 2b. Inject machine-store attributes into nodes/attr_weight.
    #
    # Machine-authored attributes carry queries that determine membership
    # (same as curated synth attrs in SYNTH_ALWAYS_ATTRIBUTES).  For each
    # machine attr, compile its query, evaluate against every card in the
    # deck, and add the attribute to the card's attribute set / the global
    # attr_weight / nodes.  This is the machine-store -> relay KEYSTONE: without
    # this, machine-authored attrs are never in ``nodes`` and the relay
    # injection in build_relay_augmented_event_edges never fires.
    #
    # With an empty store (the default/committed state), the loop body
    # never executes and behavior is byte-identical to before.
    if not store.is_empty:
        for ma in store.attributes:
            if not ma.query or not ma.trigger_event or not ma.produced_event:
                continue
            # A spellbook combo edge (ADR 0021 B1) is named after an existing
            # producer attribute so the relay AUGMENTS that real node.  When the
            # node is already in ``nodes`` from the native path its mass is
            # already counted -- re-adding it here would double-count attr_weight.
            # Gate the skip on COMBO PROVENANCE (combo_variant_id set) rather
            # than bare node presence: combo-lowered edges are by-construction
            # named after native attrs, so the native path already counted
            # their mass; non-combo store attrs (haiku, future sources) proceed
            # through normal membership evaluation even if their name coincides
            # with a native node.  This makes correctness structural (provenance)
            # rather than reliant on the incidental invariant that query-eval
            # membership always matches native otag membership (C3 review fix).
            if ma.provenance.combo_variant_id and ma.name in ctx.nodes:
                continue
            try:
                pred = compile_query(ma.query)
            except ValueError:
                continue  # skip malformed queries

            support_count = 0
            is_cmd_attr = False
            for card in ctx.cards:
                view = CardView.from_card(card)
                if pred(view):
                    w = ctx.cfg.cmd_weight if card.get('role') == 'commander' else 1.0
                    ctx.attr_weight[ma.name] += w
                    if not is_land(card):
                        support_count += 1
                    if card.get('role') == 'commander':
                        is_cmd_attr = True

            # Node admission: >= 3 support cards OR commander attr (determine_node_admitted, ADR 0004).
            if determine_node_admitted(support_count, is_cmd_attr):
                ctx.nodes.add(ma.name)
            if is_cmd_attr:
                ctx.cmd_attrs.add(ma.name)

    # 3. Run relay-augmented engine isolation.
    iso = elect_engine_units_with_relay(
        ctx.nodes,
        ctx.assets.events,
        ctx.attr_weight,
        ctx.assets,
        ctx.cards,
        store=store,
    )

    # 4. Name / characterize each engine.
    named_engines = _characterize_engines(iso, ctx.assets.events)

    # 5. Honesty label: unauthored_cards determines RESOLVED vs PROVISIONAL.
    dark = unauthored_cards(ctx.cards, authored_edges)
    dark_card_names = sorted(c['name'] for c in dark)

    state = CompletenessState.RESOLVED if not dark_card_names else CompletenessState.PROVISIONAL

    # 6. Build report.
    return EngineReport(
        engines=named_engines,
        state=state,
        dark_cards=dark_card_names,
        commander_loop=iso.get('commander_loop'),
        raw_isolation=iso,
    )
