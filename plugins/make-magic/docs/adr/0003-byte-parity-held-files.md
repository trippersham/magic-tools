# 0003 Byte-parity held files: the curated strategy layer is byte-frozen, changed only by a proof-carrying re-baseline

| Field | Value |
|---|---|
| Status | Accepted. In force on branch `feat/strategy-engine`. |
| Review | ratified by the owner on 2026-11-11. (Agent-authored; owner-ratified.) |
| Test | From `plugins/make-magic/pipeline`: `uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle` records a composite SHA over the serialized `_isolate_engines` output plus the full `resolve()` records for the 17 hermetic fixture decks; a parity test imports `composite_sha` to assert no drift. The full 170-deck curated-corpus SHA is now **independently checkable** via `full_corpus_sha()` in `engine_parity_oracle.py` (canonical serialization: `json.dumps(records, sort_keys=True, separators=(',',':'), default=list)` → SHA-256; pinned as `FULL_CORPUS_SHA`). A lake-gated test in `test_resolution_fidelity.py::TestFullCorpusCanonicalSha` asserts the value. |
| Code paths | [`pipeline/.../strategy/data/attribute_vocab_v4.json`](../../pipeline/pipeline/transforms/strategy/data/attribute_vocab_v4.json), [`event_catalog_v4.json`](../../pipeline/pipeline/transforms/strategy/data/event_catalog_v4.json), [`facts.py`](../../pipeline/pipeline/transforms/strategy/facts.py), [`query.py`](../../pipeline/pipeline/transforms/strategy/query.py), [`vocab.py`](../../pipeline/pipeline/transforms/strategy/vocab.py), [`win_walk.py`](../../pipeline/pipeline/transforms/strategy/win_walk.py), [`data/card_edges.json`](../../pipeline/pipeline/transforms/strategy/data/card_edges.json), [`combo.py`](../../pipeline/pipeline/transforms/strategy/combo.py), [`tests/fixtures/vocab_parity_baseline.json`](../../pipeline/pipeline/transforms/strategy/tests/fixtures/vocab_parity_baseline.json) |

## Context
The higher-order engine-identification work (machine-authored attributes, within-card
edges, the relay, engine reports) was built as a layer *on top of* a validated curated
base. The forcing constraint: that work had to be provably **additive** — an empty
machine store must leave the committed engine's output byte-for-byte identical, so a
regression in the curated base could never hide inside the new layer.

## Decision
Six curated files are **byte-frozen** on `feat/strategy-engine`: `attribute_vocab_v4.json`,
`event_catalog_v4.json`, `facts.py`, `query.py`, `vocab.py`, and the parity fixture
`vocab_parity_baseline.json`. The oracle (`tests/engine_parity_oracle.py`) records a
composite SHA over `_isolate_engines` + `resolve()` for the 17 hermetic fixture decks;
the current baselines live in `engine_isolation_baseline.json` and
`vocab_parity_baseline.json` and are **not restated here** (they change on every
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
- The consolidation re-baselines `facts.py` / `vocab.py` / the catalog (one change) and
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
  …). Fixture decks regenerated from the fixed Lake; `vocab_parity_baseline.json`
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
  `win_walk.py` + `data/card_edges.json` wired as authoritative via `facts.py`.
  Engine isolation SHA **unchanged** (`a6dfa607…`). Resolve composite
  `7d35fe19…` → `d409069e…`; full-corpus `de5313af…` → `0901bc62…`.
  4/17 hermetic fixture decks shifted (Kess, K'rrik, Urza, Ozai).
  **Parity-gate proof:** 253 walk promotions (new wins the regex missed), 5
  regex false-positive drops, 25 decks flipping from zero win-combos, 0 WRONG —
  exact match to the validated 5b-i shadow delta (blind-recall 100% adjusted).
  `vocab_parity_baseline.json`, `engine_isolation_baseline.json`, and
  `FULL_CORPUS_SHA` updated in the same change.

- **2026-11-13 — mass-election flip (ADR 0014, "2C flip").** Node-weighted
  event-neighborhood **mass** is now the authoritative engine-lead signal in
  `facts.py::_isolate_engines`, replacing the former recursive-first ordering (the
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
  unchanged). `vocab_parity_baseline.json`, `engine_isolation_baseline.json`, and
  `FULL_CORPUS_SHA` updated in the same change. Anchor-mode distribution is
  all-bipartite (mode 1) at this step; terminal-producer (mode 2) and
  exogenous-environmental (mode 3) anchors populate once producer-only / consumer-only
  edges exist (later execution-sequence steps) — see ADR 0014.

## Related records
- 0001 — the knowledge-placement doctrine these files exemplify.
- 0002 — the substantiveness model (`facts.py` is shared with this record).
- 0005 — the tribe-catalog fix in the re-baseline above.
- 0016 — the closure-mechanism win-walk that triggered the latest re-baseline.
- 0014 — the mass-election flip that triggered the latest re-baseline.
