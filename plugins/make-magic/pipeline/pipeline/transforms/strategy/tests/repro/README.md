# Reproducibility gate (Leg A / Leg B)

A standing, **infrequently-run** verification gate that proves the strategy
engine's rich extraction — **attributes → events → eventeffects → strategies →
engines** — is a property of the *project* (code + its committed, declared
inputs), reproducible by a third party, and not an artifact of a developer's
local machine state. The P0 composite SHAs
(`ENGINE` / `RESOLVE` / `FULL_CORPUS`) are the fast tripwire; a per-deck
semantic dump + classifying differ is the richness/accuracy comparator beneath
them.

Governed by [ADR 0003](../../../../../docs/adr/0003-byte-parity-held-files.md)
(byte-parity held files + the full-corpus SHA) and
[ADR 0022](../../../../../docs/adr/0022-concept-anchored-derived-adjacency.md)
(concept-anchored derived adjacency — the taxonomy that a tags-less lake silently
falls back away from).

## What it proves

### Leg B — determinism
Two independent **fresh-subprocess** runs against the committed pinned lake,
under a deliberately varied `PYTHONHASHSEED` (0 vs 4242) **and** external CPU
scheduling pressure, must produce:

- **identical** `ENGINE` / `RESOLVE` / `FULL_CORPUS` composites, each equal to
  the pinned manifest value, **and**
- a **zero-divergence** semantic diff (0/187 decks moved).

PASS iff all three composites match across both runs and the pin, and the diff
is empty. Any divergence is a **code non-determinism bug**; the verdict names the
layer and the likely cause (DuckDB scan order / hash seed / float formatting).
There is no DuckDB thread knob in the strategy path (every `duckdb.connect` is
`:memory:` with no `threads`/`PRAGMA`/`config=` — verified), so run2's scheduling
variation is applied externally via portable, self-terminating CPU burners
(best-effort).

### Leg A — drift impact
One **genuinely live** Scryfall `oracle_tags.sync` into an isolated lake that
keeps the pinned 39,288-card bulk **frozen** and refetches **only** the
oracle-tags, re-rolls `card_otag`, extracts, and diffs against the pinned
reference. It quantifies how much real upstream tag drift perturbs the rich
outputs — a **number and a list**, per the design, not just "tags changed":

```
N/187 decks moved, M substantive, card-universe bucket = 0
```

- **liveness guard** — the fetch is forced (`force=True`, no cursor) and verified
  genuine: real HTTP meta+payload evidence, tag count ≥ 4,600 (the bundled
  fail-open snapshot is ~4,499), SHA ≠ the coherent pre-drift source. A fail-open
  (no payload GET / snapshot-sized result) is `INVALID`, never a silent pass.
- **card-universe bucket MUST be empty** — the card bulk is frozen, so any
  card-pool movement means the pinned bulk leaked (an isolation breach,
  test-integrity failure) → `INVALID`.
- **verdict**: `PASS` iff zero substantive divergences; `SUBSTANTIVE-REVIEW`
  (non-PASS, needs human adjudication — benign-by-judgment / improvement /
  regression) when real drift moved an output; `INVALID` on a liveness/isolation
  breach. Drift is *expected*; the verdict is the benign-vs-substantive judgment,
  not diff-is-empty.

Leg A never runs the parity oracle (which rewrites the repo fixture — genuine
drift would dirty the tree); its composites come from the dump's own
`meta.composites`, and fixture-layer drift surfaces as `fixture::` decks in the
diff. The harness also restores the fixture and SHA-checks the primary lake
before/after, so a run never mutates the repo or `~/.local/share/make-magic`.

## When to run

- **pre-release** (the CI workflow's `v*` tag trigger, Leg B; Leg A is opt-in);
- after a **major refactor** to extraction / adjacency derivation / hydration;
- after an **intentional re-baseline** (then re-pin — see below).

Not on ordinary push/PR — that is `ci.yml`'s offline suite. The default
`pytest` runs **neither** leg (deselected via the `repro` marker) and makes zero
network calls.

## How to run

```bash
cd plugins/make-magic/pipeline

# Leg B only (network-free; works from the committed fixture, no primary lake):
uv run --extra dev pytest -m repro -q -rs
#   -> Leg B PASSES; Leg A skips-with-notice (needs the opt-in below)

# Both legs (ONE live Scryfall fetch):
MAKE_MAGIC_REPRO_ALLOW_LIVE=1 uv run --extra dev pytest -m repro -q -rs

# Or drive the harness CLI directly (durable artifacts + verdict.json/md in --out):
uv run python -m pipeline.transforms.strategy.tests.repro.harness legb --out /tmp/repro
uv run python -m pipeline.transforms.strategy.tests.repro.harness lega --out /tmp/repro   # live
uv run python -m pipeline.transforms.strategy.tests.repro.harness all  --out /tmp/repro   # both
```

`MAKE_MAGIC_REPRO_FETCH_LOG=<path>` steers the Leg A FETCH-* evidence log
(default `<out>/fetch.log`). The CLI exits 0 on PASS, non-zero on FAIL, naming
the failing leg; each run writes `verdict.json` (machine) + `verdict.md` (human).

## Re-pin procedure (intentional re-baseline moves `FULL_CORPUS`)

When a sanctioned, proof-carrying re-baseline (ADR 0003) intentionally moves the
corpus — e.g. a coherence rebuild that re-rolls `card_otag` from fresh raw tags —
the committed pinned lake must be re-pinned so the gate measures against the new
reference:

```bash
cd plugins/make-magic/pipeline

# 1. Rebuild/verify the real lake so it reproduces the new committed baselines
#    (normal re-baseline flow; NOT part of this gate).
# 2. Re-pin the fixture + regenerate the manifest (copies the 4 files, recomputes
#    the 3 composites from the source lake, writes manifest.json):
uv run python -m pipeline.transforms.strategy.tests.repro.harness repin \
    --from "$HOME/.local/share/make-magic"
# 3. Confirm the gate is green against the new pin:
uv run --extra dev pytest -m repro -q
# 4. Commit pinned_lake/ (4 parquets + manifest.json) in the SAME proof-carrying
#    re-baseline change, with the OFF/ON delta recorded (ADR 0003 discipline).
```

`repin` carries over the manifest's provenance notes (the `oracle_tags`
`provenance_only`/coherent-source annotations) and fails if the source lake is
missing any of the four files. The harness refuses to run if the committed
fixture SHAs ever stop matching the manifest — the reference is the root of
trust.

## The pinned lake (`pinned_lake/`)

Four content-addressed parquet files (lake-relative layout) + `manifest.json`:

| file | role | SHA (pinned) |
| --- | --- | --- |
| `raw/oracle_cards.parquet` | 39,288-card Scryfall oracle bulk (frozen) | `829d7c86…` |
| `raw/oracle_tags.parquet` | adjacency taxonomy for `derive_card_events` (provenance) | `9289959165…` |
| `normalized/card_otag.parquet` | per-card tag memberships (coherent `df31df7c` rollup) | `a51097a5…` |
| `normalized/combo.parquet` | combo mart | `f020ac84…` |

All four are required in any isolated lake: a lake **without**
`raw/oracle_tags.parquet` silently falls back to the enumerated adjacency (a
different algorithm, ADR 0022) — the harness fails the leg if that warning fires.
The pinned composites are `ENGINE 4d3f5e6d…`, `RESOLVE 940c3881…`,
`FULL_CORPUS ec917355…`.
