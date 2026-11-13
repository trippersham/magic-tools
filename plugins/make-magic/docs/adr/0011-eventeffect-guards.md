# 0011 EventEffect guards: a query/reference over a defined operand set at one evaluation chokepoint

| Field | Value |
|---|---|
| Status | Accepted. Guard fields scaffolded (Phase 2A); evaluation semantics land in Phase 2B. |
| Review | pending owner review: agent-authored from design-3 section 3.2/3.3; records the schema as built. |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests/test_eventeffect_scaffolding.py -q` (round-trip, defaults, lifecycle). Parity gate: `uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle` — all three P0 SHAs unchanged (the guard fields are additive + defaulted + inert). |
| Code paths | [`edge_roles.py`](../../pipeline/pipeline/transforms/strategy/edge_roles.py) (`EventEffect` dataclass — renamed from `EdgeRole`; guard scaffolding fields), [`relay.py`](../../pipeline/pipeline/transforms/strategy/relay.py) (`build_relay_augmented_event_edges` — reads `produced_event`), [`machine_vocab.py`](../../pipeline/pipeline/transforms/strategy/machine_vocab.py) (`CardAttribute.produced_event`, `Provenance.lifecycle`) |

## Context

The committed `EdgeRole{trigger_event, effect_event, provenance}` modeled a within-card
edge as a bare `trigger -> effect` pair. Design-3 (the canonical ontology) identified that
this is the **EventEffect** — a guarded directed edge in the event multigraph — and that the
guard is what **defines the producer set** and prevents the walk from manufacturing false wins
(Sheoldred on self-draw promoted 4/4 wrong; 74% of member-closed combos run conditional
converters). The rename + guard scaffolding is Phase 2A; evaluation semantics land in 2B.

## Decision

### Rename

`EdgeRole` is renamed to `EventEffect`; the field `effect_event` is renamed to
`produced_event` (matching design-3 section 3.2: "the Event it produces"). A backward-compat
alias `EdgeRole = EventEffect` exists during the transition. `CardAttribute.effect_event`
(formerly `MachineAttribute`) is similarly renamed to `produced_event`; `Provenance` gains a `lifecycle` field
(`'provisional'` | `'verified'`, default `'verified'`; haiku-authored items default to
`'provisional'`).

### Guard schema (scaffolded, evaluation deferred)

`EventEffect` carries the following guard fields, all **additive, defaulted, and inert** in
Phase 2A (no code path evaluates them yet):

| Field | Type | Default | Semantics (Phase 2B) |
|---|---|---|---|
| `provider` | `str` | `'card'` | `'card'` \| `'CardAttribute'` \| `'rule'` — who provides this edge |
| `guard_characteristic_ref` | `str \| None` | `None` | CardAttribute slug the guard references; also defines the producer set |
| `guard_trigger_scope` | `str` | `'any'` | `'self'` \| `'opponent'` \| `'any'` — whose event instance |
| `guard_identity` | `str` | `'any'` | `'any'` \| `'another'` \| `'self'` — event-source identity relative to the trigger card |
| `guard_event_reachable_ref` | `str \| None` | `None` | Another Event that must be in the deck's reached set |
| `guard_unresolved` | `bool` | `False` | Deferred/warn bucket for unbounded counting / sim-only |
| `magnitude` | `str \| None` | `None` | `'infinite'` \| `'near-infinite'` \| `'scaled'` \| `'finite'` — path-propagated |
| `ability_key` | `str \| None` | `None` | Per trigger-ability identifier |

### Guard semantics (design-3, to be implemented in 2B)

- **The guard defines the producer set.** The characteristic is both the fire-condition and
  what selects the producer-side cards. A bare unguarded event has no producer and forms nothing.
- **Evaluated at ONE chokepoint** — every facet is a predicate over a defined operand set
  (3 rows), not walk branches.
- **Per trigger-ability**, not per-card (a card with two relevant abilities contributes two
  `EventEffect` entries).
- **Infinity-dominance** — a threshold/ordinal gate fed by a magnitude-`infinite` stream is
  satisfied, not deferred.
- **Routing follows ADR 0017** — attr-level characteristic guard first (coarse, native);
  where it over-matches or cannot express the trigger shape, a per-card machine-store edge
  (haiku role A, provisional).

### SHA parity

The guard fields are excluded from parity by design: they have inert defaults and no code path
evaluates them. The `EventEffect` dataclass is never serialized into fixtures or baselines — it
is a sidecar consumed only by the relay path (`build_relay_augmented_event_edges`), which is a
SEPARATE code path from the default `compute_facts`/`resolve` path that the parity oracle hashes.
The `produced_event` rename is a field rename with identical data; `Provenance.lifecycle` is a
new field with a default that matches the effective behavior (curated = verified, haiku = provisional).

## Reasons

- Correctness: without scope + characteristic the walk manufactures wins (closure-validation Fix #1).
  The guard is the single thing that, unfixed, inverts the win-model's purpose.
- Consistency: a query/reference over a defined operand set at one chokepoint keeps guards as
  **verifiable data** (a CardAttribute's query is Scryfall-pasteable) with one walk operation
  (predicate evaluation), no interpreter. The alternative — a growing `trigger_condition` enum
  interpreted by walk branches — is the gate-DSL the grammar-of-operations rejection warned against.
- Provenance: `lifecycle` distinguishes curated-verified from haiku-provisional, enabling the
  promotion mechanism (design-3 section 5) and auditable provenance on every edge.

## Options not taken

- **No guard, rely on the walk alone.** Rejected: 74% false-positive rate on conditional converters.
- **Guard as a JSON blob.** Rejected: structured columns let slug-CI and renames see the refs.
- **Evaluate guards in this phase.** Deferred to 2B: the schema must land first so the curated
  entries can be progressively annotated; shipping evaluation without annotation coverage would
  suppress edges that currently fire (a regression).

## Consequences

- All 6 curated `EventEffect` entries carry inert guard defaults; they will be annotated with
  real guard values in Phase 2B as evaluation semantics arrive.
- The provisional store's `CardAttribute` (renamed from `MachineAttribute`) now writes `produced_event` (not `effect_event`) to JSONL;
  deserialization reads both keys for backward compatibility with pre-rename stores.
- The `Provenance.lifecycle` field enables the design-3 section 5 storage-by-lifecycle rule
  (provisional -> verified promotion) in a future phase.

## Related records

- 0003 — byte-parity held files; the guard fields are parity-safe (additive + inert).
- 0017 — within-card edge derivation; the guard routing follows the same two-tier waterfall.
- 0001 — knowledge placement; guard fields are knowledge-layer data on the EventEffect.
