# 0012 Event = the game's bounded trigger taxonomy; no `role` field — sink-ness is pure topology

| Field | Value |
|---|---|
| Status | Accepted. Observed from `data/event_catalog.json` as built; invariant test added. |
| Review | pending owner review: agent-authored from observed catalog shape + design-3 section 3.1 / design-2 section 2. |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests/test_event_ontology.py -q` (8 assertions: no role/terminal on any event; polarity/scope reserved for game-end; field-vocabulary guard; required-field guard; baseline non-emptiness). Parity gate: `uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle` — both P0 SHAs unchanged (test-only addition). |
| Code paths | [`data/event_catalog.json`](../../pipeline/pipeline/transforms/strategy/data/event_catalog.json) (the held event catalog), [`determine_strategy_substantiveness.py`](../../pipeline/pipeline/transforms/strategy/determine_strategy_substantiveness.py) (promiscuity-scaled Requirements — ADR 0002), [`strategy_graph.py`](../../pipeline/pipeline/transforms/strategy/strategy_graph.py) (presence gate), [`engine_hydration.py`](../../pipeline/pipeline/transforms/strategy/engine_hydration.py) (`_load_events`). |

## Context

The event catalog (`data/event_catalog.json`) defines the **bounded trigger taxonomy**
of the game — the complete set of game events a deck's cards can produce or respond to
(land-ETB, creature-ETB, dies, attacks, spell-cast, lifegain, etc.). Each event node
carries `{rule, produced_by, triggers}` and optionally `abbrev` for display.

Design-3 (section 3.1) and design-2 (section 2) settled three questions:

1. **Should events carry a `role` (source/sink/converter)?** No — an event's reachability
   (which events reach which) is a GRAPH PROPERTY derived from edges, never an authored
   annotation. Authoring `role` duplicates topology and rots when edges change.

2. **Should events carry `terminal` flags?** No — whether an event is a sink (no
   outgoing edges) is again pure topology. The only exception is `game-end`, which
   terminates the walk with a polarity (win/draw/loss) and scope (table/target/self).

3. **How is promiscuity controlled?** A maximally promiscuous event (one that many
   attributes produce or trigger) earns a higher `Requirement` bar via ADR 0002's
   substantiveness determiner. This is DATA (a `Requirement(kind, params)` in the
   `build_substantiveness` map), not new machinery — the existing determiner already
   dispatches over a closed kind-set.

## Decision

### No `role` or `terminal` field

Event nodes carry **only** `{rule, produced_by, triggers}` plus the optional
display field `abbrev`. No event carries `role`, `terminal`, `polarity`, or `scope`,
with one reserved exception: `game-end` (to be wired in Phase 2B with the fid adapter)
may carry `polarity` and `scope`. Reachability and sink-ness are pure topology, derived
from the event multigraph's edges, never authored on the node.

### `polarity`/`scope` on `game-end` only

When `game-end` is added to the catalog (Phase 2B), it carries:
- `polarity`: `'win'` | `'draw'` | `'loss'` — the outcome direction.
- `scope`: `'table'` | `'target'` | `'self'` — who the outcome affects.

No other event may carry these fields. Until `game-end` is wired, the invariant test
asserts that no event carries polarity or scope at all.

### Mechanic-class: CardAttribute, not Event

A mechanic-class (saga, dice, dungeon, morph, energy, etc.) is modeled as a
**CardAttribute** (ADR 0008), not as a shared Event node. The event taxonomy
is bounded to the game's trigger rules; mechanic identity flows through the
attribute system instead:

- **Saga** → the lore counter triggers the `counter-placed` event. "Saga" itself
  is a CardAttribute, queryable as a guard operand (ADR 0011).
- **Dice rolling / dungeon venture** → each gets its own event slug if one exists
  in the catalog (`die-rolled`, `ventured`), or `None` if not yet cataloged. The
  mechanic-class ("dice", "dungeon") is a CardAttribute.
- **Morph / manifest / disguise** → a CardAttribute synthesized from card keywords
  (via `synth_attrs_for` in `distill_strategies.py`). No event node — face-down entry is
  covered by `creature-enters-battlefield`.

This keeps the event catalog bounded (only game-rules-derived triggers) while
mechanic identity remains a first-class concept in the attribute and guard systems.

### Promiscuity is gated by substantiveness (ADR 0002), as data

A promiscuous event (e.g. `creature-enters-battlefield`, produced by many attributes)
needs a higher substantiveness bar so the engine does not over-credit incidental
producers. This is expressed as a higher `Requirement` in the `build_substantiveness`
data map — an entry in the map, not a code branch, not new machinery. The substantiveness
determiner (ADR 0002) already dispatches over its closed kind-set
`{member_count, attr_density, axis_density}`.

## Reasons

- **Topology, not annotation.** Deriving reachability from edges is always consistent with
  the graph; authored `role` annotations can drift. The walk already computes reachability
  — a `role` field would be dead weight that could mislead.
- **No machinery for promiscuity.** The substantiveness determiner is already built and
  closed-kind — adding a promiscuity-scaled `Requirement` is one data entry, zero code.
- **Minimal special-casing.** Only `game-end` extends the field vocabulary, and only with
  two semantically unique fields (polarity/scope) that no other event needs.

## Options not taken

- **Event `role` field (`source`/`sink`/`converter`).** Rejected: duplicates topology,
  rots silently when edges change, and the walk already computes reachability.
- **`terminal` flag.** Rejected: same reason — a sink is an event with no outgoing edges,
  a computable property.
- **Promiscuity as a new determiner kind or filter.** Rejected: the existing
  substantiveness determiner handles it — adding a new kind would expand the kind-set
  without justification when a data entry suffices.

## Consequences

- The event catalog field vocabulary is frozen to `{rule, produced_by, triggers, abbrev}`
  plus `{polarity, scope}` on `game-end` only. The invariant test
  (`test_event_ontology.py`) enforces this.
- When `game-end` lands in Phase 2B, the conditional test guard flips to a positive
  assertion (game-end must carry polarity + scope).
- Promiscuity thresholds are authored in `build_substantiveness()` as `Requirement`
  entries keyed by archetype name — the same mechanism all substantiveness uses (ADR 0002).

## Amendment — 2026-11-14 ([0022], task 6.5): the otag→Event adjacency is a DERIVED view

The `produced_by`/`triggers` lists are no longer authoritative as hand-enumerated catalog data:
they are **derived at catalog load** from each event side's owner-approved mechanic-exclusive
anchors (`SEED_MAP` → `derive_card_events`), applied in both
`assets.load_assets` and `engine_hydration._load_events`. The event-node **field vocabulary is
unchanged** (`{rule, produced_by, triggers, abbrev}` + `game-end` extras) — `test_event_ontology.py`
still passes — and no `role`/`terminal` field is added; only the *source* of the two adjacency
lists moves from frozen enumeration to the taxonomy-closure view. The enumerated lists remain in
`event_catalog.json` as the frozen seed (a strict subset of the derived output) but are
overwritten at load. This is an output-moving re-baseline carried under [0022] / [0003].

## Amendment — 2026-11-17 (#74 Tier-A): monarch/extra-turn split, 27 → 28 events

The conflated `monarch-or-extra-turn` node (CR 720 Taking an Extra Turn ∪ CR 726 The Monarch —
two unrelated mechanics that formed a false `extra-turn → monarch-matters` edge) is split into two
bounded nodes: **`extra-turn`** (produced_by `extra-turn`, triggers `turns-taken-matter`) and
**`monarch`** (produced_by `monarch-matters`, triggers `monarch-matters`). `monarch` is **edge-inert
by construction**: Scryfall carries one `monarch-matters` tag on both makers and payoffs, so the
single attribute on both sides forms no edge (the `p != c_` self-pair exclusion in `strategy_graph`);
the node documents the mechanic and bounds the taxonomy until a `monarch-maker` synth-attribute split
lights it up in a later wave. `off-turn-casting-matters` (a flash/off-turn-cast payoff, neither extra
turns nor monarch) was dropped. Catalog event count **27 → 28**; the field vocabulary is unchanged and
`test_event_ontology.py` still passes (`game-end` gained producers/consumers via SEED_MAP seeds while
its raw JSON lists stay `[]`, preserving the terminal-inert invariant that test asserts). Count
invariant: `test_adjacency.py::test_anchor_map_has_28_events`. Output-moving re-baseline under [0003].

## The bipartite graph and its projections

The strategy engine operates on ONE bipartite graph with two projections.
Every primitive in the system — Event, EventEffect, CardAttribute, Strategy,
engine unit — has a well-defined home in one of these layers.

### Base graph (bipartite)

Two node colors:

- **Attribute** nodes (otag slugs / synthesised CardAttributes — the vocabulary of
  `distill_strategies.py` + `synth_attrs_for`).
- **Event** nodes (the 28 slugs in `data/event_catalog.json`; see the 2026-11-17 amendment).

Two directed edge types, both drawn from the event catalog's adjacency lists
(produced at load by `derive_card_events`, amendment above):

- `attribute --produces--> event` — an attribute listed in an event's `produced_by`.
- `event --triggers--> attribute` — an attribute listed in an event's `triggers`.

The base bipartite graph is the **single source of truth**. Every downstream
structure is a projection of it.

### Projection 1 — the event multigraph (ontology / authoring layer)

Collapse attribute-nodes. What remains:

- **Nodes** = Events.
- **Edges** = EventEffects (`trigger_event → produced_event`, defined in
  `edge_roles.py:EventEffect` — "a guarded directed edge in the event
  multigraph"). Each EventEffect is a path of length 2 through the base
  bipartite graph (`trigger_event → attribute → produced_event`), with the
  attribute collapsed out.

This projection is the **authoring surface**: within-card edges are authored
as EventEffects (Projection 1 edges). They are lowered into Projection 2 by
relay-injection (`relay.py:build_relay_augmented_event_edges`) — the card's
attribute is re-expanded as a consumer of `trigger_event` and a producer of
`produced_event`, restoring the bipartite structure for the election graph.

### Projection 2 — the election graph (`elect_engine_units`)

Collapse event-nodes into edge-labels. What remains:

- **Nodes** = Attributes (the same CardAttribute slugs that were nodes in the
  base bipartite graph).
- **Edge-labels** = Events — each edge is a triple
  `(producer_attr, consumer_attr, event_name)`, constructed in `strategy_graph.py`
  (lines 617–622) by the cross-product of an event's `produced_by` × `triggers`
  filtered to in-deck nodes.

The mass-election (`elect_engine_units`) runs HERE: per-event convergence clusters
are the atomic engine units, ranked by node-weighted mass (ADR 0014).

### Strategy view (quotient of Projection 2)

Group Projection 2's attribute-nodes by their archetype
(`assets.distill_attr`) → strategy-level groupings. This is the
deck-level language (`strategies` dict in `facts`).

### Engine unit

A connected cluster in Projection 2 (one event's producer ∪ consumer set).
An **elected** unit is a ranked such cluster that passes event-substantiveness
(ADR 0020), archetype-substantiveness (ADR 0002 / 0021 B6), and the
Jaccard-dedup filter.

### Projection summary

| Primitive       | Base bipartite        | Projection 1 (event multigraph) | Projection 2 (election graph)   |
|-----------------|----------------------|---------------------------------|--------------------------------|
| Attribute       | node (color A)       | collapsed out                   | **node**                       |
| Event           | node (color B)       | **node**                        | **edge-label**                 |
| EventEffect     | length-2 path        | **edge** (trigger → produced)   | relay-injection (attr as consumer + producer) |
| Strategy        | —                    | —                               | archetype grouping (quotient of attribute-nodes) |
| Engine unit     | —                    | —                               | connected cluster (producer ∪ consumer per event) |

## Related records

- 0002 — Substantiveness: the determiner through which promiscuity-scaled Requirements flow.
- [0022](0022-concept-anchored-derived-adjacency.md) — the concept-anchored derivation that now
  produces this taxonomy's `produced_by`/`triggers` lists (adjacency as a derived view).
- 0003 — Byte-parity: the event catalog is a held file; this change is test-only (no
  catalog data change), so P0 SHAs are unchanged.
- 0008 — Canonical normalization / CardAttribute: the mechanic-class clause routes
  mechanic identity through the attribute system, not the event taxonomy.
- 0011 — EventEffect guards: the guards define which edges fire between events; this
  record governs the events themselves. Mechanic-class CardAttributes serve as guard
  operands (ADR 0011 characteristic refs).
- 0016 — Closure-mechanism win-walk: the walk consumes this taxonomy through the
  fid-to-Event adapter (ADR 0007).
