# Refine Methodology — synergy card-discovery, ranking, and swap-pairing

`card-evaluation.md` answers "is *this* card good?" (the card is supplied).
**This** rubric answers "*which* cards should I consider, ranked, and what do I cut for
them?" — the discovery + ranking + swap-pairing step that has no standalone artifact
otherwise. It **orchestrates** `card-evaluation.md`'s two-axis judgment as a subroutine;
it does not restate it.

Inputs: a deck + its **Strategy** (wants / does-not-wants / optional budget) + its
**Assessment** (from assessing-decks). Output: a ranked **candidate list** where each
entry is a concrete **swap** (add ↔ cut) with separated synergy and flexibility
rationales.

<reference file="../../building-decks/references/card-evaluation.md">
card-evaluation.md — the two-axis judgment this rubric orchestrates: **synergy**
(bucket→strategy overlap via `BUCKET_STRATEGY_SYNONYMS` + oracle patterns, ≥8/≥5/≥3
tiers) and the independent **flexibility** test (multi-quadrant vs only-when-winning).
Keep the two reasons SEPARATE — this rubric never collapses them.
</reference>

<reference file="../../building-decks/references/strategy-schema.md">
strategy-schema.md — the Strategy convention. The `What doesn't fit:` line is the
DOES-NOT-WANT list this rubric applies as a HARD pre-filter (step 2). The `Archetype:`
line frames staple selection (step 2).
</reference>

## The DOES-NOT-WANT invariant (governs every step)

The Strategy's `What doesn't fit:` line is a **hard pre-filter**, not a tiebreaker. A
card matching a stated DOES-NOT-WANT is **removed from the candidate pool before ranking
begins** — it never appears in the output, no matter how high it would score. Color
identity is the other hard filter (Commander rules: every card inside the commander's
identity). Both cut *before* scoring; scoring only ranks what survives.

---

## Step 1 — Gap derivation (Assessment → roles to fill)

Turn the Assessment into concrete **needs**, not cards yet. From the Assessment's
per-quadrant plan, loss-condition, and the three focus axes, derive a ranked list of
**roles**:
- **Under-filled focus buckets** — a `Focus Otags` item the actual cards don't back up
  (the Assessment's coverage-of-focus flags).
- **Susceptibility holes** — each count-cited weakness signal (0 counterspells, no
  board-wipe recursion, undefended payoff) → a role (interaction / protection /
  recursion).
- **Curve dead-zones** — a gap in `shape.cmc_histogram` the plan needs filled.
- **Only-when-winning clusters** — cards the flexibility test flags as live only when
  ahead; the role is "a more flexible card at this slot."

**Output of this step:** a ranked list of **roles**, each tagged with its target quadrant
/ focus item and (where relevant) its CMC slot. No card names yet.

## Step 2 — Candidate generation (discovery)

For each role, cast a **wide** net — recall over precision here; ranking narrows later.
Discovery is a **real cross-Magic functional query**, not model memory. The old
per-set `tag-set` channel was not discovery: it required already knowing which set to
tag, so generation quietly fell back to whatever cards the model happened to remember.
That anti-pattern is retired. Three channels, in order:

### Channel A — functional Scryfall search (PRIMARY)

Build the query with the **pure** `build_discovery_query` helper (`card_tagger.py`) and
run it through the existing `scryfall_cache.py search` wrapper. The helper maps each
role's buckets → Scryfall functional-search fragments via the **`BUCKET_TO_SCRYFALL_OTAG`**
map (sibling to `BUCKET_STRATEGY_SYNONYMS`, same module), OR-joins them, and pins
`f:commander` + `id<=<colors>` so the returned pool is already **in color identity and
format-legal** — the wide net, cross-Magic, selected by FUNCTION rather than by memory.

```bash
# 1. Build the query offline (PURE — no network). Role: removal at the 0-3 slot in Selesnya.
QUERY=$(uv run --with typer --with-editable ${CLAUDE_PLUGIN_ROOT}/pipeline \
  python -c 'import sys; sys.path.insert(0, "'${CLAUDE_PLUGIN_ROOT}'/scripts"); \
  import card_tagger; print(card_tagger.build_discovery_query("wg", ["removal"], cmc_max=3))')
# -> id<=wg f:commander (otag:removal) cmc<=3

