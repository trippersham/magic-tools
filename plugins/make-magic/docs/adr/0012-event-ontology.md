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

## Amendment — 2026-11-17 (#74 decomposition + gaps): monarch-maker lights up `monarch`; new `poisoned` event, 28 → 29 events

Two ontology changes land the wave the Tier-A amendment deferred, plus a new event:

1. **`monarch` becomes functional** (§2.2 option ii). The `monarch-maker` synth CardAttribute
   (`oracle:'become the monarch'`, 62 makers; a query-membership attribute per [0017], distilling
   to None like `rad-counter`) isolates the PRODUCER side, so `monarch`'s `produced_by` seeds
   `monarch-maker` and `triggers` keeps the payoff otag `monarch-matters`. Maker→payoff edges now
   form (the `p != c_` self-pair exclusion no longer zeroes the node). This is the ONE genuine
   decomposition: the single Scryfall `monarch-matters` tag is split into maker (synth) + payoff
   (otag) sides.
2. **The `poisoned` event is added** (CR 104.3d / 704.5c: ten poison counters = loss, an SBA).
   `produced_by` = the mechanic-exclusive poison GIVER otags (`poisonous`, `poison-opponents`,
   `gives-infect`, `gives-toxic`, `gives-poisonous`; 163 cards, all precision 1.0 vs the poison
   family) — the parent `poison-mechanics` (198) is **excluded** because its closure also carries
   the consumer `synergy-poison` and the `removes-infect`/`gains-*` cards (producer-role impurity).
   `triggers` = the payoff/consumer `synergy-poison` (42). Its `poisoned → game-end` WIN rule-edge
   lives in `combo_win.py` ([0016]). The field vocabulary is unchanged (no `polarity`/`scope` on
   `poisoned`; those stay reserved for `game-end`).

Energy is a GAP fill on the existing `player-counter` node, not an ontology change (producer
`energy-generator` + consumer `counter-fuel-energy`, CR 122.1a). Catalog event count **28 → 29**;
count invariant renamed `test_adjacency.py::test_anchor_map_has_29_events`; grade tally 15/11/3
(monarch's producer went otag→synth so its side grade M→empty; player-counter's producer gained
the energy-generator otag so empty→M; `poisoned` is M/M). Output-moving re-baseline under [0003];
`test_event_ontology.py` + `test_adjacency.py` re-verified green.

## Amendment — 2026-11-17 (ADR 0024 realization): new `domain` state-event, 29 → 30 events

Realizes [0024] (a wincon is an event that reaches `game-end`; its enablers are the producers of
that event). One ontology change plus a producer gap-fill:

1. **The `domain` state-event is added** (CR 104.2a: an effect states that a player wins — Coalition
   Victory: control a land of each basic land type AND a creature of each color → you win). The
   reusable board-state is a state-event keyed by KIND (not by threshold), mirroring `poisoned`.
   `produced_by` = the authored SYNTH CardAttribute `color-fixing` (`oracle:'mana of any color' OR
   otag:rainbow-land`, 709 fixers; no otag is both mechanic-exclusive and producer-role-pure, so it
   is authored per [0009]/[0017]/[0023] principle 8 — the evaluator is NOT grown with a
   `produced_mana` atom, [0009]; KNOWN GAP: basic-land-TYPE fixers are not covered). `triggers` =
   the authored SYNTH CardAttribute `five-color-win` (`otag:alternate-win-condition AND oracle:'each
   color'` = exactly Coalition Victory + Call the Spirit Dragons). Both synth attrs distill to None
   and have no otag, so both sides grade `empty` (closure = themselves). Its `domain → game-end` WIN
   rule-edge lives in `combo_win.py` (`EVENT_EDGE_CLASSES['domain'] = ['altwin::domain']`, [0016]),
   walk-inert until a domain fid route exists (none today, as with `poisoned`). Field vocabulary
   unchanged (no `polarity`/`scope`).
