# Role-B (L5) Engine Materialization — `claude -p` self-synthesis

**Task 7.4** (+ folds in 7.3 re-validation). Productionizes role-B, the L5
engine-synthesis layer, as a real pipeline component: the pipeline now resolves
each deck's core **Engine** itself — no separate API key, no human-agent in the
loop — using headless Claude Code (`claude -p`, ambient auth) as the synthesis
backend + a materialized JSONL cache for determinism / amortization.

HEAD at time of work: `a398705` (B6 — archetype-grain substantiveness gates the
election), on top of `369f209` (B1 combo-edges ON). Branch `feat/strategy-engine`.

---

## STEP 0 — `claude -p` verification (the gate)

**VERDICT: PASS.** Nested `claude -p` works in this environment under ambient
auth, with **no `ANTHROPIC_API_KEY`** set. It returns a well-formed JSON envelope
whose `.result` field carries the model's text; the strict
`{strategy, win_condition, key_pieces, confidence}` engine comes back cleanly.

### Exact working invocation

```bash
claude -p "<synthesis_prompt + one deck's input JSON>" \
    --output-format json \
    --model claude-sonnet-4-5
```

- Exit 0, `ANTHROPIC_API_KEY` **unset** throughout (confirmed before + after
  `. ../.env`; `.env` holds only `AIRTABLE_API_KEY`).
