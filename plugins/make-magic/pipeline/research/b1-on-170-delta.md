# B1-ON full-170 engine-election delta (ADR 0021 B1)

**Task 7.1 — turn ON the combo-edge machinery in the production pipeline.**
HEAD `46dce39`. Lake: `~/.local/share/make-magic/normalized/{combo,card_otag}.parquet`.
Measurement harness + raw per-deck records: `research/b1-on-170-delta.raw.json`.

## TL;DR

- **Integration: Option A (ship-materialized, #65 pattern).** `populate_combo_edges`
  is wired into the mart-build flow (`pipeline/transforms/build.py::run`) as a
  second-tier derived asset lowered from the `combo` + `card_otag` marts, landing
  the `ProvisionalAttributeStore` the `engine_report` path already loads. No
  commit (orchestrator commits).
- **Gate: 0 GENUINE regressions.** 63/170 decks change their `engine_report`
  election; **43 IMPROVEMENT, 20 NEUTRAL, 0 REGRESSION.** Every changed deck's
  dark-card coverage strictly improves (−6 … −56). 0 `named_strategy`/win-flag
  changes, 0 RESOLVED/PROVISIONAL state changes, **0 low-share (<5%) junk units
  added** (the #76 concern is NOT triggered), bounded set sizes (max 10→11, mean
  3.95→4.30).
- **Re-baseline = no-op confirmation.** B1 feeds ONLY `engine_report`; the parity
  oracle hashes the store-independent `resolve`/`compute_facts` path, so the three
  P0 SHAs do **not** move and are **not** edited. Reproduced from the live lake
  WITH the full store populated.

## Integration choice — Option A (ship-materialized)

The pipeline's derived data is **materialized**, not runtime-derived: `build.run()`
→ `driver.build_all()` writes the normalized parquet marts (`card_otag`, `combo`)
to the XDG data dir (overwrite via `write_parquet`); `collection hydrate-lake`
wraps it. The combo-edge store is the **same kind of asset one tier down**: it is
a pure deterministic function of the `combo` + `card_otag` marts, versioned by
`derivation_version`, and `engine_report.identify_engines` already loads it via
`load_store(None)` from the default store dir. Option B (runtime lazy re-derive
on load) would be the only runtime-derived asset in a materialize-everything
pipeline — inconsistent. **Option A matches.**

**Implementation** (`pipeline/transforms/build.py`): a new `_materialize_combo_edges()`
runs after `driver.build_all()` and is added to the `run()` result map as
`combo_edges`. It is a **reproducible clean rebuild** of the spellbook
(combo-edge) layer — it drops the existing spellbook records and re-derives all
combos from the current lake, preserving any non-spellbook (future haiku)
records. This matches the marts' overwrite semantics and is required for
correctness: the `derivation_version` skip key only guards *logic* changes, not
lake-content drift, so a plain append would silently keep **stale combos** from
an older lake (observed: appending to the ambient partial store yielded
53 371 edges / 1 272 combos vs. the clean 57 362 / 1 272 the current lake
implies — see "Reproducibility" below).

Materialized production store: `~/.local/share/make-magic/machine_vocab/attributes.jsonl`
= **57 362** spellbook edges over **1 272** edge-producing combos (of 2 000 in the
lake); re-running `_materialize_combo_edges()` is idempotent (57 362 → 57 362).

## Measurement method

For each of the 170 gauntlet `.dck` decks (no Airtable), built via
`corpus.build_deck_from_dck` + `attach_meta` exactly like `run.recalc_corpus`,
we ran `engine_report.identify_engines(deck, combos=combos, store=…)` twice:

- **B1-OFF** = `ProvisionalAttributeStore()` (empty) — the committed P0 state.
- **B1-ON**  = the store freshly materialized from the live lake (57 362 edges).

Diffed: the elected candidate set (`raw_isolation['elected']` events + mass/share
+ recursive/commander-coupled flags), the named engines (`report.engines`), the
honesty state, dark-card count, and `resolve`'s `named_strategy` (store-independent
label — captured to confirm it does not move).

## Result — classification (gate)

