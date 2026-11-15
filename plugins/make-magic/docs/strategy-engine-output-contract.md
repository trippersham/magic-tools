# Strategy Engine — Output Contract

> Consumer-facing contract for the make-magic strategy engine.
> Covers what you get, how it's produced, its guarantees, and its failure modes.

| Field | Value |
|---|---|
| Status | **Production** (2026-11-15). Branch `feat/strategy-engine`, HEAD `8aad1c2`. |
| Governing ADRs | [0021](adr/0021-pipeline-superstructure-card-strategy-engine.md) (superstructure), [0014](adr/0014-engine-mass-election.md) (election), [0002](adr/0002-substantiveness-generic-evaluator.md) / [0020](adr/0020-event-substantiveness-producer-anchor.md) (substantiveness), [0022](adr/0022-concept-anchored-derived-adjacency.md) (adjacency) |
| P0 SHAs | engine `afcffc8a` · resolve `3010be2a` · full-corpus `4acf0f41` |

---

## Two tiers

The strategy engine resolves a deck at two tiers. **Tier 1 is always available and
has no external dependency.** Tier 2 enriches Tier 1 with a synthesised Engine.
Consumers MUST treat them as separate: Tier 1 is the deterministic foundation;
Tier 2 is a cached enrichment whose resolution depends on the `claude` CLI.

### Tier 1 — Deterministic archetype identity (frozen, P0-pinned)

**Entry points:** `compute_facts(deck, combos, assets)` and `resolve(deck)`.

**Output:**

| Field | Type | Description |
|---|---|---|
| `archetype_identity` | `list[dict]` | Ranked set of earned archetypes for the deck, each with name, confidence, and whether it is substantive at the archetype grain (ADR 0002). Ordered by centrality (ADR 0014). |
| `engines.engine_isolation.elected` | `list[dict]` | The elected engine-candidate set — strategy sub-units that cleared substantiveness gates at all grains (attribute 0004, event 0020, archetype 0002) and survived the mass-election ranking (0014). A set, not a singleton. |
| `core_combos` | `list[dict]` | Detected combos (Spellbook-sourced), each with `variant_id`, `label`, `result`, `requires`, `n_pieces`, `win`, `win_kind`. |
| `named_strategy` | `str` | The naming-layer resolution (human-readable strategy name). |
| `combo_override_applied` | `bool` | Whether a winning combo overrode the archetype-derived name. |

**Guarantees:**

- **Byte-deterministic.** Given the same deck input and the same engine-logic version, `compute_facts` and `resolve` produce byte-identical output. This is enforced by SHA-pinned P0 baselines (ADR 0003) that every CI run verifies.
- **Pure Python, no network.** Tier 1 requires only the hydrated DuckDB lake (card data + otag data) and the curated strategy assets shipped with the package. No LLM, no API key, no ambient auth.
- **P0 SHAs:** engine `afcffc8a`, resolve `3010be2a`, full-corpus `4acf0f41`. These reproduce from the live lake via `engine_parity_oracle.full_corpus_sha()`. Any code change that moves a SHA is a proof-carrying re-baseline (ADR 0003).

**Pipeline flow (Tier 1):**

```
deck + combos + assets
  → compute_facts()              # substantiveness at all grains → election → ranked candidates
    → _isolate_engines()         # mass election (0014) + B6 archetype-substantiveness gate
  → name_deck()                  # naming-layer resolution
  → archetype_identity + elected set + named_strategy
```

### Tier 2 — Resolved Engine (cached, provisional)

**Entry point:** `engine_synthesis.resolve_engine(deck, combos, ...)`.

**Output (the `ResolvedEngine` dataclass):**

| Field | Type | Description |
|---|---|---|
| `strategy` | `str` | A 2-5 word label for the deck's core engine (e.g. "Artifact Mana-Loop Combo", "Enchantress Value Engine"). Never empty (R4 guarantee). |
| `win_condition` | `str` | How the deck wins (e.g. "Infinite mana into draw/drain outlets"). |
| `key_pieces` | `list[str]` | The 3-8 most important cards for the engine, guaranteed to be in the deck (R5 in-deck check). |
| `confidence` | `str` | `HIGH`, `MED`, or `LOW`. |

**Return shape:** `(engine: ResolvedEngine | None, meta: dict)`.

`meta['status']` tells you what happened:

| Status | Meaning | `engine` |
|---|---|---|
| `hit` | Fresh cached Engine returned. No LLM call made. | Present |
| `synthesized` | Cache miss. Backend produced a valid Engine (now cached). | Present |
| `miss` | Cache miss and synthesis is disabled (`allow_synth=False`) or no backend provided. | `None` |
| `error` | Synthesis attempted but failed (malformed output, out-of-deck key_pieces, timeout). | `None` |

---

## Runtime dependency

| Tier | Dependency | Notes |
|---|---|---|
| Tier 1 | Pure Python + hydrated DuckDB lake | No network, no LLM, no API key. |
| Tier 2 (cache hit) | Pure Python + `~/.local/share/make-magic/engine_cache/engines.jsonl` | Same as Tier 1 plus the local JSONL cache file. No LLM. |
| Tier 2 (cache miss) | `claude` CLI under ambient auth | Shells `claude -p --output-format json --model <model_id>`. No `ANTHROPIC_API_KEY` needed or used. The `claude` binary must be on `$PATH`. |

