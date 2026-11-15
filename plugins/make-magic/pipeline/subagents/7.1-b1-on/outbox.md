# 7.1-b1-on — outbox

**Task:** turn ON the B1 combo-edge machinery in make-magic's production pipeline;
measure full-170 delta; confirm 0 genuine regressions; re-baseline. NO commit.
HEAD `46dce39`. Full report: `research/b1-on-170-delta.md` (+ raw per-deck records
`research/b1-on-170-delta.raw.json`).

## Integration approach — Option A (ship-materialized, #65)

Derived data in this pipeline is **materialized**, not runtime-derived: `build.run()`
→ `driver.build_all()` overwrites the `card_otag`/`combo` parquet marts in the XDG
data dir; `collection hydrate-lake` wraps it. The combo-edge store is the same kind
of asset one tier down (a deterministic function of those two marts), and
`engine_report.identify_engines` already loads it via `load_store(None)`. So I wired
`populate_combo_edges` into the mart build:

- **Code (only in-repo change):** `pipeline/transforms/build.py` — new
  `_materialize_combo_edges()` runs after `driver.build_all()`, added to `run()`'s
  result map as `combo_edges`. It is a **reproducible clean rebuild** of the
  spellbook layer (drops existing spellbook records, re-derives all combos from the
  current lake, preserves future non-spellbook/haiku records) — matching the marts'
  overwrite semantics. Required: `derivation_version` only guards logic changes, so
  a plain append silently keeps stale combos from an older lake.
- **Materialized store:** `~/.local/share/make-magic/machine_vocab/attributes.jsonl`
  = 57 362 spellbook edges / 1 272 edge-producing combos. Idempotent (re-run
  57 362→57 362). Production path confirmed firing: `identify_engines(ayara)`
  default-store lead = `creature-enters-battlefield`; `urza` = `draw-a-card`.

Why not Option B (runtime lazy derive): it would be the only runtime-derived asset
in a materialize-everything pipeline; inconsistent.

## Decks changed + classification (the gate)

**63/170 changed (107 byte-identical). 43 IMPROVEMENT, 20 NEUTRAL, 0 REGRESSION.**

- IMPROVEMENT (14) engine added, prior lead unchanged.
- IMPROVEMENT (29) combo-implied recursive engine out-ranks prior lead, **prior
  lead stays elected at unchanged mass** (rank-don't-cut).
- NEUTRAL (3) prior lead event deduped into a same-family Jaccard-sibling (engine
  retained): wilhelt (permanent-dies→permanent-sacrificed), jeleva
  (recursion-from-graveyard→card-enters-graveyard), rocco (token-created→CEB).
- NEUTRAL (17) same lead; mass re-rank + more coverage.

Aggregate: dark cards −2 065 (63 improve, **0 worsen**); set size max 10→11, mean
3.95→4.30 (bounded); **68 added units all ≥5% share, 0 junk (<5%)** → #76 NOT
triggered; **0 `named_strategy`/win-flag changes**; **0 RESOLVED/PROVISIONAL state
changes**.

Flagship (ADR-predicted): urza surfaces `draw-a-card` recursion; aristocrats decks
surface the token/creature half of the sac loop.

## 0-genuine-regression confirmation

**Confirmed: 0 genuine regressions.** No engine family is lost (the only 3 lead-event
drops are same-family sibling-merges). No junk (<5%) displaces a lead. No label/
win-flag/state change.

One **watch-item, not a regression** (responsible combo noted): `DraconicRage_Gruul_2021`
— its single combo wires the gruul deck's removal/burn attrs as producers of
`permanent-dies`, inflating it to mass 144 / share 0.414 above the combat lead. The
combat engine (`creature-attacks`) stays elected, the label (`burn`) is unchanged,
and it is high-mass (not junk), so it fails every regression criterion. Worth a human
eyeball for over-emphasis only.

## SHAs — old → new (re-baseline = confirmation)

B1 feeds only `engine_report`; the parity oracle hashes the store-independent
`resolve`/`compute_facts` path (committed `test_p0_shas_held_after_population`
asserts exactly this). Reproduced from the **live lake** with the full store populated:

| SHA | committed | B1-ON | moved |
|---|---|---|---|
| engine_isolation | `a7edab20…17e177` | `a7edab20…17e177` | no |
| resolve | `25f50a23…ca464a` | `25f50a23…ca464a` | no |
| full-corpus `FULL_CORPUS_SHA` | `5ce447ef…c24f343` | `5ce447ef…c24f343` | no |

**No SHA constant / parity fixture edited** (none changed; editing would break the
invariant test). The `engine_report` election has no committed SHA baseline, so
there is no per-test election SHA to update.

## P0 reproduces

`python -m pipeline.transforms.strategy.tests.engine_parity_oracle` against the live
lake, store populated → all three SHAs match the recorded baselines ("OK: matches
recorded FULL_CORPUS_SHA").

## Suite

- `pipeline/transforms/strategy/tests` → **547 passed, 4 skipped** (ADR baseline).
- `tests/test_transforms.py` + `tests/test_scoring_guard.py` → 51 passed.
- `ruff check pipeline/transforms/build.py` → clean.

## Confidence

**HIGH** on: the integration matches the pipeline's derived-data pattern; the P0
byte-parity holds (reproduced from the live lake + committed invariant test); 0
junk-noise; 0 label/win-flag/state changes; coverage strictly improves; suite green.

**MEDIUM** on: the *qualitative* "improvement vs neutral" split for the 32 lead
re-ranks is a structural judgment (prior lead retained at unchanged mass, same
engine family, dark drops) rather than a ground-truth deck-strategy check; the
DraconicRage mass-inflation pattern (removal-attrs → permanent-dies) is the one
case I'd want a human to eyeball before shipping. No case meets the regression bar.

## For the orchestrator

- In-repo change: `pipeline/transforms/build.py` (+48/−3). Deliverables:
  `research/b1-on-170-delta.md`, `research/b1-on-170-delta.raw.json`, this outbox.
- Production store (`machine_vocab/attributes.jsonl`, 57 362 edges) is a data-dir
  asset (not git-tracked), materialized to the full B1-ON state.
- NO commit made (per task). Gate PASSED: 0 genuine regressions.