| Category | Decks | Meaning |
|---|---:|---|
| IMPROVEMENT (engine added) | 14 | New substantive engine surfaced; prior lead unchanged. |
| IMPROVEMENT (engine surfaced, prior lead retained) | 29 | A combo-implied recursive engine out-ranks the prior lead, which **stays elected at unchanged mass** (rank-don't-cut, ADR 0022). |
| NEUTRAL (lead→sibling-merge) | 3 | Prior lead event deduped into a Jaccard-≥0.5 **same-family sibling** (engine retained, event re-labelled). |
| NEUTRAL (re-rank/coverage) | 17 | Same elected set / lead; only mass/share re-normalized + more dark cards covered. |
| **REGRESSION** | **0** | — |
| Unchanged | 107 | byte-identical election. |

**Aggregate gate metrics (all 170):**
- dark cards 10 858 → 8 793 (**−2 065**); **63 decks improve, 0 worsen**.
- elected set size: max 10→11, mean 3.95→4.30 (bounded; 42 grew, 1 shrank, rest equal).
- added elected units: **68 substantive (≥5% share), 0 junk (<5%)**, 0 junk-as-lead → **#76 not triggered**.
- `named_strategy` / win-flags: **0 changes** (store-independent; verified structurally — `resolve`→`name_deck` never consults the store — and by the committed `test_p0_shas_held_after_population`).
- RESOLVED/PROVISIONAL state: **0 changes**.

### 0-genuine-regression enumeration

A regression = a previously-correct election broken: a genuine engine family lost
with no sibling, OR junk (<5%) displacing a genuine lead, OR a label/win-flag
change. **None occur.** The three cases that could *look* like a loss — the only
decks where the OFF lead *event* leaves the ON set — are all Jaccard-0.5
sibling-merges within the same engine family (the ADR 0022 dedup folding the
smaller unit's members into a larger one); the engine is retained:

| Deck | label | OFF lead → ON | retained via |
|---|---|---|---|
| `dimir__wilhelt-the-rotcleaver` | typal/tribal | `permanent-dies` → `creature-enters-battlefield` lead | `permanent-sacrificed` (sac/death family, recursive + cmd-coupled) still elected |
| `grixis__jeleva-nephalia-s-scourge` | alt-wincon/combo | `recursion-from-graveyard` → `card-enters-graveyard` lead | same graveyard-recursion family, now the lead |
| `naya__rocco-cabaretti-caterer` | alt-wincon/combo | `token-created` → `creature-enters-battlefield` lead | tokens-are-creatures-entering; members merged into CEB |

**Watch-item (NOT a regression), responsible-combo noted:** `DraconicRage_Gruul_2021`
(constructed burn, 1 detected combo) — the combo lowers the deck's many
removal/burn attributes (`burn-*`, `removal-*`, `spot-removal`) as producers of
`permanent-dies` and its token generators as the reciprocal, inflating a
`permanent-dies` recursive engine to mass 144 / share 0.414 — above the combat
lead. The combat engine (`creature-attacks`) **remains elected**, the label
(`burn`) is unchanged, and the unit is high-mass (not <5% junk), so it fails every
regression criterion. Flagged only because death-as-lead over-emphasizes an aggro
deck's game-plan; worth a human eyeball, but structurally correct (the deck does
run that token↔sacrifice loop when the combo is present).

### Flagship intended improvements (confirmed)

- `mono-blue__urza-lord-high-artificer` → **`draw-a-card` surfaces as lead** (share
  0.323), exactly the ADR 0021 B1 prediction ("the election surfaces Urza's genuine
  `draw-a-card` recursion"); `artifact-enters` retained at unchanged mass.
- `mono-black__ayara-first-of-locthwain`, `rakdos__rakdos-the-muscle`,
  `jund__korvold`, `mardu__alesha` (aristocrats) → the token/creature half of the
  sac loop (`creature-enters-battlefield` recursive) surfaces; `permanent-dies`
  retained at unchanged mass.
- `mono-green__{selvala,marwyn}`, `bant__helga`, `jeskai__{elsha,kykar}`,
  `izzet__ral`, `temur__xyris` → mana/spell/token engines now close into their
  `draw-a-card` payoff loop.

### Divergence from the fixture prediction (reported)

The task's fixture measurement (48 combos) predicted "13/17 no-op, Krrik/Ozai
contained, 0 win-flag changes." Against the **full 2 000-combo lake** the blast
radius is larger — **63/170 decks change, 32 lead re-ranks** — and Krrik's lead
re-ranks (`card-enters-graveyard`→`creature-enters-battlefield`), i.e. the
"contained" fixture framing does not hold at full-corpus scale. This is expected
(the full lake wires far more combo edges than 48 fixtures) and remains within the
gate: all changes are improvement/neutral, 0 regressions, 0 win-flag changes. The
"0 win-flag changes" prediction holds exactly (the win flags live on the
store-independent `resolve` path).

## SHAs — old → new (re-baseline is a confirmation, not a change)

B1 feeds **only** `engine_report`. The parity oracle hashes `resolve`/`_isolate_engines`
(facts.py), which never consult the `ProvisionalAttributeStore` — enforced by the
committed `test_combo_edges.py::test_p0_shas_held_after_population`. Reproduced
from the live lake with the full 57 362-edge store populated:

| SHA | committed (P0) | recomputed B1-ON | moved? |
|---|---|---|---|
| engine_isolation composite | `a7edab20…17e177` | `a7edab20…17e177` | **no** |
| resolve composite | `25f50a23…ca464a` | `25f50a23…ca464a` | **no** |
| full-corpus (`FULL_CORPUS_SHA`) | `5ce447ef…c24f343` | `5ce447ef…c24f343` | **no** |

**No SHA constant or parity fixture is edited** — editing them would falsely imply
the byte-parity path shifted and would break the committed invariant test. The
`engine_report` election has no committed SHA baseline, so there is no per-test
SHA constant to update for the election delta.

## Suite

`uv run --extra dev pytest pipeline/transforms/strategy/tests -q` → **547 passed,
4 skipped** (ADR baseline). `tests/test_transforms.py` + `tests/test_scoring_guard.py`
(the hydrate/build-adjacent suites) → 51 passed. `ruff check` on the changed file
passes.

## Follow-ups (not in scope, noted)

- The `ProvisionalAttributeStore` default dir is hard-coded to
  `~/.local/share/make-magic/machine_vocab/` and does not honor `MAKE_MAGIC_DATA_DIR`
  (the marts do, via `StorePaths`). They coincide in the default environment; a
  machine_vocab change to route the store through `StorePaths` is an ADR-0008 touch
  left for its own change.
- `DraconicRage`-style removal-attr mass-inflation on `permanent-dies` is the one
  qualitative over-emphasis worth a human eyeball (not a regression).
