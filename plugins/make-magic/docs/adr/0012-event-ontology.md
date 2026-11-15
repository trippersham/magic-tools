# 0012 Event = the game's bounded trigger taxonomy; no `role` field — sink-ness is pure topology

| Field | Value |
|---|---|
| Status | Accepted. Observed from `data/event_catalog_v4.json` as built; invariant test added. |
| Review | pending owner review: agent-authored from observed catalog shape + design-3 section 3.1 / design-2 section 2. |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests/test_event_ontology.py -q` (8 assertions: no role/terminal on any event; polarity/scope reserved for game-end; field-vocabulary guard; required-field guard; baseline non-emptiness). Parity gate: `uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle` — both P0 SHAs unchanged (test-only addition). |
| Code paths | [`data/event_catalog_v4.json`](../../pipeline/pipeline/transforms/strategy/data/event_catalog_v4.json) (the held event catalog), [`substantiveness.py`](../../pipeline/pipeline/transforms/strategy/substantiveness.py) (promiscuity-scaled Requirements — ADR 0002), [`facts.py`](../../pipeline/pipeline/transforms/strategy/facts.py) (presence gate), [`engine_hydration.py`](../../pipeline/pipeline/transforms/strategy/engine_hydration.py) (`_load_events`). |

## Context

The event catalog (`data/event_catalog_v4.json`) defines the **bounded trigger taxonomy**
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
   substantiveness evaluator. This is DATA (a `Requirement(kind, params)` in the
   `build_substantiveness` map), not new machinery — the existing evaluator already
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
  (via `synth_attrs_for` in `vocab.py`). No event node — face-down entry is
  covered by `creature-enters-battlefield`.

This keeps the event catalog bounded (only game-rules-derived triggers) while
mechanic identity remains a first-class concept in the attribute and guard systems.

### Promiscuity is gated by substantiveness (ADR 0002), as data

A promiscuous event (e.g. `creature-enters-battlefield`, produced by many attributes)
needs a higher substantiveness bar so the engine does not over-credit incidental
producers. This is expressed as a higher `Requirement` in the `build_substantiveness`
data map — an entry in the map, not a code branch, not new machinery. The substantiveness
evaluator (ADR 0002) already dispatches over its closed kind-set
`{member_count, attr_density, axis_density}`.

## Reasons

- **Topology, not annotation.** Deriving reachability from edges is always consistent with
  the graph; authored `role` annotations can drift. The walk already computes reachability
  — a `role` field would be dead weight that could mislead.
- **No machinery for promiscuity.** The substantiveness evaluator is already built and
  closed-kind — adding a promiscuity-scaled `Requirement` is one data entry, zero code.
- **Minimal special-casing.** Only `game-end` extends the field vocabulary, and only with
  two semantically unique fields (polarity/scope) that no other event needs.

## Options not taken

- **Event `role` field (`source`/`sink`/`converter`).** Rejected: duplicates topology,
  rots silently when edges change, and the walk already computes reachability.
- **`terminal` flag.** Rejected: same reason — a sink is an event with no outgoing edges,
  a computable property.
- **Promiscuity as a new evaluator kind or filter.** Rejected: the existing
  substantiveness evaluator handles it — adding a new kind would expand the kind-set
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
anchors (`anchor_derivation.ANCHOR_MAP` → `derive_event_adjacency`), applied in both
`assets.load_assets` and `engine_hydration._load_events`. The event-node **field vocabulary is
unchanged** (`{rule, produced_by, triggers, abbrev}` + `game-end` extras) — `test_event_ontology.py`
still passes — and no `role`/`terminal` field is added; only the *source* of the two adjacency
lists moves from frozen enumeration to the taxonomy-closure view. The enumerated lists remain in
`event_catalog_v4.json` as the frozen seed (a strict subset of the derived output) but are
overwritten at load. This is an output-moving re-baseline carried under [0022] / [0003].

## Related records

- 0002 — Substantiveness: the evaluator through which promiscuity-scaled Requirements flow.
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
