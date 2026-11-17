# 0009 Unified CardAttribute membership: one `otag:`-query map + one matcher — buckets and payoffs are not special cases

| Field | Value |
|---|---|
| Status | Accepted. Built in the WS1.2 consolidation (ticket #79). |
| Review | pending owner review: agent-authored; records the unification as built. |
| Test | From `plugins/make-magic/pipeline`: `uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle` (ENGINE `7a73aba9`, RESOLVE `1e54293c`, FULL_CORPUS `07eb6673` — all three unchanged pre/post) **and** `uv run --extra dev pytest pipeline/transforms/strategy/tests -q`. |
| Code paths | [`data/attribute_catalog.json`](../../pipeline/pipeline/transforms/strategy/data/attribute_catalog.json) (`card_attributes` map + `bucket_attributes` role list); [`assets.py`](../../pipeline/pipeline/transforms/strategy/assets.py) (`card_attr_preds`, `buckets` reconstruction); [`strategy_graph.py`](../../pipeline/pipeline/transforms/strategy/strategy_graph.py) (`otag_derived_attrs`, the one matcher); [`query.py`](../../pipeline/pipeline/transforms/strategy/query.py) (`compile_query`). |

## Context
A CardAttribute's membership is defined by exactly one thing — a Scryfall `otag:`-style
query (ADR 0008/0011; the `query.py` keystone). Yet the facts layer carried **three**
membership mechanisms that bypassed or paralleled that one model:

- **buckets** — a `{name: slug-list}` map matched by `closure & set(slugs)` (a bucket is
  carried iff the card's otag closure intersects the slug-list), assembled at load from a
  base map **plus a `GROUPS` merge** (`_v4_base_buckets` + `_v4_groups`, folded under
  `enabled_groups`);
- **payoffs** — a `{name: {count, patterns}}` map whose `count`/`patterns` were already
  dead provenance; only the key set was live, matched by `closure & payoff_slugs` (a payoff
  `p` is carried iff `p ∈ closure` — i.e. `otag:p`);
- the group-merge mechanism itself (`enabled_groups` / `_enabled_groups` / `REFINED_GROUPS`),
  which **never narrowed** from its all-groups default in production, tests, or CLI.

Each bucket match is, by construction, the OR of `otag:<slug>` over its slug-list; each
payoff match is exactly `otag:<name>`. So **every bucket and payoff was already an `otag:`
query** — just evaluated by a bespoke set-op instead of the one compiled matcher. Measured
before the change: 27/27 buckets and 822/822 payoffs reproduce with 0 membership shift.

## Decision
One membership model, one matcher. The catalog holds a single **`card_attributes: {name ->
query}`** map merging every bucket and every payoff:

- each bucket → `' OR '.join('otag:'+s for s in slugs)` over its **final, group-merged**
  slug-list (the `GROUPS=all` result baked in — see the group collapse below);
- each payoff → `'otag:'+name`;
- the one bucket∖payoff **name collision** (`typal`, both a bucket and a payoff) resolves to
  the **bucket** query, whose membership is the superset (`otag:typal` ⊆ the bucket's
  OR-list), so the single entry reproduces the old union exactly.

`assets.card_attr_preds` compiles the whole map once (via `compile_query`); the facts layer's
`otag_derived_attrs` admits a name iff its predicate matches a `CardView` over the card's otag
closure — the single pass that **replaced** the separate bucket and payoff lines. The
`event_anchors` / enchantment / EDHREC slug lines are left as direct closure intersections
(already clean `otag:name` membership — out of scope).

Two **role name-lists** ride alongside the unified map, carrying metadata the flat map cannot:

- **`bucket_attributes`** — the bucket-origin names (the multi-slug aggregates). The
  Phase-A combo-edge query builder (`combo_edges._attr_query`) expands these to their slugs
  (reconstructed into `assets.buckets`), and the `event_anchors` split subtracts exactly this
  set (`if ref not in bucket_names`) so a bucket NAME coinciding with an event ref is not
  double-surfaced as an anchor. A payoff name that is an event ref **stays** an anchor — this
  is why the split subtracts the bucket subset, not all of `card_attributes` (263 anchors, not
  177).
- **`support`** (catalog `support_attributes`) — the removal/ramp/draw/tutor role set, consumed
  for `support_quantities`. Byte-identical; preserved as a role tag, not membership.

**Group collapse.** Because `enabled_groups` always equalled all groups, the merge result is a
constant: it is computed once and baked into each bucket's query. `_v4_base_buckets`,
`_v4_groups`, `_v4_new_bucket_names`, and the whole `enabled_groups` / `_enabled_groups` /
`REFINED_GROUPS` / `ALL_GROUPS` mechanism are removed (ADR 0006's value/branch test — the last
group knob was a frozen branch).

## Reasons
Membership had one named definition (the `otag:` query) but three evaluators; a per-card
attribute was recognised one way by the facts layer and another way (already a query string)
by the combo-edge builder — exactly the drift ADR 0001 clause 4 forbids. Collapsing to one
compiled matcher makes the membership of every CardAttribute auditable in one place, in the
one query language, with no bespoke set-ops to keep in sync. The change is byte-parity **by
contract**: each query was proven membership-equivalent to the set-op it replaces, so all
three P0 SHAs hold.

## Options not taken
- **Keep buckets and payoffs as separate maps/matchers.** Rejected: two representations of
  one concept (otag-query membership), guaranteed to drift.
- **Flatten everything into `card_attributes` with no role lists.** Rejected: the
  `event_anchors` split and the combo-edge slug expansion need the bucket/payoff distinction;
  dropping it silently shrinks `event_anchors` (263 → 177) and breaks the combo-edge query
  builder. Roles are metadata beside the map, never a second matcher.
- **Reuse the vocab identity query for a bucket whose NAME is a vocab identity** (e.g.
  `clones`). Rejected: a vocab identity distills an attribute NAME (membership vs edge never
  mix, ADR 0001); its query (`otag:clones`) is NOT the bucket's membership query
  (`otag:copy-creature OR otag:clone`) and diverges. The `card_attributes` value is always the
  bucket's own OR-of-slugs, so parity is guaranteed.

## Consequences
- `StrategyAssets` loses `payoff_slugs` and `new_bucket_names` (both consumer-dead after the
  matcher change) and gains `card_attr_preds`. `assets.buckets` is now reconstructed from the
  bucket queries (order-preserving split on ` OR `) purely for the combo-edge builder + two
  tests; membership no longer routes through it.
- The matcher is one pass over ~848 compiled predicates per card (vs 27 bucket set-ops + one
  payoff intersection); each predicate is membership-order work (an `in`/`any` over the otag
  closure), so per-card cost stays the same order. All three P0 SHAs and the 580-test suite are
  unchanged.
- `attribute_catalog.json` is a byte-frozen held file (ADR 0003); this is a behavior-preserving
  restructure (no output shifts), so no baseline re-generation is required — the equivalence
  proof is the three unchanged SHAs.

## Related records
- 0001 — one-named-rule (clause 4): the drift this removes.
- 0003 — byte-frozen held files; this is an output-preserving restructure of the catalog.
- 0006 — configuration universal-or-gone: the group-mechanism collapse applies its test.
- 0008 / 0011 — the CardAttribute concept + the query/reference operand model this realizes.
- 0022 — `event_anchors` → `otag_derived_attrs`; its anchor membership is byte-preserved here.