2. **`free-cast-another` (437) folds into `spell-cast.produced_by`** — a card that casts another
   spell for free makes a real CR 601 cast; a producer gap-fill, not an ontology change.

**`life-threshold` was specced in [0024] but DROPPED**: the existing `lifegain` event already models
the lifegain-win end-to-end (`EVENT_EDGE_CLASSES['lifegain']` already owns `altwin::lifegain` =
Felidar/Angel/Test, and its triggers already carry the `life-total-matters-self`/`lifegain-matters`
payoffs). A second `life-threshold` event would double-count that win-edge, so it is subsumed rather
than added. Catalog event count **29 → 30**; count invariant renamed
`test_adjacency.py::test_anchor_map_has_30_events`; grade tally 15/11/**4** (`domain` is empty/empty).
Output-moving re-baseline under [0003]; `test_event_ontology.py` + `test_adjacency.py` re-verified
green.

## Amendment — 2026-11-18 (ADR 0025: grant-vs-use collapse of `deathtouch-combat`), 30 → 29 events

Realizes [0025] (a keyword's grant produces its nearest tracked event; a keyword is never its own
event — the mechanic-class clause above, generalized). The one over-modeled keyword USE node,
**`deathtouch-combat`**, is removed: deathtouch is a static combat-damage modifier of the same shape as
double-strike/trample/evasion, so its grants (`deathtouch-counter`/`gains-deathtouch`/`gives-deathtouch`)
become **producers of `combat-damage-dealt`** (lethal combat damage) **and `permanent-dies`**
(deathtouch kills what it fights), not their own event. The keyword-tribal payoff (`synergy-deathtouch`
+ the tiny `old-*-deathtouch`) stays in the `deathtouch` distill identity (the archetype layer, where
mechanic-class belongs) — dropped from the event layer, not rehomed as a trigger. `deathtouch-combat`
was fully-M (both sides grade M) and carried no win-edge, so `EVENT_EDGE_CLASSES` is unchanged.
Catalog event count **30 → 29**; count invariant renamed
`test_adjacency.py::test_anchor_map_has_29_events`; grade tally 15/**10**/4 (fully-M 11 → 10).
Field vocabulary unchanged (no `polarity`/`scope`). Gated on the Shelob fixture (PRESERVED + IMPROVED:
its `permanent-dies` aristocrats engine gains the deathtouch producer, identity byte-identical);
output-moving re-baseline under [0003] (exactly 2/170 gauntlet + 1/17 fixture records moved, 0 hard
flips); `test_event_ontology.py` + `test_adjacency.py` re-verified green.

## Amendment — 2026-11-18 (ADR 0025 resource-split doctrine — Batch A: two splits), 29 → 33 events

Realizes the **split half** of [0025] (distinct CR resources, each with their own producers/consumers,
earn their own event — the mirror of the keyword fold). **Two splits:**

- **A1 — `spell-cast` split (29 → 31).** A cast (CR 601) and a copy (CR 707.10/.12) are distinct stack
  events (a copy is not cast). The old `spell-cast` is redefined to the **cast-only** node (producers
  `impulse-cast`/`free-cast-another`; copies removed); two new events are added: **`spell-copied`**
  (the `copy-*` producers; empty triggers — no copy-only payoff otag exists) and **`spell-on-stack`**
  (the cast∪copy union; triggers `magecraft`, CR 702.157a "cast or copy" — the one payoff on the
  superset). A copy engine lands on spell-copied + spell-on-stack and reaches magecraft without
  over-promising the cast-only storm/prowess payoffs.
- **A2 — `player-counter` split (31 → 33).** The generic `player-counter` lumped three distinct CR
  resources; it is **removed** and replaced by **`energy`** (CR 122.1a; `energy-generator` →
  `counter-fuel-energy`), **`rad`** (Fallout; synth `rad-counter` → self-mill), and **`experience`**
  (C15+; the NEW synth `experience-counter`, `oracle:'experience counter'`, 19 members 0 FP; triggers
  empty — the payoff is the card's own scaling ability). `proliferate` (`pseudo-proliferate`/
  `repeatable-proliferate`) is the shared producer seeded into all three.

