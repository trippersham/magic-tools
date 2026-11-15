# Task 7.4 — Role-B production materialization — OUTBOX

**Status: COMPLETE (NO COMMIT — orchestrator verifies + commits).**
HEAD `a398705`, branch `feat/strategy-engine`, worktree `uncommon-mtg`.

Productionizes role-B (L5 engine-synthesis) as a real pipeline component: the
pipeline resolves each deck's core Engine itself via headless `claude -p`
(ambient auth, no API key) + a materialized cache. Also re-validates role-B on
the final B1+B6 engine (folds in 7.3).

---

## STEP 0 — `claude -p` verdict: **PASS**

Nested `claude -p` works in this environment under **ambient auth, no
`ANTHROPIC_API_KEY`**. Exact working invocation:

```bash
claude -p "<synthesis_prompt + one deck input JSON>" \
    --output-format json --model claude-sonnet-4-5
```

- Returns a JSON envelope; the model text is in `.result` (may be ```` ```json ````
  fenced). Parses to strict `{slug, engine:{strategy, win_condition, key_pieces,
  confidence}}`. `is_error` / `subtype:"success"` / `total_cost_usd` present.
- **Model pinning works with explicit ids** (`claude-sonnet-4-5` $0.18/deck,
  `haiku` $0.063). The bare `sonnet` alias is NOT recognized and silently falls
  to the session default (opus-4-6, $0.24) — so the driver defaults to the
  explicit `claude-sonnet-4-5`.
- Because it works nested, the driver is built directly on it AND the backend is
  pluggable (`SynthBackend` protocol + `MockBackend` for offline unit tests).

## What's built (all Tier-2 / provisional — NOT the frozen P0 path)

1. **Engine cache** `pipeline/transforms/strategy/engine_cache.py` — per-deck
   resolved Engine as JSONL at `~/.local/share/make-magic/engine_cache/engines.jsonl`.
   **Two-layer staleness key**: `input_hash` (canonical SHA over the role-B
   input — any B1/B6/deck change auto-invalidates) + `SYNTHESIS_VERSION`
   (hand-bumped on prompt change). Idempotent, append-only, resumable.
2. **Materialization driver** `scripts/materialize_engines.py` — extract input on
   current engine → cache-hit skip / miss → `claude -p` → verify well-formed +
   key_pieces in-deck (reject+log) → cache. Sequential, idempotent, resumable;
   `--fixtures`/`--gauntlet`/`--slugs`/`--model`/`--dry-run`/`--outputs`.
3. **Pipeline reader** `engine_synthesis.resolve_engine(deck,…)` → `(engine,
   meta)`: `hit` / `synthesized` / `miss` / `error`. Tier-1 (`resolve(deck)`
   archetype identity) is always available; the Engine is this Tier-2 enrichment.
   Prompt vendored into the repo (`roleb_prompt.md`); `extract_one` refactored to
   delegate to the packaged builder (byte-identical, no drift).
4. **Tests** — 33 new (13 cache + 20 synthesis/reader, all MockBackend/offline).

## Sample materialized + re-validation

Representative sample = **17 golden fixtures** (incl. all 7 anchors), inputs
re-extracted on the current B1+B6 engine, materialized end-to-end via real
`claude -p --model claude-sonnet-4-5`:

- **17/17 synthesized, 0 rejects, 0 failures.** Immediate re-run = **17/17 cache
  HIT, 0 LLM calls, 0.4 s** (idempotency proven). Sample cost ~$3.
- **Re-validation (7.3):** `roleb_measure.py` → **7/7 anchors RIGHT, 0 wrong, 0
  F4 violations, 0 empty.** Role-B did NOT regress after B1+B6.
- **Anchors still RIGHT?** YES, all 7. Urza→Artifact Mana-Loop Combo; Ozai→Burn
  Direct Damage (F4 holds); Tuvasa→Enchantress Value Engine; Baral→Countermagic
  Control; Kess→Consultation Storm Combo; K'rrik→Life-as-Resource Combo;
  Sokka→Ally Spellslinger Token Swarm (not counters).
- **Any Engine changes post-B1+B6?** 3 anchors identical to baseline; 14/17
  strategy *strings* reworded but **identity preserved in all**. Churn is from
  the v2 prompt change + LLM non-determinism, **not** B1+B6 — verified the one
  real shift (`abzanarmor`) is NOT in the B6 delta and has an unchanged elected
  set. No regressions.

### Key finding — R5 grounding (prompt v1→v2)

Production R5 enforcement (reject out-of-deck key_pieces) exposed a real gap the
agent-layer harness never caught (it only *notes*): with no card list in the
input, the model named plausible staples not in the specific deck (v1: 4
rejects incl. the Sokka anchor, whose *engine was correct* — only key_pieces
were ungrounded). **Fix:** added `in_deck_cards` roster to the input + tightened
R5 (`SYNTHESIS_VERSION=2`) → **0 rejects** on re-run. R5 is now
enforceable-and-satisfiable. Name canonicalisation also handles the
`name|SET|collector` suffix lake decks carry.

## Full-170 batch command

```bash
cd plugins/make-magic/pipeline
set -a; . ../.env; set +a          # AIRTABLE only; NO ANTHROPIC_API_KEY
uv run python scripts/extract_roleb_inputs.py --gauntlet      # refresh inputs (no LLM)
uv run python scripts/materialize_engines.py --gauntlet \
    --model claude-sonnet-4-5 \
    --outputs subagents/7.4-materialization/gauntlet_outputs.jsonl
uv run python scripts/roleb_measure.py subagents/7.4-materialization/gauntlet_outputs.jsonl
```

Sequential, idempotent, resumable (skips already-fresh). Est. ~170 × $0.18 ≈
**$30** (sonnet) / ~$11 (haiku). **Not run here** — the 17-deck sample (incl.
all anchors) is the required proof; the $30 full sweep is left for an explicit
ops decision. Runs one-at-a-time; no detached fan-out.

## P0 reproduces — YES

All three frozen SHAs reproduce at HEAD `a398705`: engine `afcffc8a`, resolve
`3010be2a`, full-corpus `4acf0f41` (lake-gated, ran green). Frozen path
untouched (new modules are Tier-2, not imported by it). Full strategy suite:
**580 passed, 4 skipped**.

## ADR

Resolves ADR 0021 OPEN item **(B.3)** (role-B production wiring / materialization
+ reader). On commit, update ADR 0021's B.3 line to "wired" (composition map
still says `role-B (ADR TBD at build)`); a dedicated role-B ADR citing these
code paths is optional. I did NOT edit the ADR (no-commit scope).

## Guardrails honored

NO `ANTHROPIC_API_KEY` (verified unset before/after `. ../.env`); no Gmail; no
commit; no destructive git; sequential `claude -p` (no fan-out); generic (no
deck-specific logic — name canonicalisation + basic-land filter are format
rules). Artifacts in `subagents/7.4-materialization/` + `research/`.

## Confidence: HIGH

`claude -p` self-synthesis proven end-to-end on 17 decks incl. all anchors; cache
+ reader + driver tested (33 unit tests) and idempotent; re-validation 7/7 RIGHT
on the final B1+B6 engine; P0 fully reproduces. One judgment call worth the
orchestrator's eye: the v2 `in_deck_cards` prompt change (bumped SYNTHESIS_VERSION
to 2) — it's the correct production bar for R5, but it does feed the model the
decklist, which slightly shifts role-B from purely graph-output-driven. The
engine *strategy* still derives from the graph; only *key_pieces* grounding uses
the roster.

## Artifacts

- `subagents/7.4-materialization/sample_outputs.jsonl` — 17 v2 engines
- `…/revalidation_table.txt` — measure harness output (7/7 RIGHT)
- `…/baseline_comparison.txt` — pre vs post B1+B6 strategies
- `…/materialize_run_v2.log`, `…/engines_v1_stale.jsonl` (archived v1), `…/baseline/`
- `research/roleb-materialization.md` — full write-up
