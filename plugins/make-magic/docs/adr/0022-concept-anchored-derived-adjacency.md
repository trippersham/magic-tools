# 0022 Concept-anchored derived adjacency: the Maximal-mechanic-exclusive-anchor rule replaces hand-enumerated otag→Event lists; rank-don't-cut engine keep

| Field | Value |
|---|---|
| Status | **Accepted.** In force on branch `feat/strategy-engine`. Output-moving re-baseline (ADR 0003) carried at the 2026-11-14 flip (task 6.5). |
| Review | Agent-authored from the empirically-validated build design (`research/adjacency-build-design.md`) and the three-round shadow measurement (`research/derived-adjacency-shadow-delta.md`); the orchestrator reconciled, ran the full-170 gauntlet (0 genuine regressions), showed the owner the delta + old→new SHAs, and committed + re-baselined. (Supersedes the `0018-DRAFT` proposal; 0018–0019 remain unused.) |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests -q` (547 passed, 4 skipped). Anchor rule: `test_anchor_derivation.py` (superset `derived ⊇ enumerated` except the intended ramp-correction; `validate_anchor_map()` 0 violations; `TestExclusivityValidator` gate — `ramp` refused for both `mana-produced` and `land-enters-battlefield`; `test_ramp_as_seed_caught_by_validator` — non-tautological acceptance test; `TestEventAdmittedGate` — producer-anchor admission). Parity gate: `uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle` + `test_event_ontology.py` + the re-baselined `FULL_CORPUS_SHA`. |
| Code paths | [`anchor_derivation.py`](../../pipeline/pipeline/transforms/strategy/anchor_derivation.py) (`ANCHOR_MAP`, `derive_event_adjacency`, the enforced exclusivity validator), [`assets.py`](../../pipeline/pipeline/transforms/strategy/assets.py) + [`engine_hydration.py`](../../pipeline/pipeline/transforms/strategy/engine_hydration.py) (derive-at-load), [`facts.py`](../../pipeline/pipeline/transforms/strategy/facts.py) (`_isolate_engines` rank-don't-cut), [`event_catalog_v4.json`](../../pipeline/pipeline/transforms/strategy/data/event_catalog_v4.json) (the frozen enumerated seed, now overwritten at load). |

## Context

The event catalog's `produced_by`/`triggers` were **hand-enumerated otag lists** (~5–6 slugs ×
27 events). They are incomplete by construction: **83/116 (72%) of role-A's authored edges are
correct but fail corroboration because the card's real otag isn't in the list** — e.g.
`creature-enters-battlefield.triggers` listed `enters-in-company` but not `creaturefall`, the otag
that actually means "a creature entered." Measured, the enumerated lists corroborate only
**28.4%** of the 116 edges; the other ~72% of *correct* signal was discarded, forcing recurring
hand batch-wiring (the `mana-produced` correction of ADR 0020 was one). The same lists
**over-captured** in the opposite direction: anchoring `mana-produced.produced_by` at the `ramp`
umbrella pulled in land-fetch / land-drops that are not mana production (ADR 0020 had to drop
`ramp` from mass by hand). These are **one mistake in two directions** — anchoring at the wrong
taxonomy height.

Separately, the ADR 0014 engine mass-election kept the 0..n `elected` set by a **0.6·top-mass
relative keep-cut**. Under the richer derived graph this cut discarded earned secondary engines
on a density-sensitive ratio — a deck whose top event's mass inflated (e.g. the `sac` subtree in
`permanent-dies`) pushed genuine secondary engines (Teysa's `token-created`, Elenda's `lifegain`)
below `0.6·top` even though they were substantive by construction. This is issue **#72**.

## Decision

Two coupled changes, each one named rule (ADR 0001 — one rule, one implementation):

### A. Adjacency is derived — the Maximal mechanic-exclusive anchor rule

> **Maximal mechanic-exclusive anchor.** For each event side, the anchor is the **highest otag
> concept in the taxonomy whose descendant-closure stays exclusive to that event's mechanic.**
> Climb from the specific otags for coverage; **stop one level below any ancestor that would admit
> an otag belonging to a different mechanic.**

- **The adjacency is a *view over the live taxonomy*, not a frozen list.** `ANCHOR_MAP`
  (`anchor_derivation.py`) holds, per event side, the owner-approved mechanic-exclusive **anchor
  seeds**; `derive_event_adjacency(events)` replaces each side's list with the sorted
  descendant-closure union of its anchors. The committed enumerated lists are a strict **subset
  except the intended ramp-correction**: `ramp` was in `land-enters-battlefield.produced_by` in the
  enumerated catalog; the derived adjacency intentionally drops it because `land-ramp` (not `ramp`)
  is the mechanic-exclusive concept for "puts a land onto the battlefield."
- **Authoritative at catalog load.** The derivation is applied where the catalog is loaded —
  `assets.load_assets` (the election/density path) and `engine_hydration._load_events` (the role-A
  hydration path) — so every downstream reader (relay, facts, combo_edges, substantiveness,
  `event_anchors` → `otag_derived_attrs`) sees the derived view. The former
  `SHADOW_DERIVED_ADJACENCY` flag is retired. The enumerated lists remain in
  `event_catalog_v4.json` as the frozen seed but are overwritten at load and are no longer
  authoritative.
- **Corroboration = carries-the-concept.** Card otags are ancestor-closed (verified: a card tagged
  with any descendant already carries the ancestor — 0 violations / 5,439 instances), so
  corroboration is `anchor ∈ card.otags`; new/specific otags a tagger adds later are covered for
  free.
- **Exclusivity is enforced code with an honest boundary.** `is_mechanic_exclusive` /
  `validate_anchor_map` (task 6.1b) measure a candidate ancestor against the
  side's own mechanic population: a node is mechanic-exclusive iff (b) its cards stay ≥
  `EXCLUSIVITY_PRECISION_MIN` inside the mechanic AND (a) it has no foreign heavy child. `ramp` is
  refused for BOTH `mana-produced` (precision 0.56) and `land-enters-battlefield` (0.28) by the
  **same predicate and thresholds** — no per-event special-casing. The output purity check proves no
  refused umbrella leaks into any derived output. The cross-mechanic umbrella check (invariant 3 of
  `validate_anchor_map`, CI-enforced via `MAKE_MAGIC_VALIDATE_ANCHORS=1`) catches a `ramp`
  hand-added as a seed: an anchor whose closure covers ≥ 50% of a disjoint foreign event-side's
  seed set is flagged. Seeds are owner-approved DATA; a `ramp` hand-listed as a seed is a
  reviewable data error now CAUGHT by the CI validator, not silently derived.
  The rule has two facets under the one-named-rule doctrine (ADR 0001):
  *anchor selection* (what seeds are approved — data, validated by CI) and *mechanic exclusivity*
  (the generic climb/output guard — code).

### B. Engine keep is rank-don't-cut (#72)

`_isolate_engines` drops the `0.6·top-mass` relative keep-cut. It keeps **every** substantive unit
ranked by mass desc, with only the Jaccard<0.5 near-duplicate dedup. Units are already substantive
by construction (`node_admitted` + the `producer_anchor` event-admission gate upstream, ADR 0002 /
0020), so a relative-mass cut among them discarded earned engines on a density-sensitive ratio.
**Centrality RANKS, it does not CUT**; substantiveness (upstream) already decided which units
exist. The former `SHADOW_RANK_DONT_CUT` flag is retired. This **supersedes the 0.6 keep-ratio of
ADR 0014** (the Jaccard-0.5 dedup is retained).

## Measured effect

- **Adjacency unlock** (role-A edges, build design): enumerated 28.4% → rule-derived 62.9%
  (+40 edges; trigger 72%→82%, produce 42%→77%).
- **Full-170 gauntlet flip** (vs committed `3ab4034`): 147 decks change election — **142
  elected-set changes** (removals exhaustively audited: 0 genuine regressions, 9 ramp-corrections
  + 6 Jaccard-dedups; additions not individually audited and include low-share land-enters units —
  fetchland noise, see #76), **23 neutral**; elected sizes
  bounded (mean 3.95, max 10, both legitimately multi-axis). **1 `named_strategy` change** (Shiko →
  `alt-wincon/combo`, a real infinite-combo deck with a non-dominant primary — the textbook
  override case, via the event-anchor expansion deflating one span-confidence signal MED→LOW).
  Full classified delta: `research/flip-170-gauntlet-delta.md`.

## Self-healing direction (ADR 0008 lifecycle — not yet built)

The anchor map can gain a per-side `{anchors:[verified], candidates:{otag:{count,first_seen}}}`
shape and a three-way gate (accept on verified anchor; accept-provisional + increment a candidate's
count on a plausible unrecognized otag, promote at **N = 3** independent cards; reject on
over-match) — the existing `provisional → verified` store (ADR 0008) applied to the adjacency
itself. This is the design-4 direction; the current flip derives from static owner-approved seeds
and does **not** yet heal from evidence.

## Points toward #68 (DFC-robust by construction)

Derivation reads **otags** (`oracle_id`-keyed, text-independent), not oracle TEXT regex, so engine
formation works on the ~8% DFC/multi-face pool where the #68 text path goes blind. #68's eventual
fix *widens* coverage rather than being load-bearing for engine formation.

## Simplification ledger (what this removes)

| Removed | Replaced by |
|---|---|
| Hand-enumerated `produced_by`/`triggers` as authoritative (hundreds of slugs × 27 events) | one small anchor-concept map (view over the taxonomy) |
| Manual catalog batch-wiring (the `mana-produced` hand-wiring, ADR 0020) | derive from the taxonomy; the ramp exclusion is the rule's own consequence |
| Corroboration against a frozen incomplete list | corroboration against a self-current taxonomy view |
| The 0.6 relative keep-cut (density-sensitive engine starvation, #72) | rank-don't-cut: centrality ranks the already-substantive units |
| Two shadow flags (`SHADOW_DERIVED_ADJACENCY`, `SHADOW_RANK_DONT_CUT`) | a single authoritative default path |

## Options not taken

- **Keep enumerating but exhaustively.** Rejected: the list can never be current (new otags ship
  every set); it is the source of both the 72% under-capture and the `ramp` over-capture.
- **Anchor at the umbrella (`ramp`/`triggered-ability`) for coverage.** Rejected: over-captures;
  the exclusivity rule's single job is to refuse it.
- **Derive only the election adjacency, keep `event_anchors` from the enumerated seed** (to pin
  labels byte-for-byte). Rejected: adjacency would not be uniformly authoritative; the one label
  change it would avoid (Shiko) is itself a correction, not a regression. Noted as the scoping
  option if the owner later wants label-parity over derived-everywhere.
- **N = 1 candidate promotion** (self-healing). Rejected: admits single-card tagger noise.

## Honest residue

- **~25% of role-A edges stay text-only** and self-healing does not rescue them — they carry no
  relevant otag (own-ETB of non-creature permanents, untagged reanimation, ETB-burn with no ETB
  otag). This is the #68 text frontier, not a healing gap.
- **`event_anchors` now expands corpus-wide** (every deck derives +16 event-anchor CardAttributes
  on average). Its only output consequence across 170 decks is the single Shiko label correction,
  but the density-rank `span` confidence signal is now mildly sensitive to the derived attribute
  count — flagged for the owner as a future confidence-tuning consideration.
- **Self-healing is a direction, not built** — the flip derives from static seeds.

## Related records
- 0001 — one-named-rule doctrine; the Maximal-mechanic-exclusive-anchor rule and rank-don't-cut are each one rule, one impl.
- 0002 / 0020 — substantiveness + the `producer_anchor` gate this adjacency feeds; `ramp` stays anchor-side-only there, never in the adjacency.
- 0003 — the byte-parity re-baseline this flip carries (the 2026-11-14 entry).
- 0008 — the `provisional → verified` store the self-healing direction would reuse.
- 0012 — the Event ontology; this changes how the otag→Event adjacency is *represented* (derived view), with no new field on event nodes.
- 0014 — the engine mass-election; rank-don't-cut here **supersedes** its 0.6 keep-ratio (Jaccard-0.5 dedup retained).
- 0017 — within-card edge derivation (native-then-haiku); this shrinks haiku role A to the residue.
- 0021 — Phase-A Derive is where this lives; unblocks coherence task B1.
- Issues: #72 (keep-ratio starvation — **resolved** by rank-don't-cut), #68 (DFC/text-independence direction), #71 (ramp decomposition, ADR 0020).