- Output shape: a JSON envelope. Fields used: `.result` (string, the model
  output — may be fenced in ```` ```json ````), `.is_error` (bool),
  `.subtype` (`"success"`), `.total_cost_usd`, `.modelUsage.<model>`.
- The model text parses to exactly `{"slug": ..., "engine": {strategy,
  win_condition, key_pieces, confidence}}`.

### Model pinning + cost (measured, one Ozai deck)

| `--model`              | model that ran     | cost/deck |
|------------------------|--------------------|-----------|
| `claude-sonnet-4-5`    | claude-sonnet-4-5  | ~$0.18    |
| `haiku`                | claude-haiku-4-5   | ~$0.063   |
| `sonnet` (bare alias)  | **ignored** → claude-opus-4-6 (default) | ~$0.24 |

**Pinning works with explicit model ids** (`claude-sonnet-4-5`, `haiku`). The
bare `sonnet` alias is NOT recognized by this CLI build and silently falls
through to the session default (opus-4-6) — so the driver defaults to the
explicit `claude-sonnet-4-5`.

Because `claude -p` works nested, the driver is built directly on it (no
out-of-band ops requirement), AND the synth backend is pluggable
(`SynthBackend` protocol) with a `MockBackend` for offline unit tests.

---

## Architecture

Three new pieces, modeled on the existing combo-edge / machine-vocab stores
(JSONL + version stamping + XDG data dir), all **Tier-2 / provisional** — never
on the frozen P0 deterministic path.

### 1. Engine cache store — `pipeline/transforms/strategy/engine_cache.py`

Per-deck resolved Engine, durable JSONL at
`~/.local/share/make-magic/engine_cache/engines.jsonl` (XDG data dir, same home
as the machine-vocab + DuckDB lakes). One record per deck:

```json
{"slug": "...", "deck_name": "...", "input_hash": "<sha256>",
 "synthesis_version": 1, "engine": {...}, "model_id": "claude-sonnet-4-5",
 "synthesized_at": "...Z", "extraction_version": 1, "derivation_version": 1}
```

**Staleness key (two-layer, ADR 0021 model extended to L5):** a cached Engine is
fresh iff BOTH hold:

- `input_hash` matches — a stable SHA-256 over the canonical role-B *input*
  (the deterministic `compute_facts` + `name_deck` output). The input carries
  the full elected set + detected combos + archetype identity, so **any
  engine-logic change (B1, B6, a new curated edge, a deck edit) flows through to
  a different hash and auto-invalidates the cache**. This is the "deck or
  engine-logic changed" trigger, self-maintaining. `deck_name` and internal
  `_`-prefixed flags are excluded from the hash (cosmetic).
- `synthesis_version` matches — bumped by hand ONLY when the synthesis
  prompt/rules change (input unchanged, re-run wanted).

`persist` / `persist_batch` are idempotent (skip already-fresh), append-only
with last-write-wins on load → resumable in-place refresh.

### 2. Materialization driver — `scripts/materialize_engines.py`

Iterate decks → extract role-B input on the **current B1+B6 engine** (reuses the
packaged `build_synthesis_input`, the single source the extract tool now also
delegates to) → compute staleness key → cache-hit skip / miss → `claude -p`
synthesis → verify JSON well-formed + `key_pieces` in-deck (reject + log
otherwise) → write to cache. Sequential, idempotent, resumable. Never fans
`claude -p` out in parallel.

- `--fixtures` (17 golden fixtures, incl. all 7 B-test anchors; lake-free) —
  the representative re-validation sample.
- `--gauntlet` (170 lake-backed decks) — the full corpus.
- `--slugs a,b` allow-list; `--model`; `--dry-run` (extract+hash only);
  `--outputs <jsonl>` (also emit a measure-harness file).

### 3. Pipeline reader — `engine_synthesis.resolve_engine(deck, ...)`

The production Tier-2 entry point. Returns `(engine, meta)`: `hit` (fresh
cached Engine, no LLM), `synthesized` (miss → `claude -p` → validate → cache),
`miss` (synth disabled / no backend), or `error` (synth failed). Tier-1
(deterministic `resolve(deck)` archetype identity) is ALWAYS available and
independent of this layer; the Engine is the Tier-2 enrichment on top.

Backends: `ClaudeCliBackend` (shells `claude -p --output-format json --model`,
reads `.result`) and `MockBackend` (deterministic, for tests/offline). The
synthesis prompt is **vendored into the repo** at
`pipeline/transforms/strategy/roleb_prompt.md` so the component is self-contained
(no plan-tree dependency at runtime).

### Reuse / no-drift

`scripts/extract_roleb_inputs.py::extract_one` was refactored to delegate to the
packaged `build_synthesis_input` — verified **byte-identical** output on all 17
fixtures, so the extract tool and the production reader can never diverge.

---

## Sample materialization (end-to-end via real `claude -p`)

Representative sample = the **17 committed golden fixtures** (lake-free), which
include **all 7 B-test anchors** (urza, ozai, baral, kess, krrik, tuvasa, sokka)
plus external-validation decks (3 precons, 4 other gauntlet year-bundles, 6
owned). All inputs re-extracted on the **current B1+B6 engine** first.

```
SUMMARY: {'hit': 0, 'synthesized': 17, 'rejected': 0, 'failed': 0, 'dry': 0}
Wrote 17 engines to subagents/7.4-materialization/sample_outputs.jsonl
```

- **17/17 synthesized** by real `claude -p --model claude-sonnet-4-5`, cached.
- **0 rejects, 0 failures** — every `key_pieces` list is in-deck, every engine
  well-formed. (Stub deck `welcoming-vampire`, 19 cards, grounds to "Small
  Creature Draw" LOW with 4 real cards rather than erroring.)
- **Idempotency proven**: an immediate re-run is **17/17 cache HIT, 0
  synthesized, 0.4 s, zero `claude -p` calls**.
- Cost: ~17 × $0.18 ≈ **$3** for the sample.

### The R5-grounding finding (prompt v1 → v2)

The first real run (prompt **v1**, graph-outputs only) produced **4 rejects**:
`facelessmenace`, `sokka`, `spiders` named plausible archetype staples NOT in
the specific decklist (e.g. Sokka → Young Pyromancer / Impact Tremors; Shelob →
Blood Artist / Zulaport Cutthroat), and `welcoming-vampire` returned empty
`key_pieces`. These were **the production R5 check working** — the agent-layer
harness (`roleb_measure.py`) only *notes* out-of-deck key_pieces, never enforces,
so this gap was invisible until now. Root cause: the role-B input gave the model
no card list, so it could only safely cite commanders + combo pieces.

**Fix (prompt v2, `SYNTHESIS_VERSION=2`):** add an `in_deck_cards` roster
(canonical non-basic-land names) to the input and tighten R5 to "choose
`key_pieces` ONLY from `in_deck_cards`". Re-running the full sample on v2:
**0 rejects**. This makes R5 both *enforceable and satisfiable* — the right
production bar. (Name canonicalisation also strips the `name|SET|collector`
suffix that lake/dck-built decks carry, so the in-deck check is format-robust.)

## Re-validation (task 7.3) — anchors on the final B1+B6 engine

Ran `scripts/roleb_measure.py` on the v2 sample:

```
SUMMARY: 17 decks | 7 RIGHT | 10 defensible | 0 wrong
FLAGS: 0 F4 violations | 0 empty engines
```

**All 7 B-test anchors RIGHT on the final B1+B6 engine. 0 wrong, 0 F4, 0 empty**
— role-B did **not** regress after B1 (combo-edges ON) + B6 (archetype-gated
election); the richer/gated candidate sets are equal-or-better. The 10
non-anchor "defensible" verdicts are the harness's default for decks without a
pinned expectation, not misses. Exit code 0 (all anchors RIGHT).

Anchor strategies: Urza → *Artifact Mana-Loop Combo*; Ozai → *Burn Direct
Damage* (F4 holds, no combo hijack); Tuvasa → *Enchantress Value Engine*; Baral
→ *Countermagic Control*; Kess → *Consultation Storm Combo*; K'rrik →
*Life-as-Resource Combo*; Sokka → *Ally Spellslinger Token Swarm* (spellslinger,
NOT counters).

### Did any Engine CHANGE vs the pre-B1+B6 baseline?

Comparing the 17 v2 engines to the agent-layer pre-B1+B6 baseline
(`baseline/outputs_pre_b1b6.jsonl`): **3 anchors identical** (Urza, Ozai,
Tuvasa — the hard F4/R3 cases), 14/17 strategy *strings* reworded. **Every
rewording preserves the semantic identity** (e.g. Baral "Control Spellslinger"→
"Countermagic Control"; Sokka "Spellslinger Ally Tokens"→"Ally Spellslinger
Token Swarm"). The string churn is attributable to **(a) the v2 prompt change
(in_deck_cards grounding) and (b) LLM synthesis non-determinism**, NOT to B1+B6
moving any identity — verified: the one deck with a real identity shift
(`abzanarmor` Sacrifice→Beatdown) is **NOT** in the B6 28-deck election-delta and
its elected set is unchanged, so that shift is generative churn, not a B6 effect.
The rigorous no-regression signal is the measure verdict (7/7 RIGHT), which holds
on the final engine. Full table: `subagents/7.4-materialization/baseline_comparison.txt`.

## Full-170 batch command

```bash
cd plugins/make-magic/pipeline
set -a; . ../.env; set +a   # AIRTABLE only; NO ANTHROPIC_API_KEY
# Refresh inputs on the current engine (deterministic, no LLM), then materialize:
uv run python scripts/extract_roleb_inputs.py --gauntlet
uv run python scripts/materialize_engines.py --gauntlet \
    --model claude-sonnet-4-5 \
    --outputs subagents/7.4-materialization/gauntlet_outputs.jsonl
# Re-validate:
uv run python scripts/roleb_measure.py subagents/7.4-materialization/gauntlet_outputs.jsonl
```

Sequential, idempotent, resumable (re-run skips already-fresh decks). Estimated
cost ~170 × $0.18 ≈ **$30** (sonnet-4-5) or ~$11 (haiku). Runs outside the
frozen path; the cache is Tier-2 provisional.

## P0 reproduction

The frozen P0 deterministic path is **untouched** — the new modules
(`engine_cache.py`, `engine_synthesis.py`) are Tier-2 and are not imported by
`resolve` / `compute_facts` / `name_deck`. The only edit to existing code is
`scripts/extract_roleb_inputs.py::extract_one` (a tool, delegating to the
packaged builder), verified byte-identical on all 17 fixtures.

All three frozen P0 SHAs reproduce at HEAD `a398705`:

- engine `afcffc8a…` — ✓ (`test_machine_vocab.py`)
- resolve `3010be2a…` — ✓ (`test_machine_vocab.py`)
- full-corpus `4acf0f41…` — ✓ (`test_resolution_fidelity.py`, lake-gated)

Full strategy suite: **580 passed, 4 skipped** (+33 new tests for the cache +
synthesis modules).

## Files

| Path | Role |
|------|------|
| `pipeline/transforms/strategy/engine_cache.py` | Tier-2 engine cache store (JSONL, staleness key, versions) |
| `pipeline/transforms/strategy/engine_synthesis.py` | input builder, `claude -p` + mock backends, parse/validate, `resolve_engine` reader |
| `pipeline/transforms/strategy/roleb_prompt.md` | vendored synthesis rules (v2, `in_deck_cards` + R5) |
| `scripts/materialize_engines.py` | materialization driver (`--fixtures`/`--gauntlet`/`--slugs`/`--dry-run`) |
| `scripts/extract_roleb_inputs.py` | now delegates `extract_one` to the packaged builder (no drift) |
| `pipeline/transforms/strategy/tests/test_engine_cache.py` | 13 cache tests |
| `pipeline/transforms/strategy/tests/test_engine_synthesis.py` | 20 synthesis/reader tests (MockBackend) |

Cache store (XDG): `~/.local/share/make-magic/engine_cache/engines.jsonl`.

## ADR note (for the committing orchestrator)

This resolves ADR 0021 OPEN item **(B.3)** — "role-B production wiring …
production materialization … is a TRACKED FOLLOW-UP." The ADR's composition map
lists `Synthesis → Engine | B.3 | role-B (ADR TBD at build)`. On commit, the
orchestrator should update ADR 0021's B.3 line to "wired" and consider a
dedicated role-B ADR citing these code paths.