# 2. Run it against Scryfall (the in-identity, legal, functional pool for the role).
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_cache.py search "$QUERY"
```

`build_discovery_query(color_identity, buckets, *, cmc_max=None, extra=None)` always
emits `f:commander`; uses `id<=<colors>` for identity (empty colors → `id<=c`,
colorless); OR-joins the mapped fragments for the given buckets; appends `cmc<=<n>` when
given. An **unknown bucket raises** (`KeyError`) and **no buckets raises** (`ValueError`)
— a discovery gap fails loudly, never a silent drop. Each search result carries the card's
name/type/oracle/color identity — the pool step 3 scores.

#### Bucketless roles (a role with NO matching bucket) — query oracle text directly

Step 1 can legitimately derive a role that `BUCKET_TO_SCRYFALL_OTAG` has **no bucket
for** — e.g. `recursion`, `reanimation`, `graveyard`, `lifegain`, `mill`, `landfall`.
`build_discovery_query([..., 'recursion'])` **raises `KeyError`** for these (that's the
loud-fail above — the query cannot even be BUILT, which the degradation contract's
"search returned empty/errored" path does NOT cover). This is distinct from *Going
surgical* below (which is for a line FINER than an existing bucket): here there is no
bucket at all.

**Rule:** if a derived role has no matching bucket, do NOT force it through
`build_discovery_query`. Query it **directly with an oracle-text `search`**, then
tag-verify the results as usual (Step 2 verification). Examples:

```bash
# recursion → cards that return things from your graveyard
search 'id<=bg f:commander o:"return target" o:"from your graveyard"'
# reanimation → return a creature card from a graveyard to the battlefield
search 'id<=b f:commander o:"return target creature card" o:"to the battlefield"'
# lifegain → whenever you gain life
search 'id<=wb f:commander o:"whenever you gain life"'
# landfall
search 'id<=g f:commander o:"landfall"'
```

Build the `id<=<colors> f:commander` prefix yourself (same identity/format pins the
helper would add) and hang the oracle-text clauses off it. This is a first-class
Channel-A path for bucketless roles — deterministic, no dependence on a tag existing.
(Do NOT invent an `otag:` for these — `otag:recursion` is DEAD.)

#### Going surgical (finer than a bucket)

Buckets are **rolled-up** (our `combat` bucket folds double-strike, evasion, equipment,
attack-triggers into one). When a deck's line is more surgical than a bucket, do NOT settle
for the whole bucket — narrow to the exact tag. Two safe ways, in order of preference:

1. **Query one specific crosswalk ROOT.** `BUCKET_TO_SCRYFALL_OTAG` is *derived* from the
   crosswalk's `BUCKET_ROOTS` (`pipeline/transforms/crosswalk.py`), and every root is a
   live Scryfall `otag:` **by construction** (our otag vocabulary IS Scryfall's tagger
   vocabulary). So read the roots for the bucket and pass the specific one via `extra=`, or
   just hand it to `search`. E.g. the `combat` bucket's roots include `gives-double-strike`,
   `gives-evasion`, `gives-trample`; for a double-strike voltron line run
   `search 'id<=rw f:commander otag:gives-double-strike'` — a real 140-card pool, not the
   whole combat bucket. This is safe because the root is guaranteed live.
2. **Oracle text for anything the tagger doesn't cover.** `search 'id<=u f:commander
   o:"whenever you cast your second spell"'` — deterministic, no dependence on a tag existing.

**Do NOT guess `otag:` slugs from English** (`otag:double-strike`, `otag:creates-tokens`
are DEAD — return nothing, which reads as "no such cards" and silently starves the role).
Only use `otag:` slugs that come from the crosswalk `BUCKET_ROOTS` (guaranteed live) or that
you've count-checked returns cards; otherwise use oracle text.

### Channel B — EDHREC curation (OPTIONAL, degradable)

If reachable, fetch the commander's EDHREC page JSON to **rerank/enrich** channel A with
"what actually fits this archetype" (its high-synergy / top-cards lists):

```bash
# slug = commander name lowercased, non-alphanumerics → hyphens (e.g. "atraxa-praetors-voice")
curl -sf "https://json.edhrec.com/pages/commanders/<slug>.json" \
  || echo '{}'   # DEGRADE SILENTLY — A + C still ship a full result
```

This channel is **OPTIONAL**. If the endpoint is unreachable, 404s, or the slug doesn't
resolve, **drop it silently** and proceed on A + C — never block or warn the user over a
missing enrichment. Use its lists only to *reorder/boost* channel-A candidates, not to
introduce un-verified names.

### Channel C — synergy-adjacency

`search` on the **shared otags of the deck's own highest-synergy existing cards** — the
deck's best pieces name what "more of this" looks like. Tag the anchor cards
(`card_tagger.py tag-card "<name>"`), take their common buckets, and run those through
`build_discovery_query` for the same identity. This channel also serves as the **fallback
floor** when channel A is unavailable (see below).

### Hard pre-filters (applied HERE, before anything is scored)

Color identity is **native to the query** (`id<=<colors>`), so channel-A results are
already in-identity. Then drop every candidate matching a **DOES-NOT-WANT**. What survives
is the pool to rank. Both cuts happen *before* step 3 — scoring only ranks survivors.

### Degradation contract (the anti-pattern this step fixes)