Net catalog event count **29 → 33** — **6 new** (`spell-cast`[redefined], `spell-copied`,
`spell-on-stack`, `energy`, `rad`, `experience`) and **2 removed** (old `spell-cast`, `player-counter`).
`combo_win.py`: `EVENT_EDGE_CLASSES['spell-cast']` keeps `cast_drain`+`altwin::spell-cast`;
`spell-on-stack` gets `cast_drain`; `altwin::player-counter` (Azor's filibuster) is now **unmapped**
(walk-inert — the former node spuriously let infinite energy/experience "win" through it); `fid_route`
re-routes 7 magecraft/copy fids → `spell-on-stack`, energy/experience fids → their events. Field
vocabulary unchanged (no `polarity`/`scope`). Count invariant renamed
`test_adjacency.py::test_anchor_map_has_33_events`; grade tally **15/12/6** (S unchanged; fully-M 10 →
12 via spell-on-stack/energy/rad +3, player-counter −1; empty 4 → 6 via spell-copied + experience).
Output-moving re-baseline under [0003] (exactly 10/157 gauntlet engine relocations + 1 benign n_nodes,
0 hard flips; 1/17 fixtures moved); `test_event_ontology.py` + `test_adjacency.py` re-verified green.

## Amendment — 2026-11-18 (Batch B: five producer-concept edits), 33 events unchanged

No events added or removed (33 unchanged); five producer/consumer concept refinements (full detail in
[0003]). The only ontology-shape change: **B5** populates `library-depleted` (was empty/empty) with
`produced_by` = `mill-opponent`/`mill-each`/`mill-exile` (NOT `mill-self`) + `triggers` =
`empty-library`/`library-size-matters`, moving it from the empty bucket to **fully-M**. **B4** adds
`alternate-loss-condition` to `game-end.produced_by` alongside `alternate-win-condition` — `game-end`
keeps its raw terminal invariant (`produced_by`/`triggers` == `[]`); polarity stays `game-end`-only and
is read per-producer/per-card by `combo_win.fid_route`, never off the node's polarity field. **B3**
adds `gains-poisonous` to `poisoned.produced_by` (grade already M). **B1/B2** refine the `monarch-maker`
/ `color-fixing` synth CardAttributes (attribute layer, not events). Grade tally **15/12/6 → 15/13/5**
(library-depleted empty → fully-M); count invariant `test_anchor_map_has_33_events` unchanged.
`test_event_ontology.py` gains `test_library_depleted_is_populated` and drops `library-depleted` from
the still-inert set (game-end stays inert). Output-moving re-baseline under [0003] (0 hard flips; 35/157
+ 1/17 benign n_nodes +1 — the edge-less color-fixing node; 0 engine relocations).

## Amendment — 2026-11-18 (R5d: owner-ratified producer promotions), 33 events unchanged

No events added or removed (33 unchanged); field vocabulary unchanged; no ontology-shape change. Four
already-M-graded producer events gained ratified `produced_by` seeds under the owner's promotion rule
(detail in [0003]/[0022]): **creature-copied** 4 → 13 seeds (+9), **creature-enters-battlefield** 11 →
22 (+11), **land-enters-battlefield** 6 → 11 (+5), **token-created** 13 → 16 (+3); **artifact-enters**
unchanged (promoted nothing). Because all four were already grade **M** with non-empty seeds, the
**grade tally stays 15/13/5** and the count invariant `test_anchor_map_has_33_events` is unchanged — the
promotion only enriches existing events' producer seed sets, it does not touch the event ontology
shape. Output-moving re-baseline under [0003] (0 headline/identity flips; 2 engines recovered, 0
spurious; 73 strengthened; Shelob preserved + strictly improved).

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
| Strategy        | —                    | —                               | the substantive sub-graph itself (admitted nodes + event edges + engine units); `archetype_identity` is its quotient into the shared archetype vocabulary — a compatibility projection, not the Strategy |
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
