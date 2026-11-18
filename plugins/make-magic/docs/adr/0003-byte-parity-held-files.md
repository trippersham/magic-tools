# 0003 Byte-parity held files: the curated strategy layer is byte-frozen, changed only by a proof-carrying re-baseline

| Field | Value |
|---|---|
| Status | Accepted. In force on branch `feat/strategy-engine`. |
| Review | ratified by the owner on 2026-11-11. (Agent-authored; owner-ratified.) |
| Test | From `plugins/make-magic/pipeline`: `uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle` records a composite SHA over the serialized `elect_engine_units` output plus the full `resolve()` records for the 17 hermetic fixture decks; a parity test imports `composite_sha` to assert no drift. The full 170-deck curated-corpus SHA is now **independently checkable** via `full_corpus_sha()` in `engine_parity_oracle.py` (canonical serialization: `json.dumps(records, sort_keys=True, separators=(',',':'), default=list)` → SHA-256; pinned as `FULL_CORPUS_SHA`). A lake-gated test in `test_resolution_fidelity.py::TestFullCorpusCanonicalSha` asserts the value. |
| Code paths | [`pipeline/.../strategy/data/attribute_catalog.json`](../../pipeline/pipeline/transforms/strategy/data/attribute_catalog.json), [`event_catalog.json`](../../pipeline/pipeline/transforms/strategy/data/event_catalog.json), [`strategy_graph.py`](../../pipeline/pipeline/transforms/strategy/strategy_graph.py), [`query.py`](../../pipeline/pipeline/transforms/strategy/query.py), [`distill_strategies.py`](../../pipeline/pipeline/transforms/strategy/distill_strategies.py), [`combo_win.py`](../../pipeline/pipeline/transforms/strategy/combo_win.py), [`data/card_edges.json`](../../pipeline/pipeline/transforms/strategy/data/card_edges.json), [`combo.py`](../../pipeline/pipeline/transforms/strategy/combo.py), [`tests/fixtures/distill_parity_baseline.json`](../../pipeline/pipeline/transforms/strategy/tests/fixtures/distill_parity_baseline.json) |

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
  (`combo_win.py`) replaces the regex win-detection path (`is_game_win_result` +
  `LETHAL_MANA_SINK_NAMES`) as the authoritative combo win-classification in the
  strategy engine. `combo.py`'s regex patterns and frozen sink list deleted;
  `combo_win.py` + `data/card_edges.json` wired as authoritative via `strategy_graph.py`.
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
  level-3 event-substantiveness gate (`determine_event` / `producer_anchor`) filtering the
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
  side's owner-approved anchors via `derive_card_events`, applied
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

