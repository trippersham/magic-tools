---
paths:
  - "**/pipeline/transforms/strategy/edge_roles.py"
  - "**/pipeline/transforms/strategy/relay.py"
  - "**/pipeline/transforms/strategy/machine_vocab.py"
---

# EventEffect guards

An `EventEffect` is a guarded directed edge in the event multigraph (`trigger_event -> produced_event` + provenance + guard fields). Guard fields are scaffolded; evaluation semantics land in Phase 2B.

<eventeffect-guards>

- **`EventEffect`** (renamed from `EdgeRole`) is the canonical name; the transition is complete and the backward-compat alias `EdgeRole = EventEffect` has been removed. Use `EventEffect`.
- **`produced_event`** (renamed from `effect_event`) is the canonical field name for the produced event.
- **Guard fields are ADDITIVE, DEFAULTED, and INERT** in Phase 2A: `provider='card'`, `guard_characteristic_ref=None`, `guard_trigger_scope='any'`, `guard_identity='any'`, `guard_event_reachable_ref=None`, `guard_unresolved=False`, `magnitude=None`, `ability_key=None`. No code path evaluates them yet.
- **The guard defines the producer set** (design-3 section 3.3). A bare unguarded event has no producer and forms nothing.
- **Per trigger-ability**, not per-card — a card with two relevant abilities contributes two `EventEffect` entries.
- **Provenance.lifecycle** (`'provisional'` | `'verified'`, default `'verified'`) distinguishes curated-verified from haiku-provisional.
- **SHA parity is safe**: `EventEffect` is never serialized into fixtures or baselines — it is a sidecar consumed only by the relay path, which is SEPARATE from the default `compute_facts`/`resolve` path that the parity oracle hashes.
- If you change the guard schema or evaluation semantics, update ADR 0011 in the same change.

</eventeffect-guards>