**Never silently generate candidates from model memory.** This is the core failure the
operationalized discovery replaces: if `search` returns empty or errors, **surface the
limitation explicitly** ("Scryfall discovery unavailable / empty for role X — results are
narrowed") and fall back to **Channel C + the archetype-staples prose**, in that order —
NOT to remembered card lists. Any candidate name that does enter ranking from memory or
from EDHREC MUST first be **tag-verified** (`card_tagger.py tag-card "<name>"` resolves it
and returns real buckets) before it is scored; a name that fails to resolve is dropped, not
ranked. A wide-but-honest pool beats a rich-but-hallucinated one.

## Step 3 — Scoring & ranking (two-axis, + conditional budget axis)

Run each surviving candidate through `card-evaluation.md`'s judgment. Keep the axes
separate:

- **Synergy axis** — bucket→strategy overlap (via `BUCKET_STRATEGY_SYNONYMS`) + oracle
  patterns, scored on the ≥8 / ≥5 / ≥3 confidence tiers. Answers "does it serve the
  Strategy's wants?"
- **Flexibility axis** (independent) — the multi-quadrant vs only-when-winning test.
  Read from the neutral facts (keyword census, type line, instant-speed). Multi-quadrant
  = prize; only-when-winning = trap. This AUGMENTS synergy, never replaces it: a flexible
  card with no synergy is still not a recommendation.
- **Budget axis — CONDITIONAL.** Active **only** when the Strategy captured a budget
  constraint (distilling-strategy). When on: fetch each candidate's live price
  (`scryfall_cache.py get-card`) and score price-fit against the ceiling — a candidate
  over a per-card ceiling is filtered like a DOES-NOT-WANT; within-budget candidates rank
  on price as a third axis. **When the Strategy has no budget, this axis is off** — do
  not fetch prices, do not rank on cost.

Rank candidates **within each role**. Keep the synergy reason and the flexibility reason
as separate strings for the output — never one opaque verdict.

## Step 4 — Swap pairing (add ↔ cut, size-preserving)

Every recommendation is a concrete **swap**, not a bare "add." For each top-ranked add,
pair it against the **weakest same-role / same-CMC-slot incumbent**:
- "Same role" = the quadrant/focus role it fills; "same CMC slot" = the 0-2 / 3-4 / 5-6 /
  7+ bracket.
- Prefer to cut a card that is **live in fewer game-states** (an only-when-winning
  incumbent) — do not cut the deck's only answer for a game-state it's already thin on.
- Compute the **net deltas**: `synergy_delta` (add's synergy − cut's synergy) and
  `flexibility_delta` (add's flexibility − cut's), and — when the budget axis is on —
  `price_delta` (add price − cut price).

This is **size-preserving** by construction: one add for one cut. The commit deck-size
guard is SHRINK-ONLY — it refuses shrinking an at-target deck below target, but it does
NOT block a write that GROWS the deck (an add without a cut sails through), and building
up from a skeleton is fine. So size-preservation is enforced by the PAIRING, not the
guard: `apply_swaps` folds exactly one add per cut into the working deck, so a swap never
grows or shrinks the deck. Do not rely on the guard to catch an unpaired add.

## Step 5 — Output contract

Each candidate is a swap object with these fields (this shape feeds the orchestrator's
sim-depth proposals and is persisted as `draft.candidates[]`):

```
{
  add:               <incoming card name>,
  cut:               <outgoing card name>,          # the paired weakest incumbent
  role_filled:       <the quadrant/focus role from step 1>,
  synergy_reason:    <why the add serves the Strategy's wants — SEPARATE from flexibility>,
  flexibility_reason:<which game-states the add is live in — SEPARATE from synergy>,
  synergy_delta:     <add.synergy − cut.synergy>,   # net, may be negative
  flexibility_delta: <add.flexibility − cut.flexibility>,
  confidence:        <very-high | high | medium | low, from the synergy tiers>,
  price_delta:       <add.price − cut.price>         # OPTIONAL — present only when budget axis is on
}
```

The list is ranked (best swap first). It is a list of **recommendations** — this rubric
deliberately does **NOT** acquire cards, resolve availability (#5), or touch schema.
Persistence (COMMIT) and acquisition are downstream.

## Delegating bulk evaluation (keep the driver's context lean)

Step 2 can surface a large candidate pool. **Delegate the bulk scoring pass to a
context-isolated subagent** (drift-control item 7): hand it the survived pool + the
Strategy + the role list, and have it return a distilled `{keep, cut, rationale}` per
candidate — NOT the full tag dumps and price JSON. The driver keeps only the distilled
verdicts, writes them into `draft.candidates`, and never bloats its own window with the
raw evaluation traffic. The driver *is* the machine; the subagent is a pure evaluator
that returns a summary.

## Deliberately NOT

- **Acquire cards or resolve availability** (#5) — output is recommendations only.
- **Collapse synergy and flexibility** into one score — always two separate reasons.
- **Rank on price when no budget was set** — the budget axis is conditional, off by
  default.
- **Change deck size** — every recommendation is a size-preserving swap.
- **Bypass a DOES-NOT-WANT** — it is a hard pre-filter, never overridden by a high score.
