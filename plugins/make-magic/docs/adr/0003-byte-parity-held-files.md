# 0003 Byte-parity held files: the curated strategy layer is byte-frozen, changed only by a proof-carrying re-baseline

| Field | Value |
|---|---|
| Status | Accepted. In force on branch `feat/strategy-engine`. |
| Review | ratified by the owner on 2026-11-11. (Agent-authored; owner-ratified.) |
| Test | From `plugins/make-magic/pipeline`: `uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle` records a composite SHA over the serialized `elect_engine_units` output plus the full `resolve()` records for the 17 hermetic fixture decks; a parity test imports `composite_sha` to assert no drift. The full 170-deck curated-corpus SHA is now **independently checkable** via `full_corpus_sha()` in `engine_parity_oracle.py` (canonical serialization: `json.dumps(records, sort_keys=True, separators=(',',':'), default=list)` → SHA-256; pinned as `FULL_CORPUS_SHA`). A lake-gated test in `test_resolution_fidelity.py::TestFullCorpusCanonicalSha` asserts the value. |
| Code paths | [`pipeline/.../strategy/data/attribute_catalog.json`](../../pipeline/pipeline/transforms/strategy/data/attribute_catalog.json), [`event_catalog.json`](../../pipeline/pipeline/transforms/strategy/data/event_catalog.json), [`strategy_graph.py`](../../pipeline/pipeline/transforms/strategy/strategy_graph.py), [`query.py`](../../pipeline/pipeline/transforms/strategy/query.py), [`distill_strategies.py`](../../pipeline/pipeline/transforms/strategy/distill_strategies.py), [`win_walk.py`](../../pipeline/pipeline/transforms/strategy/win_walk.py), [`data/card_edges.json`](../../pipeline/pipeline/transforms/strategy/data/card_edges.json), [`combo.py`](../../pipeline/pipeline/transforms/strategy/combo.py), [`tests/fixtures/distill_parity_baseline.json`](../../pipeline/pipeline/transforms/strategy/tests/fixtures/distill_parity_baseline.json) |

## Context
The higher-order engine-identification work (machine-authored attributes, within-card
edges, the relay, engine reports) was built as a layer *on top of* a validated curated
base. The forcing constraint: that work had to be provably **additive** — an empty
machine store must leave the committed engine's output byte-for-byte identical, so a
regression in the curated base could never hide inside the new layer.

## Decision
Six curated files are **byte-frozen** on `feat/strategy-engine`: `attribute_catalog.json`,
`event_catalog.json`, `strategy_graph.py`, `query.py`, `distill_strategies.py`, and the parity fixture
`distill_parity_baseline.json`. The oracle (`tests/engine_parity_oracle.py`) records a
composite SHA over `elect_engine_units` + `resolve()` for the 17 hermetic fixture decks;
the current baselines live in `engine_isolation_baseline.json` and
`distill_parity_baseline.json` and are **not restated here** (they change on every
re-baseline). A change to any held file is permitted **only** as a deliberate
re-baseline that (a) carries a behavioral-equivalence proof for every output that
shifts and (b) regenerates the baselines in the same change. A silent byte change is a
regression, not a change.

## Reasons
An unexplained SHA shift is indistinguishable from a regression across 170 decks, which
review cannot catch by eye. Freezing the base + a composite-SHA oracle turns "did this
refactor change any output?" into a deterministic check, and the proof-carrying
re-baseline makes every *intended* change legible as such.

## Options not taken
- **Freeze nothing, rely on review.** Rejected: a subtle output shift across the corpus
  is not reviewable by inspection.
- **Freeze permanently (never re-baseline).** Rejected: the consolidation deliberately
  improves these files; the discipline is *proof-carrying change*, not immutability.

## Consequences
- The consolidation re-baselines `strategy_graph.py` / `distill_strategies.py` / the catalog (one change) and
  `query.py` (a separate change), each with equivalence proofs (e.g. 613=613). Those are
  the sanctioned re-baselines this record governs.
- The hermetic oracle is 17 decks; the full-corpus parity (170 decks) needs lake access.
  This gap is now covered by `full_corpus_sha()` + `FULL_CORPUS_SHA` in
  `engine_parity_oracle.py`, with a lake-gated test asserting the recorded value.

