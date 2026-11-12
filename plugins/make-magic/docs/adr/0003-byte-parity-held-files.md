# 0003 Byte-parity held files: the curated strategy layer is byte-frozen, changed only by a proof-carrying re-baseline

| Field | Value |
|---|---|
| Status | Accepted. In force on branch `feat/strategy-engine`. |
| Review | ratified by the owner on 2026-11-11. (Agent-authored; owner-ratified.) |
| Test | From `plugins/make-magic/pipeline`: `uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle` records a composite SHA over the serialized `_isolate_engines` output plus the full `resolve()` records for the 17 hermetic fixture decks; a parity test imports `composite_sha` to assert no drift. **Honest gap:** the oracle covers only the 17 hermetic fixtures; the full 170-deck curated-corpus SHA needs lake access (DuckDB + card-meta + combo tables) and is not lockable offline. |
| Code paths | [`pipeline/.../strategy/data/attribute_vocab_v4.json`](../../pipeline/pipeline/transforms/strategy/data/attribute_vocab_v4.json), [`event_catalog_v4.json`](../../pipeline/pipeline/transforms/strategy/data/event_catalog_v4.json), [`facts.py`](../../pipeline/pipeline/transforms/strategy/facts.py), [`query.py`](../../pipeline/pipeline/transforms/strategy/query.py), [`vocab.py`](../../pipeline/pipeline/transforms/strategy/vocab.py), [`tests/fixtures/vocab_parity_baseline.json`](../../pipeline/pipeline/transforms/strategy/tests/fixtures/vocab_parity_baseline.json) |

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
- Honest gap (restated): the hermetic oracle is 17 decks; the full-corpus parity needs
  lake access via `recalc_corpus()`.

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

## Related records
- 0001 — the knowledge-placement doctrine these files exemplify.
- 0002 — the substantiveness model (`facts.py` is shared with this record).
- 0005 — the tribe-catalog fix in the re-baseline above.