**If the `claude` CLI is unavailable**, Tier 2 degrades to `status='miss'` with `engine=None`. Tier 1 is unaffected. Consumers that need only the archetype identity and elected set can ignore Tier 2 entirely.

---

## Model choice

The synthesis backend requires an **explicit model id** passed to `ClaudeCliBackend(model=...)` or the `--model` flag of the materialization driver. Bare aliases (e.g. `sonnet`) are NOT reliably recognized and silently fall through to the session default. Use the explicit id.

| Model id | Quality | Cost/deck | Notes |
|---|---|---|---|
| `claude-sonnet-4-5` | Higher quality. Default. | ~$0.18 | Recommended for production materialization. |
| `haiku` / `claude-haiku-4-5` | Adequate for most decks. | ~$0.06 | Cost-validated on 17-deck sample. |

The model id is recorded in each cache record (`model_id` field) for traceability.

---

## Staleness invalidation (cache freshness)

A cached Engine is **fresh** if and only if BOTH conditions hold:

1. **`input_hash` matches.** A SHA-256 over the canonical role-B input (the deterministic output of `compute_facts` + `name_deck`). Because the input carries the full elected set, detected combos, and archetype identity, any engine-logic change (a new curated edge, a B1 combo-edge update, a B6 gate change, a deck edit) flows through to a different hash and **auto-invalidates** the cache. No manual version bump is needed for engine-logic changes.

2. **`synthesis_version` matches.** Bumped by hand only when the synthesis prompt/rules change (`roleb_prompt.md`). The current version is **2** (added `in_deck_cards` roster + strengthened R5 grounding).

**Implication:** adding a card to a deck, changing a curated edge, or updating any upstream engine logic automatically invalidates the cache for affected decks. Only a prompt rewrite requires a `SYNTHESIS_VERSION` bump.

---

## Graceful degradation

The engine is designed to never crash on unusual input:

- **Unknown mechanic / sparse deck:** The synthesis prompt instructs the model to ground in what it sees. On the 20-deck external validation (decks the engine never trained on, including 3 precons and a 19-card stub), the model produced defensible engines with `LOW` confidence rather than errors. The stub deck (19 cards, `welcoming-vampire`) grounded to "Small Creature Draw" LOW with 4 real cards.
- **No combos detected:** The engine falls back to the elected archetype candidates and commander identity. The `named_strategy` and `archetype_identity` (Tier 1) are always present.
- **Synthesis failure / timeout:** `resolve_engine` returns `(None, {'status': 'error', ...})`. The error is logged; the consumer can fall back to Tier 1.
- **Empty elected set:** Possible for decks with no substantive strategy sub-graph (e.g. a pile of good-stuff with no synergy). The naming layer produces a "defensible" fallback via the never-empty waterfall. Tier 2 synthesis handles this by grounding on the commander and card list.

**Validation record:** 187-deck full-corpus (7/7 B-test anchors RIGHT, 0 wrong, 0 empty, 0 genuine F4 violations). 20-deck external validation (fresh decks, 0 failures). 17-deck materialization sample (0 rejects, 0 failures, idempotency proven).

---

## Cache location and format

| Item | Value |
|---|---|
| Path | `~/.local/share/make-magic/engine_cache/engines.jsonl` (XDG data dir, same home as machine-vocab + DuckDB lakes) |
| Format | JSONL — one JSON object per line, human-inspectable, grep-able |
| Record fields | `slug`, `deck_name`, `input_hash`, `synthesis_version`, `engine` (the 4-field `ResolvedEngine`), `model_id`, `synthesized_at` (ISO-8601 UTC), `extraction_version`, `derivation_version` |
| Idempotency | `persist` / `persist_batch` deduplicate on `slug` (last-write-wins). A re-run is a no-op for already-fresh decks. |

The cache is a **Tier-2 provisional** artifact. It is never on the frozen P0 deterministic path and curated-base tests never consult it.

---

## Materialization driver

`scripts/materialize_engines.py` iterates decks, computes staleness keys, skips cache hits, synthesises misses via `claude -p`, validates, and persists. Sequential, idempotent, resumable.

Key flags: `--fixtures` (17 golden fixtures, lake-free), `--gauntlet` (170 lake-backed decks), `--slugs a,b` (allow-list), `--model` (explicit model id), `--dry-run` (extract + hash only, no LLM).

---

## Code paths (module index)

| Module | Role | Tier |
|---|---|---|
| `pipeline.transforms.strategy.facts` | `compute_facts`, `_isolate_engines` — the deterministic Tier-1 path | 1 |
| `pipeline.transforms.strategy.naming` | `name_deck` — naming-layer resolution | 1 |
| `pipeline.transforms.strategy.engine_synthesis` | `resolve_engine`, `build_synthesis_input`, backends, parse/validate | 2 |
| `pipeline.transforms.strategy.engine_cache` | `ResolvedEngine`, `EngineCacheRecord`, cache store, staleness key | 2 |
| `pipeline.transforms.strategy.roleb_prompt.md` | Vendored synthesis rules (v2) | 2 |
| `scripts/materialize_engines.py` | Batch materialization driver | 2 |
| `scripts/extract_roleb_inputs.py` | Role-B input extraction tool (delegates to `build_synthesis_input`) | 2 |