- **2026-11-17 — impulse cheat-into-play seed re-baseline (#74 decision 5).** The two
  measured, mechanic-exclusive onto-battlefield seeds were wired into `SEED_MAP`
  (`derive_card_events.py`): `creature-enters-battlefield.produced_by += sneak-from-library`
  (closure adds `sneak-from-library`, `consult-onto-battlefield`, `impulse-onto-battlefield`)
  and `artifact-enters.produced_by += sneak-artifact`. Full-corpus
  `07eb6673512cc9a2…406149` → `188992d290aa98f3…708fae6`. Engine isolation **UNCHANGED**
  (`7a73aba98b9fb877…095442`) and resolve composite **UNCHANGED** (`1e54293cd148f2fd…691a59a`)
  — the 17 hermetic fixtures carry none of the touched cheat-into-play commanders, so only the
  lake-backed full corpus moved; `FULL_CORPUS_SHA` regenerated in the same change
  (`distill_parity_baseline.json` + `engine_isolation_baseline.json` byte-identical).
  **Equivalence proof (live full-171 OFF/ON, in-process `SEED_MAP` injection + assets-cache
  clear):** 166 election-identical (incl. shares); **5 decks touched, ALL genuine
  cheat-into-play** — `casual__temur__ureni-of-the-unwritten` (correct LEAD flip
  creature-attacks→creature-enters 0.28→0.32), `mid__naya__gishath-sun-s-avatar` +
  `mid__gruul__zilortha` (creature-enters sub-lead 0.171→0.236), `cedh__bant__aang-at-the-crossroads`
  (creature-enters rank 4→2, 0.144→0.194, additive + `token-created` surfaces, no eviction),
  `owned__put-that-thang-down` (Nick Fury: `creature-enters-battlefield` **0.064 NEW**, was zero
  graph events). **0 evictions, 0 `named_strategy` changes, 0 `archetype_identity` changes, 0
  spurious leads** (the one lead change is the correct Ureni improvement). Two further gauntlet
  decks (`casual__jund__henzie-toolbox-torre`, `mid__boros__winota-joiner-of-forces`) are
  election-identical (identical `ranked_units` + shares) and shift only in non-election
  structure (new producer otags become visible as nodes/community members) — benign, and part
  of why the full-corpus blob SHA moves while the election SHAs do not. Full measurement:
  `~/.claude/plans/trippersham/magic-tools/2026-11-11-strategy-abstraction-consolidation/research/impulse-seed-scope.md`.
  The seeds govern under [0020]/[0022]; the live delta reproduced the measurement exactly.

- **2026-11-17 — Tier-A event-catalog corrections + monarch/extra-turn split (#74 follow-on).**
  Seven correctness fixes landed as one proof-carrying re-baseline (`derive_card_events.py` SEED_MAP
  + `event_catalog.json` + `fid_event_adapter.json` + `combo_win.py`): (1) `creature-copied.produced_by`
  `clones`→`clone` (real Scryfall slug); (2) `card-enters-graveyard.produced_by` `sac`→`sacrifice-outlet`
  (recovers the 1,554-card sacrifice-outlet family); (3) `proliferate` `synergy-proliferate` moved
  producer→trigger (it is a payoff, not a producer); (4) `food-created` dropped `repeatable-treasures`
  + `eat-food`, and `creature-copied.triggers` dropped `eat-food` (Treasure≠Food CR 111.10; eat-food is
  an Un-set flavor tag); (5) `game-end.produced_by += alternate-win-condition` (87), `triggers +=
  lose-trigger` (12) via SEED_MAP seeds (raw JSON lists stay `[]` so the terminal invariant holds);
  (6) `spell-cast.produced_by += impulse-cast` (28); (7) `monarch-or-extra-turn` split into `extra-turn`
  (produced_by `extra-turn`, triggers `turns-taken-matter`) + `monarch` (edge-inert: one `monarch-matters`
  attr on both sides, no edge via the `p != c_` self-pair exclusion), `off-turn-casting-matters` dropped,
  fids 42/621/1183/1742 remapped to `extra-turn`, `combo_win.fid_route` branch renamed (27→28 events).
  SHAs (ALL three moved — the hermetic fixtures DO carry the sacrifice-outlet/clone families this time):
  full-corpus `188992d290aa98f3…708fae6` → `38dd7733a440fb13…b50367`; engine isolation
  `7a73aba98b9fb877…095442` → `1eb27096118d38ad…f3529b`; resolve `1e54293cd148f2fd…691a59a` →
  `00fbb8360a711583…e77063`. `FULL_CORPUS_SHA` + all engine/resolve constants (7 test files) +
  `distill_parity_baseline.json` + `engine_isolation_baseline.json` regenerated in the same change.
  **Correctness proof (full OFF/ON delta over 157 gauntlet + 17 fixtures):** 60/157 + 5/17 decks moved;
  the catalog-level diff is EXACTLY the 7 fixes (no foreign slug leaked into any closure); **0 engines
  lost** except the **3 intended** `food-created` false-positives (Karlov/Liesa/Judith are Treasure decks,
  not Food); `sac`→`sacrifice-outlet` replaced sac-node edges 1:1 at the deck level and expanded coverage;
  `clones`→`clone` removed only redundant edges (copy-creature already covers those consumers); the
  game-end/spell-cast/proliferate/extra-turn/monarch producer changes are catalog-correct with 0
  deck-edge impact on this corpus (rare co-occurrence; monarch edge-inert by design). Every changed edge
  traces to one of the 7 fixes. Full measurement + per-change delta table:
  `~/.claude/plans/trippersham/magic-tools/2026-11-17-concept-anchored-self-healing/subagents/tier-a-corrections/outbox.md`.

- **2026-11-17 — decomposition + gaps wave (#74): monarch-maker, energy, poison.** Three
  catalog changes: (1) the `monarch-maker` synth CardAttribute (`oracle:'become the monarch'`,
  62 makers; ADR 0017 query-membership, distills to None like `rad-counter`) replaces the inert
  `monarch-matters` producer on the `monarch` node (produced_by `monarch-maker`, triggers
  `monarch-matters`) so the maker→payoff split lights up; (2) the energy gap — `player-counter`
  gains producer `energy-generator` (137) and consumer `counter-fuel-energy` (moved from
  `counter-placed.triggers`, where no energy producer sat; kept in `proliferate.triggers`),
  CR 122.1a; (3) a new `poisoned` event (CR 104.3d / 704.5c; produced_by the mechanic-exclusive
  poison givers `poisonous`/`poison-opponents`/`gives-infect`/`gives-toxic`/`gives-poisonous`,
  163 cards, triggers `synergy-poison`) + its `poisoned → game-end` WIN rule-edge
  (`EVENT_EDGE_CLASSES['poisoned'] = ['altwin::poisoned']`, ADR 0016). Event count **28 → 29**.
  **Measured OFF/ON delta (157 gauntlet + 17 fixtures): only FULL_CORPUS moved;
  ENGINE + RESOLVE are UNCHANGED** (the 17 hermetic fixtures carry none of the touched families).
  `FULL_CORPUS_SHA 38dd7733a440fb13… → 604f6673e7d3d42c…`; ENGINE `1eb27096…` and RESOLVE
  `00fbb836…` constants left as-is (byte-identical; `distill_parity_baseline.json` +
  `engine_isolation_baseline.json` unchanged). **Correctness proof:** exactly **1/157** gauntlet
  decks moved — `WakandaForever_2026` gains the monarch maker→payoff engine
  (`enabler=monarch-maker, consumers=[monarch-matters], event=monarch`); **0 evictions, 0 foreign
  slugs, 0 spurious leads.** The energy + poison edges form **0 deck changes on this corpus**
  (no gauntlet deck runs an energy or infect/toxic engine) but are proven **live on synthetic
  decks** (`player-counter` enabler=`energy-generator`→`counter-fuel-energy`; `poisoned`
  enabler=`poisonous`/`gives-infect`→`synergy-poison`). The `poisoned→game-end` walk rule-edge
  is registered but walk-inert (no poison fid route today — fid 1800 → None, out of scope). Full
  per-change delta:
  `~/.claude/plans/trippersham/magic-tools/2026-11-17-concept-anchored-self-healing/subagents/monarch-energy-poison/outbox.md`.

- **2026-11-17 — ADR 0024 realization: `domain` state-event + `color-fixing` producer + free-cast.**
  Four catalog changes realizing [0024]: (1) the `color-fixing` synth CardAttribute
  (`oracle:'mana of any color' OR otag:rainbow-land`, 709 fixers, 92% produce ≥3 of WUBRG — a UNION
  inside the closed query vocabulary; the `produced_mana` FIELD form is forbidden by [0009], and
  the land-type-fetch otags are too dirty/absent to include, a documented coverage gap); (2) the
  `five-color-win` synth CardAttribute (`otag:alternate-win-condition AND oracle:'each color'` =
  Coalition Victory + Call the Spirit Dragons); (3) a new `domain` state-event (CR 104.2a;
  produced_by `color-fixing`, triggers `five-color-win`) + its `domain → game-end` WIN rule-edge
  (`EVENT_EDGE_CLASSES['domain'] = ['altwin::domain']`, ADR 0016); (4) `free-cast-another` (437)
  folded into `spell-cast.produced_by`. `life-threshold` was specced in [0024] but **dropped**
  (the existing `lifegain` event already owns `altwin::lifegain` = Felidar/Angel/Test; a second
  event would double-count). Event count **29 → 30**. **Measured OFF (code@736afe0) vs ON (157
  gauntlet + 17 fixtures): all three SHAs moved** — the fixtures DO carry color-fixing/free-cast.
  `FULL_CORPUS_SHA 604f6673e7d3d42c… → 39f1136e9c2289ef…`; `ENGINE 1eb27096… → 84b9617efcf0…`
  (3 fixtures: urza/grandlarceny/fantastic-four); `RESOLVE 00fbb836… → 5a43fc56ce9f…` (12 fixtures);
  all engine/resolve constants (7 test files) + `distill_parity_baseline.json` +
  `engine_isolation_baseline.json` regenerated. **Correctness proof:** 105/157 gauntlet decks moved,
  **0 label/identity flips, 0 spurious leads, 0 evictions**; every changed leaf traces to
  `color-fixing` (attribute surfacing + ramp-community membership + `n_nodes`+1 + tiny confidence
  drift) or `free-cast-another` (spell-cast producer, mass 116→122; +5 commander-loop closures).
  `domain` forms **0 engine-units on the corpus** — no deck runs the 2 wincon cards, and the
  consumer `five-color-win` (2 cards total, neither a legal commander) can never reach node-admission
  support≥3 — exactly as `poisoned` is walk-inert; the wiring + rule-edge are proven live
  (`five-color-win`/`color-fixing` synth membership + derived-catalog match). Full per-change delta:
  `~/.claude/plans/trippersham/magic-tools/2026-11-17-concept-anchored-self-healing/subagents/r1-realize-adr0024/outbox.md`.

- **2026-11-17 — decompose the `alt-wincon/combo` DISTILL catch-all (R1b, ADR 0023/0024).**
  One held file changed (`distill_strategies.py`): the catch-all distill identity
  `('alt-wincon/combo', 'otag:wincon OR otag:alternate-win-condition')` — whose 2nd disjunct
  was a **dead ref** (no CardAttribute named `alternate-win-condition` is ever surfaced; the
  lake slug surfaces as the attribute NAME `wincon`) — is decomposed into honest archetype
  names: a new `deckout/thoracle` identity (synth payoff `deckout-win` =
  `otag:alternate-win-condition AND otag:empty-library`, 5 cards, 0 FP) and a `domain`
  identity (the existing `five-color-win` synth payoff now carries an archetype edge). Every
  OTHER wincon RIDES its family (lifegain/poison/spellslinger) once the catch-all is gone, and
  the standalone-state tail distills to **None** (no `misc-wincon` bucket — a broad one would
  re-bundle unrelated mechanics, the conflation ADR 0023 forbids). The combo-override HEADLINE
  literal (`naming.ALT_WINCON_COMBO`) is a **separate axis** and is **untouched**. Spec
  narrowed on measurement: `otag:mill-self`/`oracle:'no cards in'` were dropped (they misroute
  Central Elevator / Barren Glory / The Cheese Stands Alone — empty-board, not deckout).
  SHAs: `ENGINE 84b9617e… UNCHANGED` (elect_engine_units never read the distill archetype);
  `RESOLVE 5a43fc56… → 50cd3873…`; `FULL_CORPUS 39f1136e… → 62abb4b1…` (both carry the
  facts-graph strategies/community archetypes). `distill_parity_baseline.json` regenerated
  (2 fixtures: Baral, Peace Offering); `engine_isolation_baseline.json` byte-identical;
  `FULL_CORPUS_SHA` + the RESOLVE constant across 7 test files updated in the same change.
  **Correctness proof (OFF/ON, 157 gauntlet + 17 fixtures):** exactly **2 gauntlet decks**
  (Azami, Baral) + **2 fixtures** (Baral, Peace Offering) moved — precisely the 4 that admit
  `wincon` as a node (≥3 wincon cards); **0 headline flips, 0 archetype_identity membership
  flips, 0 confidence-tier flips, 0 spurious leads, 0 evictions**. Every changed leaf traces to
  dropping the inert `alt-wincon/combo` strategy from those decks' identity-mass accounting
  (confidence SCORES nudged UP — the catch-all was diluting the real archetype's share — tiers
  unchanged). The new `deckout/thoracle`/`domain` identities are **engine-inert on the corpus**
  (`deckout-win` max co-occurrence 2 < node-admission 3; `five-color-win` = 2 non-commander
  cards) — honest vocabulary, no output. **Ghave did NOT re-flip** (its `alt-wincon/combo` is
  the combo-override label, untouched; byte-identical OFF→ON). Full per-change delta:
  `~/.claude/plans/trippersham/magic-tools/2026-11-17-concept-anchored-self-healing/subagents/r1b-decompose-altwincon/outbox.md`.

- **2026-11-17 — Tier-B over-inclusion narrowing (R2, ADR 0022 seeds; #74 §3.2/§3.4): 1 of 3
  shipped, 2 STOPPED.** One held file changed (`derive_card_events.py` SEED_MAP), one event
  producer seed list narrowed to drop off-mechanic cards: **life-loss-drain** drops the `burn`
  umbrella (keeps `burn-player` + `burn-player-each`) and the dead `sac` ref — CR 120.3, only damage
  to a PLAYER is life loss; the `burn` closure dragged in removal-burn/burn-battle/bombard/
  burn-creature/-planeswalker/removal-fight (1,253 cards dropped, **0 carrying any player-damage
  tag** — 100% creature/planeswalker/battle/fight damage; `drain-life`/`opponent-loses-life`/
  `burn-player`/`burn-player-each` retained, so no player-drain producer lost). The other two
  intended narrowings were **STOPPED**:
  - **permanent-dies** (replace `removal` with `removal-destroy ∪ removal-burn ∪ removal-sacrifice`)
    UNDER-COVERS genuine deaths at scale — 141 board-wipe deaths tagged `sweeper`/`sweeper-one-sided`/
    `sunder` + `removal-toughness` but NOT `removal-destroy` (The Meathook Massacre, Black Sun's
    Zenith, Night of Souls' Betrayal, Find // Finality, …) plus 669 no-method cards with only generic
    spot-removal/target-type tags. `removal`'s children mix a METHOD axis and a TARGET-TYPE axis and
    are not a clean tree (`removal-creature` itself parents destroy/burn/bounce/fight children), so no
    union of method-children cleanly separates death from non-death. `removal` KEPT; reported to the
    owner (keep + document as removal-approx-death, or a measured richer narrowing that re-adds
    sweeper/sunder and still owner-adjudicates the 669 no-method bucket).
  - **artifact-enters** (drop `animate-artifact` + `artifactify`) is output-CLEAN (136 cards, CR
    603.6a — a type-change is not an ETB; measured 0 engine evictions, 0 label flips) BUT cannot
    ship standalone: narrowing this side to 4 seeds pushes `creature-enters-battlefield`'s
    pre-existing over-broad `repeatable-token-generator` umbrella to **2/4 (50%)** coverage of the
    artifact-enters seed set, tripping `validate_seed_map` (`UMBRELLA_COVERAGE_MIN`, ADR 0022). A
    clean ship is coupled to narrowing that umbrella to `repeatable-creature-tokens` (itself correct
    — non-creature token generators don't produce creature ETBs), which is out of R2 scope. Seeds
    KEPT as-is; reported to the owner.
  **Measured OFF (`e60dbe5`) / ON delta for the SHIPPED change (170-record corpus + 17 fixtures):
  only FULL_CORPUS + RESOLVE moved; ENGINE is UNCHANGED** (`elect_engine_units` carries none of the
  touched layers). `FULL_CORPUS_SHA 62abb4b19c4eddfce… → 9c26ad8907b63e7d…`; `RESOLVE 50cd387393ba7a7a…
  → 3fbd927f92d21dbb…` (4 owned fixtures: ozai/sokka/spiders/world-reclaimer carry burn-creature);
  `ENGINE 84b9617e…` left as-is (byte-identical; `engine_isolation_baseline.json` unchanged,
  `distill_parity_baseline.json` regenerated). **Correctness proof:** 29/170 records moved; **0
  headline/archetype_identity/confidence-tier/combo/bracket/win-combo flips, 0 engine evictions**
  (engine identity-sets + per-deck counts unchanged everywhere), 0 `commander_loop.closes` flips.
  Every changed leaf traces to the burn-drop (`burn-battle` attribute surfacing off, `n_nodes`−1,
  downstream communities/edges/confidence drift). `FULL_CORPUS_SHA` + the RESOLVE constant across 7
  test files updated in the same change. Full per-narrowing delta + the permanent-dies/artifact-enters
  STOP analyses:
  `~/.claude/plans/trippersham/magic-tools/2026-11-17-concept-anchored-self-healing/subagents/r2-tier-b-narrowing/outbox.md`.

- **2026-11-17 — R2 wave 2: the two wave-1 STOPs resolved (owner-adjudicated), all three Tier-B
  narrowings now shipped.** On top of `e579682`. Two held files changed (`derive_card_events.py`
  SEED_MAP + `distill_strategies.py` SYNTH_ALWAYS_ATTRIBUTES):
  - **permanent-dies** — the raw `removal` producer seed is replaced by a new SUBTRACTIVE synth
    CardAttribute `dies-removal` = `otag:removal AND NOT removal-exile AND NOT removal-bounce AND
    NOT removal-tuck` (authored in `SYNTH_ALWAYS_ATTRIBUTES`, the rad-counter/color-fixing pattern;
    AND/NOT over otags is in the closed ADR 0009 vocabulary — no new atom). This KEEPS every
    destroy/burn/sacrifice/sweeper (the 141 sweeper/sunder deaths return) and subtracts only the
    1,200 pure zone-changes. **Measured carve:** raw `removal` 6,751 → `dies-removal` 5,551;
    carved-out = exactly the exile(617)/bounce(448)/tuck(151) cards (0 "neither"). Residual
    imperfection = disable-auras on a bare `removal` tag (Pacifism-likes): ~84 pacifism = 1.5% of
    the set (owner STOP threshold was >~10% — passes), and these were in the raw `removal` seed
    before (the carve only REMOVES). Known conservative under-coverage: ~182 MODAL cards with a
    death method AND an exile/bounce/tuck mode (Bant Charm, Chandra Awakened Inferno) are excluded
    by the NOT. The holistic `removal` otag is UNTOUCHED in permanent-leaves-battlefield.
  - **artifact-enters** — DROP `animate-artifact` + `artifactify` (CR 603.6a type-change ≠ ETB; 136
    cards).
  - **creature-enters-battlefield (coupled fix)** — seed `repeatable-token-generator` →
    `repeatable-creature-tokens`. The umbrella's non-creature-token children (446 cards:
    artifact/enchantment/land/noncreature token generators, **0 of which make a creature token**)
    were false creature-ETB edges; this also clears the `validate_seed_map` cross-mechanic flag the
    artifact-enters narrowing surfaced in wave 1.
  **Measured OFF (`e579682`) / ON (170-record corpus + 17 fixtures): all three SHAs moved**
  (fixtures carry the touched families). `FULL_CORPUS_SHA 9c26ad8907b63e7d… → 62d5fa5c87e2c962…`;
  `ENGINE 84b9617efcf0ec74… → 9559fa42ea76f661…`; `RESOLVE 3fbd927f92d21dbb… → b88d8bb5d3a32414…`;
  all engine/resolve constants (7 test files) + `distill_parity_baseline.json` +
  `engine_isolation_baseline.json` regenerated. **Correctness proof:** 153/170 records moved
  (`dies-removal` is a new corpus-wide producer attribute surfacing on 146 decks — the same
  attribute-surfacing shape as `color-fixing` in the ADR 0024 wave), but **0 headline/
  archetype_identity/confidence-tier/combo/bracket/win-combo flips, 0 engine-set evictions** (every
  deck keeps its engine identity-set), **1 intended loop correction** (Sydri Galvanic Genius
  `commander_loop.closes` True→False — her `artifactify` is a type-change, not an ETB). Every
  changed leaf traces to one of the three edits; membership-verified (carve removes only
  exile/bounce/tuck; creature-enters trim removes only non-creature-token generators, 0 creature
  producers). Full per-change delta:
  `~/.claude/plans/trippersham/magic-tools/2026-11-17-concept-anchored-self-healing/subagents/r2-tier-b-narrowing/outbox.md`.
- **2026-11-17 — recursion-from-graveyard restructure (R3, Reading B; audit §3.6 / #19).** The
  event was role-inverted: `produced_by` = the graveyard-FILLERS (discard/mill/sac) and `triggers` =
  the reanimate/recursion family — i.e. it re-modeled "graveyard stocked → recursion consumes",
  which IS `card-enters-graveyard`'s semantics (same filler producers, same reanimate-family
  consumers): a role-inverted near-duplicate. Reading B restores the CR-literal orientation ("a card
  RETURNS from the graveyard"):
  - `produced_by` → the `recursion` ROOT (2,370 cards / 100-slug closure; every reanimate-*/
    recursion-* variant + regrowth/restock/copy-from-graveyard/…). It is mechanic-exclusive against
    the returner mechanic (precision 1.0, no foreign heavy child) and the HIGHEST such anchor — a
    taxonomy root with nothing to climb to. grade `S`.
  - `triggers` → the recursion PAYOFFS `leaving-graveyard-matters` (59) + `trigger-from-graveyard`
    (147) — the cards that care about something leaving / acting from the graveyard. grade `M`.
  - the gy-FILLERS move OUT: they own `card-enters-graveyard.produced_by` (discard/mill/sacrifice),
    where the filler→reanimate "graveyard engine" still lives (the reanimate family is a consumer
    THERE). Relocation, not loss. The opponent-scoped `discard` otag (opponent-hand attack; §3.3) and
    the dead `sac` ref are dropped with them.
  **Measured OFF (`31f5c7c`) / ON (157 gauntlet + 17 fixtures): all three SHAs moved** (fixtures
  carry the recursion/reanimate families). `FULL_CORPUS 62d5fa5c87e2c962… → cb1a103be7614116…`;
  `ENGINE 9559fa42ea76f661… → 4549062831f5bfc9…`; `RESOLVE b88d8bb5d3a32414… → f209237b352ebf62…`;
  all engine/resolve constants (7 test files) + `distill_parity_baseline.json` +
  `engine_isolation_baseline.json` regenerated. **Correctness proof:** 85/157 records moved, but **0
  headline/pre_combo_name/archetype_identity/ranked_identity/confidence-tier/combo/bracket/win-combo/
  tribe flips, 0 determinism flips, 0 reanimator engine losses.** `recursion-from-graveyard` stops
  electing as a redundant graveyard engine on 23 decks (each already had `card-enters-graveyard`, or
  RELOCATES to it — 8 decks incl. the graveyard/reanimator Marchesa-Dealer-of-Death + Dihada gain
  `card-enters-graveyard` as rfg drops) and now elects ONLY where a genuine returns→payoff pair
  exists (braids, xu-ifit: producers = recursion/reanimate, consumers = leaving-graveyard-matters).
  **8 `commander_loop.closes` True→False** (Karador/Nethroi/Sheoldred/Mimeoplasm/Sharuum/Hashaton/
  Kess/Feather) = the correct removal of the SPURIOUS self-loop the role-inversion created (OFF,
  `recursion` was a PRODUCER and the reanimate family CONSUMERS of the SAME event, so any commander
  carrying a recursion attr trivially closed a loop); none change `determinism` (all keep their
  `mass-elected` engines). `validate_seed_map()` 0 violations; 30 events (count unchanged). Full
  per-change delta:
  `~/.claude/plans/trippersham/magic-tools/2026-11-17-concept-anchored-self-healing/subagents/r3-recursion-restructure/outbox.md`.

## Related records
- 0001 — the knowledge-placement doctrine these files exemplify.
- 0002 — the substantiveness model (`strategy_graph.py` is shared with this record).
- 0005 — the tribe-catalog fix in the re-baseline above.
- 0016 — the closure-mechanism win-walk that triggered an earlier re-baseline.
- 0022 — the derived-adjacency + rank-don't-cut flip of the 2026-11-14 re-baseline above.
- 0014 — the mass-election flip; its `lead` removal re-baselined in the 2026-11-14 entry.
- 0020 — the event-substantiveness gate + corrected mana-wiring behind the 2026-11-14 re-baseline.
