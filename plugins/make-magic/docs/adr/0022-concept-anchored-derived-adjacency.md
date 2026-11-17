# 0022 Concept-anchored derived adjacency: the Maximal-mechanic-exclusive-seed rule replaces hand-enumerated otag→Event lists; rank-don't-cut engine keep

| Field | Value |
|---|---|
| Status | **Accepted.** In force on branch `feat/strategy-engine`. Output-moving re-baseline (ADR 0003) carried at the 2026-11-14 flip (task 6.5). |
| Review | Agent-authored from the empirically-validated build design (`research/adjacency-build-design.md`) and the three-round shadow measurement (`research/derived-adjacency-shadow-delta.md`); the orchestrator reconciled, ran the full-170 gauntlet (0 genuine regressions), showed the owner the delta + old→new SHAs, and committed + re-baselined. (Supersedes the `0018-DRAFT` proposal; 0018–0019 remain unused.) |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests -q` (547 passed, 4 skipped). Seed rule: `test_adjacency.py` (superset `derived ⊇ enumerated` except the intended ramp-correction; `validate_seed_map()` 0 violations; `TestExclusivityValidator` gate — `ramp` refused for both `mana-produced` and `land-enters-battlefield`; `test_ramp_as_seed_caught_by_validator` — non-tautological acceptance test; `TestEventAdmittedGate` — producer-anchor admission). Parity gate: `uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle` + `test_event_ontology.py` + the re-baselined `FULL_CORPUS_SHA`. |
| Code paths | [`derive_card_events.py`](../../pipeline/pipeline/transforms/strategy/derive_card_events.py) (`SEED_MAP`, `derive_card_events`, the enforced exclusivity validator), [`assets.py`](../../pipeline/pipeline/transforms/strategy/assets.py) + [`engine_hydration.py`](../../pipeline/pipeline/transforms/strategy/engine_hydration.py) (derive-at-load), [`strategy_graph.py`](../../pipeline/pipeline/transforms/strategy/strategy_graph.py) (`elect_engine_units` rank-don't-cut), [`event_catalog.json`](../../pipeline/pipeline/transforms/strategy/data/event_catalog.json) (the frozen enumerated seed, now overwritten at load). |

## Context

The event catalog's `produced_by`/`triggers` were **hand-enumerated otag lists** (~5–6 slugs ×
27 events). They are incomplete by construction: **83/116 (72%) of role-A's authored edges are
correct but fail corroboration because the card's real otag isn't in the list** — e.g.
`creature-enters-battlefield.triggers` listed `enters-in-company` but not `creaturefall`, the otag
that actually means "a creature entered." Measured, the enumerated lists corroborate only
**28.4%** of the 116 edges; the other ~72% of *correct* signal was discarded, forcing recurring
hand batch-wiring (the `mana-produced` correction of ADR 0020 was one). The same lists
**over-captured** in the opposite direction: seeding `mana-produced.produced_by` at the `ramp`
umbrella pulled in land-fetch / land-drops that are not mana production (ADR 0020 had to drop
`ramp` from mass by hand). These are **one mistake in two directions** — seeding at the wrong
taxonomy height.

Separately, the ADR 0014 engine mass-election kept the 0..n `elected` set by a **0.6·top-mass
relative keep-cut**. Under the richer derived graph this cut discarded earned secondary engines
on a density-sensitive ratio — a deck whose top event's mass inflated (e.g. the `sac` subtree in
`permanent-dies`) pushed genuine secondary engines (Teysa's `token-created`, Elenda's `lifegain`)
below `0.6·top` even though they were substantive by construction. This is issue **#72**.

## Decision

Two coupled changes, each one named rule (ADR 0001 — one rule, one implementation):

### A. Adjacency is derived — the Maximal mechanic-exclusive seed rule

> **Maximal mechanic-exclusive seed.** For each event side, the seed is the **highest otag
> concept in the taxonomy whose descendant-closure stays exclusive to that event's mechanic.**
> Climb from the specific otags for coverage; **stop one level below any ancestor that would admit
> an otag belonging to a different mechanic.**

- **The adjacency is a *view over the live taxonomy*, not a frozen list.** `SEED_MAP`
  (`derive_card_events.py`) holds, per event side, the owner-approved mechanic-exclusive **seeds**;
  `derive_card_events(events)` replaces each side's list with the sorted
  descendant-closure union of its seeds. The committed enumerated lists are a strict **subset
  except the intended ramp-correction**: `ramp` was in `land-enters-battlefield.produced_by` in the
  enumerated catalog; the derived adjacency intentionally drops it because `land-ramp` (not `ramp`)
  is the mechanic-exclusive concept for "puts a land onto the battlefield."
- **Authoritative at catalog load.** The derivation is applied where the catalog is loaded —
  `assets.load_assets` (the election/density path) and `engine_hydration._load_events` (the role-A
  hydration path) — so every downstream reader (relay, facts, combo_edges, substantiveness,
  `event_anchors` → `derive_card_attributes`) sees the derived view. The former
  `SHADOW_DERIVED_ADJACENCY` flag is retired. The enumerated lists remain in
  `event_catalog.json` as the frozen seed but are overwritten at load and are no longer
  authoritative.
- **Corroboration = carries-the-concept.** Card otags are ancestor-closed (verified: a card tagged
  with any descendant already carries the ancestor — 0 violations / 5,439 instances), so
  corroboration is `seed ∈ card.otags`; new/specific otags a tagger adds later are covered for
  free.
- **Exclusivity is enforced code with an honest boundary.** `is_mechanic_exclusive` /
  `validate_seed_map` (task 6.1b) measure a candidate ancestor against the
  side's own mechanic population: a node is mechanic-exclusive iff (b) its cards stay ≥
  `EXCLUSIVITY_PRECISION_MIN` inside the mechanic AND (a) it has no foreign heavy child. `ramp` is
  refused for BOTH `mana-produced` (precision 0.56) and `land-enters-battlefield` (0.28) by the
  **same predicate and thresholds** — no per-event special-casing. The output purity check proves no
  refused umbrella leaks into any derived output. The cross-mechanic umbrella check (invariant 3 of
  `validate_seed_map`, CI-enforced via `MAKE_MAGIC_VALIDATE_ANCHORS=1`) catches a `ramp`
  hand-added as a seed: an seed whose closure covers ≥ 50% of a disjoint foreign event-side's
  seed set is flagged. Seeds are owner-approved DATA; a `ramp` hand-listed as a seed is a
  reviewable data error now CAUGHT by the CI validator, not silently derived.
  The rule has two facets under the one-named-rule doctrine (ADR 0001):
  *seed selection* (what seeds are approved — data, validated by CI) and *mechanic exclusivity*
  (the generic climb/output guard — code).

### B. Engine keep is rank-don't-cut (#72)

`elect_engine_units` drops the `0.6·top-mass` relative keep-cut. It keeps **every** substantive unit
ranked by mass desc, with only the Jaccard<0.5 near-duplicate dedup. Units are already substantive
by construction (`determine_node_admitted` + the `producer_anchor` event-admission gate upstream, ADR 0002 /
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
  fetchland noise, see #76 — **NOT retired by B6** (2026-11-15): B6's archetype-grain election
  gate keeps the Alesha `land-enters-battlefield` unit because its `landfall` claim clears the
  default `member_count(3)` bar (3 generic ramp/removal cards tagged `landfall`), so it is
  exempt under policy (a); retiring #76 would need a landfall-specific density `Requirement`
  — an owner-graded [0002] DATA decision, outside B6's generic reuse of the existing bar), **23
  neutral**; elected sizes
  bounded (mean 3.95, max 10, both legitimately multi-axis). **1 `named_strategy` change** (Shiko →
  `alt-wincon/combo`, a real infinite-combo deck with a non-dominant primary — the textbook
  override case, via the event-anchor expansion deflating one span-confidence signal MED→LOW).
  Full classified delta: `research/flip-170-gauntlet-delta.md`.

## Seed-discovery direction — event-candidate evaluation (ADR 0008 lifecycle — not yet built)

> Formerly described as "self-healing." The machinery *evaluates candidate* event-producers/
> triggers and *discovers* new seeds from evidence; it does not "heal." The canonical verb for
> this stage is **evaluate** (as in `evaluate_event_candidates`).

The seed map can gain a per-side `{seeds:[verified], candidates:{otag:{count,first_seen}}}`
shape and a three-way gate (accept on verified seed; accept-provisional + increment a candidate's
count on a plausible unrecognized otag, promote at **N = 3** independent cards; reject on
over-match) — the existing `provisional → verified` store (ADR 0008) applied to the adjacency
itself. This is the design-4 direction; the current flip derives from static owner-approved seeds
and does **not** yet discover new seeds from evidence.

## Points toward #68 (DFC-robust by construction)

Derivation reads **otags** (`oracle_id`-keyed, text-independent), not oracle TEXT regex, so engine
formation works on the ~8% DFC/multi-face pool where the #68 text path goes blind. #68's eventual
fix *widens* coverage rather than being load-bearing for engine formation.

## Simplification ledger (what this removes)

| Removed | Replaced by |
|---|---|
| Hand-enumerated `produced_by`/`triggers` as authoritative (hundreds of slugs × 27 events) | one small seed-concept map (view over the taxonomy) |
| Manual catalog batch-wiring (the `mana-produced` hand-wiring, ADR 0020) | derive from the taxonomy; the ramp exclusion is the rule's own consequence |
| Corroboration against a frozen incomplete list | corroboration against a self-current taxonomy view |
| The 0.6 relative keep-cut (density-sensitive engine starvation, #72) | rank-don't-cut: centrality ranks the already-substantive units |
| Two shadow flags (`SHADOW_DERIVED_ADJACENCY`, `SHADOW_RANK_DONT_CUT`) | a single authoritative default path |

## Options not taken

- **Keep enumerating but exhaustively.** Rejected: the list can never be current (new otags ship
  every set); it is the source of both the 72% under-capture and the `ramp` over-capture.
- **Seed at the umbrella (`ramp`/`triggered-ability`) for coverage.** Rejected: over-captures;
  the exclusivity rule's single job is to refuse it.
- **Derive only the election adjacency, keep `event_anchors` from the enumerated seed** (to pin
  labels byte-for-byte). Rejected: adjacency would not be uniformly authoritative; the one label
  change it would avoid (Shiko) is itself a correction, not a regression. Noted as the scoping
  option if the owner later wants label-parity over derived-everywhere.
- **N = 1 candidate promotion** (seed discovery). Rejected: admits single-card tagger noise.

## Honest residue

- **~25% of role-A edges stay text-only** and seed discovery does not rescue them — they carry no
  relevant otag (own-ETB of non-creature permanents, untagged reanimation, ETB-burn with no ETB
  otag). This is the #68 text frontier, not a seed-discovery gap.
- **`event_anchors` now expands corpus-wide** (every deck derives +16 event-anchor CardAttributes
  on average). Its only output consequence across 170 decks is the single Shiko label correction,
  but the density-rank `span` confidence signal is now mildly sensitive to the derived attribute
  count — flagged for the owner as a future confidence-tuning consideration.
- **Seed discovery (event-candidate evaluation) is a direction, not built** — the flip derives from static seeds.

## Later changes

- **2026-11-17 — impulse cheat-into-play seeds wired (#74 decision 5; ADR 0003 re-baseline).**
  Two measured, mechanic-exclusive onto-battlefield seeds were added to `SEED_MAP`
  (`derive_card_events.py`): `creature-enters-battlefield.produced_by += sneak-from-library`
  (maximal node on the from-library branch; closure adds `sneak-from-library`,
  `consult-onto-battlefield`, `impulse-onto-battlefield` — `sneak-creature` already covers the
  from-HAND creature slice) and `artifact-enters.produced_by += sneak-artifact` (artifact-specific
  from-hand onto-bf). Both score **precision 1.000 vs the onto-battlefield reference, 0 off-mechanic
  cards** — the clean "put a permanent onto the battlefield from outside the normal cast" family
  under `cost-ignorer → sneak`/`sneak-from-library`. The rejected `impulse-*` subtree (parent
  `card-advantage`, "put into HAND") is exile-and-CAST, off-mechanic (impulse-creature 0.296,
  impulse-artifact-equipment/-vehicle 0.300/0.167) and was correctly excluded; `validate_seed_map()`
  stays at 0 violations. **Live full-171 OFF/ON delta (reproduced the measurement exactly):** 166
  election-identical; **5 decks touched, all genuine cheat-into-play** — Ureni (LEAD flip
  creature-attacks→creature-enters 0.28→0.32, correct), Gishath + Zilortha (creature-enters
  0.171→0.236), Aang (creature-enters rank 4→2, 0.144→0.194, + `token-created`, no eviction), Nick
  Fury `owned__put-that-thang-down` (`creature-enters-battlefield` **0.064 NEW**, from zero graph
  events to visible). **0 evictions, 0 `named_strategy` changes, 0 `archetype_identity` changes, 0
  spurious leads.** SHAs: full-corpus `07eb6673512cc9a2…406149` → `188992d290aa98f3…708fae6`;
  engine (`7a73aba9…`) + resolve (`1e54293c…`) **UNCHANGED** (no hermetic fixture carries a touched
  commander). Full measurement:
  `~/.claude/plans/trippersham/magic-tools/2026-11-11-strategy-abstraction-consolidation/research/impulse-seed-scope.md`.
  (Note: this is the interim manual seed; the scope doc's primary recommendation is the
  concept-anchored self-healing fix that would *re-derive* exactly this family — WS3, not yet built.)

- **2026-11-17 — Tier-A catalog corrections + monarch/extra-turn split (#74 follow-on; ADR 0003
  re-baseline).** Seven SEED_MAP/catalog corrections from the event-catalog CR audit: (1)
  `creature-copied.produced_by` `clones`→`clone` (dead ref → real 77-card slug; its closure adds
  `clone-assassin`/`clone-graveyard`); (2) `card-enters-graveyard.produced_by` `sac`→`sacrifice-outlet`
  (dead ref → the real sacrifice-outlet subtree, +23 producer slugs: sacrifice-outlet-*, free-/
  repeatable-sacrifice-outlet, devour, emerge, fling/plunder/casualty — all sacrifice mechanics,
  precision clean); (3) `proliferate` `synergy-proliferate` producer→trigger (payoff, not producer);
  (4) `food-created` −`repeatable-treasures` −`eat-food`, `creature-copied.triggers` −`eat-food`
  (Treasure≠Food; eat-food is an Un-set flavor tag); (5) `game-end` += producer `alternate-win-condition`
  / trigger `lose-trigger` as SEED_MAP seeds (raw JSON lists stay `[]`, so the derived view carries the
  edges while `test_event_ontology`'s terminal-inert invariant holds); (6) `spell-cast.produced_by +=
  impulse-cast` (copy-spell over-promise documented in the rule text — copies aren't cast CR 707.12,
  but magecraft counts them); (7) `monarch-or-extra-turn` split into `extra-turn` + edge-inert `monarch`,
  `off-turn-casting-matters` dropped (ADR 0012). `validate_seed_map()` stays at **0 violations**; 28
  events. **Full OFF/ON delta (157 gauntlet + 17 fixtures):** catalog-level diff is EXACTLY the 7 fixes
  (no foreign slug in any closure); 60/157 + 5/17 decks moved; **0 engines lost** except the 3 intended
  `food-created` false-positives (Karlov/Liesa/Judith — Treasure, not Food); `sac`→`sacrifice-outlet`
  replaced edges 1:1 and expanded coverage; `clones`→`clone` dropped only redundant edges. SHAs (all
  three moved — fixtures carry the sacrifice-outlet/clone families): full-corpus `188992d2…` →
  `38dd7733…`; engine `7a73aba9…` → `1eb27096…`; resolve `1e54293c…` → `00fbb836…`. Proof + per-change
  delta table: `~/.claude/plans/trippersham/magic-tools/2026-11-17-concept-anchored-self-healing/subagents/tier-a-corrections/outbox.md`.

- **2026-11-17 — decomposition + gaps wave (#74; ADR 0003 re-baseline).** Three SEED_MAP
  producer/consumer changes: (1) **monarch decomposition** — `monarch.produced_by` seed
  `monarch-matters` → `monarch-maker` (the new query-membership synth CardAttribute, [0017]; its
  closure is itself → grade empty, like `rad-counter`), `triggers` keeps `monarch-matters`; the
  single conflated Scryfall tag is split into maker (producer) + payoff (consumer) sides so the
  node forms maker→payoff edges instead of zeroing on the `p != c_` self-pair exclusion. (2)
  **energy gap** — `player-counter.produced_by += energy-generator` (137, mechanic-exclusive
  precision 1.0) and `triggers += counter-fuel-energy` (moved from `counter-placed.triggers`,
  where no energy producer sat; kept in `proliferate.triggers`), CR 122.1a. (3) **poison** — new
  `poisoned` event ([0012]) with `produced_by` the mechanic-exclusive poison givers
  (`poisonous`/`poison-opponents`/`gives-infect`/`gives-toxic`/`gives-poisonous`; 163 cards; the
  parent `poison-mechanics` excluded for producer-role impurity) and `triggers` `synergy-poison`.
  `validate_seed_map()` stays at **0 violations**; **29** events. **Full OFF/ON delta (157
  gauntlet + 17 fixtures): exactly 1/157 decks moved** — `WakandaForever_2026` gains the monarch
  maker→payoff engine; **0 evictions, 0 foreign slugs, 0 spurious leads.** The energy + poison
  edges change 0 gauntlet decks (none run an energy/infect/toxic engine) but are proven live on
  synthetic decks. Only FULL_CORPUS moved (fixtures carry none of the touched families):
  full-corpus `38dd7733…` → `604f6673…`; engine `1eb27096…` + resolve `00fbb836…` **unchanged**.
  Proof + per-change delta:
  `~/.claude/plans/trippersham/magic-tools/2026-11-17-concept-anchored-self-healing/subagents/monarch-energy-poison/outbox.md`.

## Related records
- 0001 — one-named-rule doctrine; the Maximal-mechanic-exclusive-seed rule and rank-don't-cut are each one rule, one impl.
- 0002 / 0020 — substantiveness + the `producer_anchor` gate this adjacency feeds; `ramp` stays anchor-side-only there, never in the adjacency.
- 0003 — the byte-parity re-baseline this flip carries (the 2026-11-14 entry).
- 0008 — the `provisional → verified` store the seed-discovery direction would reuse.
- 0012 — the Event ontology; this changes how the otag→Event adjacency is *represented* (derived view), with no new field on event nodes.
- 0014 — the engine mass-election; rank-don't-cut here **supersedes** its 0.6 keep-ratio (Jaccard-0.5 dedup retained).
- 0017 — within-card edge derivation (native-then-haiku); this shrinks haiku role A to the residue.
- 0021 — Phase-A Derive is where this lives; unblocks coherence task B1.
- Issues: #72 (keep-ratio starvation — **resolved** by rank-don't-cut), #68 (DFC/text-independence direction), #71 (ramp decomposition, ADR 0020).