## Later changes
- **2026-11-11 — first sanctioned re-baseline.** Two output-changing bug fixes shifted the
  resolve + full-corpus baselines (the engine_isolation composite stayed `fcf27cf6…`
  unchanged): **A7** (enchantment surfacing switched from a 4.6×-over-matching regex to the
  16 exact vocab otag slugs) and **T1** (tribe resolution via the Scryfall creature-type
  catalog, [0005]). Resolve composite `f8e1060b…68be0` → `60858cfce883db1a…385fea`;
  full-corpus `8b0125ee…523554` → `2f32fc0599…fc8849`. **Improvement proof** (not
  equivalence): A7 removed 162 false enchantment-slug-instances across 11 confirmed-false
  families with **0** `named_strategy`/identity/confidence-tier/density changes; T1 dropped a
  spurious `Forest` tribe on exactly 2/170 decks with **0** classification changes. Both are
  pure false-positive removal, reviewed per the owner's "zero-doubt-strictly-better" rule.

- **2026-11-13 — resolution-fidelity re-baseline (#68 + #69).** Two data-layer bugs
  degraded card resolution; **#69** (art-series first-row-wins) was fixed in code
  + data, **#68** (DFC `oracle_text` drop in bulk ingest) was fixed in code only
  (cached raw parquet lacks `card_faces` — data fix is BLOCKED until next network
  re-ingest). All three baselines shifted from the #69 fix:
  engine `fcf27cf6…` → `ba35052c…`; resolve `60858cfc…` → `760120d2…`;
  full-corpus `2f32fc05…` → `c25f1a5f…`.
  **Improvement proof:** 1,129 names changed from art-series (`Card // Card`, 0 tags)
  to the canonical printing (real type, real tags); **0** regressions in the name→oid
  resolution. 167/170 corpus decks shifted; 10 strategy labels changed (all from
  corrupt-data misclassification → correct-data classification); **0** decks lost
  cmd_attrs or tags. 19 unique gauntlet commanders previously resolving to tagless
  art-series oracle_ids now resolve correctly (Baral 20 tags, Yawgmoth 36, Urza 26,
  …). Fixture decks regenerated from the fixed Lake; `distill_parity_baseline.json`
  and `engine_isolation_baseline.json` updated in the same change.

- **2026-11-13 — #68 DFC data fix (re-ingest).** The #68 code fix
  (`scryfall_bulk._dfc_oracle_text` + `_project` reading `card_faces`) was already in
  place but the cached `raw/oracle_cards.parquet` predated it (Oct 12 cursor). A
  `hydrate-lake --force` re-fetched the full Scryfall oracle bulk (Nov 13,
  `RATE_LIMIT_MS=100` throttled, ~39k cards) so DFC oracle_text is now populated from
  `card_faces`. Combined with fixture regeneration from the refreshed lake (which also
  re-applies the #69 art-series resolution that was previously only in uncommitted fixture
  state). All three baselines shifted:
  engine `ba35052c…` → `a6dfa607…`; resolve `760120d2…` → `7d35fe19…`;
  full-corpus `c25f1a5f…` → `88de96a7…`.
  **Monotonicity proof:** 168 cards gained oracle_text (empty→populated); 1,786 otag
  gains; 3 otag losses (Scryfall community corrections: `activated-ability` removed from
  Urza's Saga ×2 decks, `utility-land` removed from Urborg ×1 — both correct tag
  housekeeping, not regressions); 152 art-series type_line fixes (`Card // Card` →
  real type); **0** ambiguous regressions. 13/17 fixture resolve SHAs shifted, 5/17
  engine-isolation SHAs shifted. All golden-label tests pass (Urza→`artifacts`,
  Baral→`control/permission` MED, Peace Offering→`group-hug` — all expected by #69 test
  code). Ral, Monsoon Mage (#68 gut-check) confirmed fixed: was NULL oracle_text, now
  carries real spellslinger text. 3 new DFC lake tests added
  (`test_resolution_fidelity.py::TestDfcOracleTextInLake`).

- **2026-11-13 — canonical full-corpus hash.** The full-corpus SHA was previously
  recorded as `88de96a7` by an ad-hoc method whose serialization was not committed
  and could not be reproduced by a second party. A canonical, committed helper
  `full_corpus_sha()` in `engine_parity_oracle.py` now defines the method: compact
  JSON (`sort_keys=True, separators=(',',':'), default=list`) → SHA-256 over the
  170-record gauntlet corpus from `recalc_corpus()`. The canonical value is
  `de5313af330f802f9381e3d587cb54d3a87a637dcbb368db28f47091f2aa468f`, pinned as
  `FULL_CORPUS_SHA` and asserted by a lake-gated test. The hermetic engine
  (`a6dfa607`) and resolve (`7d35fe19`) baselines are unchanged. The old `88de96a7`
  is **superseded** — same 170 records, same lake state, just a defined hash.

- **2026-11-13 — closure-mechanism win-walk flip (ADR 0016).** The closure walk
  (`win_walk.py`) replaces the regex win-detection path (`is_game_win_result` +
  `LETHAL_MANA_SINK_NAMES`) as the authoritative combo win-classification in the
  strategy engine. `combo.py`'s regex patterns and frozen sink list deleted;
  `win_walk.py` + `data/card_edges.json` wired as authoritative via `strategy_graph.py`.
  Engine isolation SHA **unchanged** (`a6dfa607…`). Resolve composite
  `7d35fe19…` → `d409069e…`; full-corpus `de5313af…` → `0901bc62…`.
  4/17 hermetic fixture decks shifted (Kess, K'rrik, Urza, Ozai).
  **Parity-gate proof:** 253 walk promotions (new wins the regex missed), 5
  regex false-positive drops, 25 decks flipping from zero win-combos, 0 WRONG —
  exact match to the validated 5b-i shadow delta (blind-recall 100% adjusted).
  `distill_parity_baseline.json`, `engine_isolation_baseline.json`, and
  `FULL_CORPUS_SHA` updated in the same change.

- **2026-11-13 — mass-election flip (ADR 0014, "2C flip").** Node-weighted
  event-neighborhood **mass** is now the authoritative engine-lead signal in
  `strategy_graph.py::elect_engine_units`, replacing the former recursive-first ordering (the
  `kind != 'recursive-engine'` primary sort key + `next(recursive)` lead). The lead
  is the top-mass unit; a new `elected` field reports the ranked 0..n Engine set
  (0.6 keep-ratio + member-Jaccard-0.5 dedup); `determinism` gains `'mass-elected'`.
  Engine isolation `a6dfa607…` → `59fe5d33…`; resolve composite
  `d409069e…` → `82a71840…`; full-corpus `0901bc62…` → `d01d8701…`.
  5/17 hermetic fixture engine-isolation SHAs shifted; 13/17 resolve SHAs shifted
  (the embedded `engine_isolation` sub-dict drives the resolve shift).
  **0-regression proof (full 170-deck gauntlet):** 131/170 leads changed — 108
  none→lead (committed had no recursive lead), 23 recursive-flips (each to a
  strictly-higher-or-equal-mass unit; identical to the GATE-0 shadow's 23). **0**
  `named_strategy` labels changed (STRUCTURE-ONLY invariant intact — naming never
  reads the election). Mass-monotonicity verified: no lead dropped to None, no
  lower-mass unit displaced a higher-mass one. E3 fixes reproduced (Karador→gy,
  Aesi→lands, Kykar/Prosper→tokens, Urza None→artifact-enters m=47, Muldrotha
  unchanged). `distill_parity_baseline.json`, `engine_isolation_baseline.json`, and
  `FULL_CORPUS_SHA` updated in the same change. Anchor-mode distribution is
  all-bipartite (mode 1) at this step; terminal-producer (mode 2) and
  exogenous-environmental (mode 3) anchors populate once producer-only / consumer-only
  edges exist (later execution-sequence steps) — see ADR 0014.

- **2026-11-14 — step-2 landing: corrected mana-wiring + `producer_anchor` gate + `lead`
  removal (ADR 0020, ADR 0014 amendment).** Three coupled changes in one re-baseline:
  `mana-produced.produced_by` set to the taxonomy-derived, closure-deduplicated
  `[adds-multiple-mana, mana-producer, repeatable-treasures, ritual]` (dropping the umbrella
  `ramp` and the `mana-producer`-redundant `mana-dork`/`mana-rock` children, #71); a new
  level-3 event-substantiveness gate (`event_admitted` / `producer_anchor`) filtering the
  election candidate pool; and the `lead_engine` field dropped (output = the `elected` set
  only — an election-neutral field drop).
  Engine isolation `59fe5d33…` → `987f2660…`; resolve composite `82a71840…` → `85752bb9…`;
  full-corpus `d01d8701…` → `7e5190e8…`. All 17 hermetic fixture resolve SHAs shift; the
  170-record full corpus shifts.
  **Behavioral-equivalence + improvement proof.** (a) *Full-170 election* vs `7b53e6a`:
  exactly **4 elected-set changes** (Selvala, Marwyn, Helga, Derevi gain `mana-produced` — the
  4 genuine command-zone-anchored mana engines), **0 evictions**, every other deck's elected
  set byte-identical to the pre-wiring election; **1 `named_strategy` change** — Ghave
  (aristocrats/sacrifice → alt-wincon/combo), from the CONSUMER-side wiring
  (`bottomless-mana-sink` on Ghave's commander), owner-flagged as defensible. Omnath
  (Locus of Mana) and Urza are **unchanged vs `7b53e6a`** (mana correctly not a candidate —
  keep-ratio starvation, #72 — NOT a regression). (b) *Fixtures:* the SHA shift is the
  additive, reported `mana-produced` unit now present in the `engines` list wherever mana
  producers/consumers exist; **16/17 fixtures keep a byte-identical ELECTED set** (only the
  reported unit shifts bytes), and the one election change — `owned__ozai` gains
  `mana-produced` — is a correct `producer_anchor` admit (its commander carries
  `adds-multiple-mana` + `mana-producer` + `ramp`; identical mechanism to Selvala/Marwyn).
  `distill_parity_baseline.json`, `engine_isolation_baseline.json`, and `FULL_CORPUS_SHA`
  updated in the same change. Measurement: the real pipeline (`compute_facts` + `name_deck`)
  over the 170-deck gauntlet, reproducing the step-2 research docs
  (`mana-produced-correct-wiring.md` §3/§4, `event-substantiveness-empirical.md` §5) exactly.

- **2026-11-14 — derived-adjacency + rank-don't-cut flip (ADR 0022, task 6.5).** Two
  coupled, output-moving changes made authoritative in one re-baseline:
  **(1) adjacency is DERIVED at catalog load** — the hand-enumerated
  `produced_by`/`triggers` lists are replaced by the descendant-closure of each event
  side's owner-approved anchors via `anchor_derivation.derive_event_adjacency`, applied
  in `assets.load_assets` and `engine_hydration._load_events` (the `SHADOW_DERIVED_ADJACENCY`
  flag retired); **(2) rank-don't-cut** — `elect_engine_units` drops the 0.6·top-mass relative
  keep-cut, keeping every substantive unit ranked by mass with only the Jaccard<0.5 dedup
  (the `SHADOW_RANK_DONT_CUT` flag retired, #72 resolved).
  Engine isolation `987f2660…` → `a7edab20…`; resolve composite `85752bb9…` → `25f50a23…`;
  full-corpus `7e5190e8…` → `5ce447ef…` (reproduced from the live lake in two independent
  fresh-process runs). All three regenerated in the same change
  (`engine_isolation_baseline.json`, `distill_parity_baseline.json`, `FULL_CORPUS_SHA`).
  **0-genuine-regression proof (full 170-deck gauntlet vs `3ab4034`):** 147 decks change
  election, classified — **142 improvements** (net engine gains), **5 baseline-ramp-corrections**
  (land-enters-battlefield de-elected in non-landfall decks where the committed enumerated catalog
  over-counted via the generic `ramp` umbrella — the flip's ramp exclusion corrects the committed
  bug, not a regression), **23 neutral**, **0 Jaccard-dedup-only**, and **0 GENUINE regressions**
  (no previously-correct election broken; every removal is ramp-correction). No election lost an
  engine except the 5 ramp over-counts. Elected-set sizes bounded (mean 3.95, max **10** — Betor /
  The Ur-Dragon, both legitimately multi-axis 5-color decks; clean tail 10:2 9:2 8:4). **1
  `named_strategy` change** — Shiko, Paragon of the Way (`blink/flicker` → `alt-wincon/combo`):
  the deck genuinely runs the Dualcaster Mage + Ghostly Flicker infinite combo (7 win-combos
  detected identically on BOTH committed and flipped) with a non-dominant primary (identity share
  0.364 < 0.50), the textbook combo-naming-override case (same accepted pattern as Ghave/Ozai in
  the step-2 re-baseline above). The mechanism is a side effect of making adjacency authoritative at
  load: the event-anchor attribute universe (`assets.event_anchors`) expands with the derived lists,
  so every deck derives more event-anchor CardAttributes (corpus-wide node count +16 mean), which
  deflated Shiko's density-rank `span` confidence signal MED→LOW and thus un-blocked the override.
  Corpus confidence distribution is otherwise stable (MED 88→87, HIGH 41→40, LOW 40→42; 5 tier
  shifts total, net-neutral). Anchor-map validator clean (0 violations); `ramp` correctly absent
  from the effect-otag universe (`land-ramp` present). Suite green (545 passed). Measurement: the
  real pipeline (`recalc_corpus` → `resolve` → `elect_engine_units` + `name_deck`) over the full
  170 `.dck` gauntlet; full classified delta in
  `research/flip-170-gauntlet-delta.md`.

- **2026-11-16 — generic demotion re-baseline (behavior-equivalent).** The hardcoded
  `'graveyard/reanimator'` demotion branch in `strategy_graph.py` and the `'demotions'` naming key
  in `naming.py` were replaced with a generic `demoted_archetypes: list[str]` derived from
  `earned_archetypes` + `Requirement.satisfied()`. Engine isolation **unchanged**
  (`afcffc8a...`); resolve composite `3010be2a...` -> `5c4f9c05...`; full-corpus
  `4acf0f41...` -> `c0d979ea...`.
  **Equivalence proof:** 571 semantic tests pass (labels/elections/engines byte-identical);
  the ONLY field that shifted is the diagnostic `demoted_archetypes` (consumed by no code),
  reshaped from a boolean `gy_demoted` to a list. The generic set correctly reports 6
  demoted archetypes corpus-wide (graveyard x52 + mill/theft/enchantress/vehicles/
  -1-1-counters x1 each) where the old hardcoded code tracked only graveyard — a latent
  incompleteness, now correct. `distill_parity_baseline.json`, `engine_isolation_baseline.json`,
  and `FULL_CORPUS_SHA` updated in the same change.

- **2026-11-16 — emitted-key naming-clarity re-baseline (pure key renames + one field
  removal).** The OUTPUT-AFFECTING half of the naming-clarity pass renamed emitted schema
  keys and removed the redundant derivable `named_strategy` field (see [0014] for the
  field-rename rationale). Engine isolation `afcffc8a…` → `7a73aba98b9fb877…095442`;
  resolve composite `5c4f9c05…` → `1e54293cd148f2fd…691a59a`; full-corpus `c0d979ea…` →
  `07eb6673512cc9a2…406149` (reproduced from the live lake via `full_corpus_sha()`). All
  three baselines regenerated in the same change (`engine_isolation_baseline.json`,
  `distill_parity_baseline.json`, `FULL_CORPUS_SHA`).
  **Renames** (pure key substitutions, values byte-identical): engine-unit `weight`→`mass`,
  `hub`→`interior_node` (in both `engine_units['engines']` and `engine_units['ranked_units']`);
  ranked-unit `anchor`→`formation_mode`, `thin`→`marginal`; `engine_units['elected']`→
  `['ranked_units']`; `facts['engines']['engine_isolation']`→`['engine_units']`; top-level
  `combo_override_f4_blocked`→`combo_override_blocked`. The emitted `note` string value inside
  `engine_units` retains the word "elected" by design (it is a VALUE, not a key — changing it
  would break byte-value parity). **Removed:** the top-level `named_strategy` field, now
  derived on demand by `naming.headline_strategy(rec)` (= combo-override label when applied,
  else `archetype_identity[0]['strategy']`, else `None`).
  **Equivalence proof (ZERO value changes):** a canonical transform applied the key-rename
  map + dropped `named_strategy` on the pre-change snapshot of all **187 records** (170
  lake-backed full-corpus + 17 hermetic fixtures) and deep-compared to the post-change
  output — **187/187 identical**, the ONLY differences being the renamed keys and the removed
  field, no value altered. **Lossless-removal proof:** for all 170 corpus decks (and 17
  fixtures) the old `named_strategy` == the reconstruction (`headline_strategy`) — **170/170
  + 17/17, 0 mismatches**, so the field carried no information not derivable from
  `archetype_identity` + `combo_override_applied`. Suite green (580 passed, 4 skipped);
  offline clean (477 passed, 107 skipped, 0 errors). (Requirement-kind rename
  `producer_anchor`→`produced_by_commander`, [0020], is internal dispatch data — NOT emitted —
  so it did not move any SHA.)

## Related records
- 0001 — the knowledge-placement doctrine these files exemplify.
- 0002 — the substantiveness model (`strategy_graph.py` is shared with this record).
- 0005 — the tribe-catalog fix in the re-baseline above.
- 0016 — the closure-mechanism win-walk that triggered an earlier re-baseline.
- 0022 — the derived-adjacency + rank-don't-cut flip of the 2026-11-14 re-baseline above.
- 0014 — the mass-election flip; its `lead` removal re-baselined in the 2026-11-14 entry.
- 0020 — the event-substantiveness gate + corrected mana-wiring behind the 2026-11-14 re-baseline.
