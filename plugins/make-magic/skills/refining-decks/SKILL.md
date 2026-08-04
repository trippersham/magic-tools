---
name: refining-decks
description: >
  Deep synergy card-discovery for a Commander deck — turn an Assessment into ranked,
  swap-paired upgrade candidates that honor the deck's does-not-wants. TRIGGER when: user
  asks "what should I add to [deck]", "find upgrades for [deck]", "recommend cards for
  [deck]", "what fills [deck]'s gaps", "propose swaps for [deck]", "what should I add
  from [set]", "shore up my [quadrant]". Also trigger as the REFINE step of a
  building-decks session. SKIP for single-card "is X good here" evaluation, whole-deck
  balance diagnosis (that's assessing-decks, which this CONSUMES), or empirical win-rate
  testing (that's simulating-games).
user-invocable: true
---

# Refining Decks

Discovery + ranking + swap-pairing: given a deck, its **Strategy**, and its
**Assessment**, produce a ranked list of concrete **swaps** (add ↔ cut) — each with a
separated synergy reason and flexibility reason — that plug the Assessment's gaps and
**honor the deck's stated does-not-wants as a hard filter**. This skill answers "*which*
cards, ranked, and what do I cut for them?" — the step `card-evaluation.md` (single-card)
doesn't cover.

<primary-constraint>
**The Strategy's DOES-NOT-WANTs are a HARD pre-filter, applied before ranking.**

Why: refining is where a deck silently drifts toward generic goodstuff. The user stated
what the deck should NOT do (`What doesn't fit:` in the Strategy) precisely so that a
high-scoring-but-off-axis card never sneaks in. A card matching a DOES-NOT-WANT is
removed from the candidate pool **before** anything is scored — it never appears in the
output, no matter how well it would rank. Color identity is the other hard filter. Both
cut before scoring; scoring only ranks what survives.

Instead: read the Strategy's `What doesn't fit:` line first, and treat it (plus color
identity) as a pool filter — not a tiebreaker.
</primary-constraint>

<red-flags>
If you catch yourself about to:
- **Rank a card that violates a DOES-NOT-WANT because it scored high** — STOP. The
  does-not-want cut happens BEFORE scoring. It never enters the ranking.
- **Recommend outside the deck's color identity** — STOP. Hard Commander rule; filter
  against the deck's identity.
- **Present a bare "add X" with no cut** — STOP. Every recommendation is a
  size-preserving SWAP (add ↔ weakest same-role/CMC incumbent). The commit deck-size
  guard is SHRINK-ONLY — it refuses shrinking an at-target deck below target, but an
  add-without-cut GROWS the deck and is NOT blocked. So swaps must be size-preserving by
  construction: `apply_swaps` enforces one add per cut, and building up from a skeleton
  is fine (grow-only never trips the guard). The guard is not the backstop for a botched
  swap — the one-add-per-cut pairing is.
- **Rank candidates on price when no budget was set** — STOP. The budget axis is
  conditional — off unless distilling-strategy captured a budget. Don't fetch prices or
  rank on cost otherwise.
- **Collapse synergy and flexibility into one verdict** — STOP. They are two separate
  reasons in the output, always.
- **Dump the whole tagged pool into your own context** — STOP. Delegate bulk scoring to
  a context-isolated subagent; keep only the distilled `{keep, cut, rationale}`.
</red-flags>

## What you produce

A ranked **candidate list** where each entry is a swap object:

```
{ add, cut, role_filled, synergy_reason, flexibility_reason,
  synergy_delta, flexibility_delta, confidence, price_delta? }
```

`price_delta` is present **only** when the Strategy carried a budget constraint. The list
is ranked best-swap-first and is a set of **recommendations** — this skill does not
acquire cards, resolve availability, or persist to the deck; that's downstream (COMMIT).

The full rubric — gap derivation → candidate generation → scoring/ranking → swap pairing
→ output contract — lives in the reference. Read it before you refine:

<reference file="references/refine-methodology.md">
refine-methodology.md — the five-step §4 rubric: (1) gap derivation (Assessment →
roles), (2) candidate generation via a REAL functional Scryfall query — channel A
(`build_discovery_query` + `scryfall_cache.py search`, keyed on `BUCKET_TO_SCRYFALL_OTAG`),
channel B (optional EDHREC curation, degrades silently), channel C (synergy-adjacency);
color identity native to the query + DOES-NOT-WANT as HARD pre-filters; NEVER fall back to
unverified memory, (3) two-axis synergy+flexibility scoring + conditional budget axis, (4)
size-preserving swap pairing with net deltas, (5) the exact output contract. It
orchestrates `card-evaluation.md` as a subroutine.
</reference>

## The data surface

Two surfaces:

- **The `collection` CLI** — reads the deck, Strategy, Assessment, inventory, chase (the
  backend-agnostic wrapper, local YAML or Airtable):
  ```bash
  ${CLAUDE_PLUGIN_ROOT}/scripts/collection status
  ${CLAUDE_PLUGIN_ROOT}/scripts/collection get-deck "<deck>"   # strategy + assessment + focus_otags + cards[]
  ${CLAUDE_PLUGIN_ROOT}/scripts/collection list-inventory
  ${CLAUDE_PLUGIN_ROOT}/scripts/collection list-chase
  ```
- **The tagger + Scryfall scripts** — for discovery, tag-verification, and (conditional)
  pricing:
  ```bash
  # Channel A discovery (step 2): build a functional query (PURE), then search it live.
  QUERY=$(uv run --with typer --with-editable ${CLAUDE_PLUGIN_ROOT}/pipeline python -c \
    'import sys; sys.path.insert(0, "'${CLAUDE_PLUGIN_ROOT}'/scripts"); \
     import card_tagger; print(card_tagger.build_discovery_query("wg", ["removal"], cmc_max=3))')
  uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_cache.py search "$QUERY"

  uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/card_tagger.py tag-card "<name>"   # tag-verify a name before ranking it
  uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_cache.py get-card "<name>"  # live price (budget axis only)
  ```
  `search` returns the real, in-identity, format-legal, functional pool you rank in step 2
  — each card carrying name/type/cmc/color_identity/oracle_text. `tag-card` is how you
  **tag-verify** any name sourced from EDHREC or memory before it enters ranking (an
  unresolved name is dropped, never ranked). Price is fetched from `scryfall_cache.py`
  only when the budget axis is on.

`BUCKET_STRATEGY_SYNONYMS` and its sibling **`BUCKET_TO_SCRYFALL_OTAG`** live in
`card_tagger.py`. The former maps each otag bucket → the strategy keywords it satisfies
(used by scoring); the latter maps each bucket → the Scryfall `otag:`/`o:` search
fragments `build_discovery_query` assembles for channel-A discovery.

## Prerequisites

- **uv** — the CLI and tagger scripts run via `uv run`.
- **A deck with a Strategy AND an Assessment.** Refining consumes both. No Assessment →
  run **assessing-decks** first. No Strategy → **distilling-strategy** first.

## Two run modes

- **Standalone** — the user asks for upgrades. You load the committed Strategy +
  Assessment from the deck (via the CLI), run the rubric, and present the ranked swaps.
  Standalone, you produce recommendations only; persisting a swap is the user's call
  (and is a building-decks / managing-inventory action, not this skill's).
- **Under building-decks (the REFINE state)** — you write the ranked candidates to
  `draft.candidates[]` (the output-contract shape above). Those feed the VALIDATE step's
  sim-depth proposals. If your candidate generation shows the deck's working list changed
  since ASSESS, the machine routes back to ASSESS to re-diagnose; if **no** candidate can
  honor a stated want, it routes back to FRAME (the want may be unfulfillable). You never
  write to the deck yourself. Your candidates are **proposals**: the ORCHESTRATOR applies
  the user-selected ones to `draft.working_deck` at the REFINE→VALIDATE boundary (via
  `deckbuild apply-swaps`), so VALIDATE and COMMIT act on the real proposed deck. COMMIT
  does NOT consume `draft.candidates` — it persists `draft.working_deck`; a candidate that
  was never applied never reaches the deck.
- **Under the orchestrator, if your output (`candidates`) is flagged in `draft.stale`**,
  you are being asked to RECOMPUTE them — re-rank against the current working deck, do not
  reuse the held candidates. The orchestrator clears the flag once you hand back the recompute.

## Workflow (thin wrapper over the rubric)

1. **Load context.** Read the deck, Strategy (esp. `What doesn't fit:` and any budget),
   and Assessment via the CLI. Announce the backend (`collection status`).
2. **Gap derivation** — turn the Assessment into a ranked list of **roles** to fill
   (refine-methodology §1).
3. **Candidate generation** — for each role, discover widely via the real functional
   query: **channel A** (`build_discovery_query` → `scryfall_cache.py search`, in-identity
   + legal by construction), **channel B** (optional EDHREC curation — degrade silently if
   unreachable), **channel C** (synergy-adjacency on your best cards' shared otags). Apply
   the **hard pre-filters** (color identity is native to the query; then DOES-NOT-WANT) to
   get the pool. If search is empty/errors, **surface the limitation and fall back to
   channel C + archetype staples — NEVER silently generate from memory**; tag-verify any
   memory- or EDHREC-sourced name (`tag-card`) before it enters ranking (refine-methodology §2).
4. **Delegate the bulk scoring pass.** Hand the survived pool + Strategy + role list to a
   **context-isolated subagent**; it returns a distilled `{keep, cut, rationale}` per
   candidate (two-axis synergy + flexibility, + conditional budget). Keep only the
   distilled verdicts — do not pull the raw tag/price traffic into your own window
   (refine-methodology "Delegating bulk evaluation").
5. **Swap pairing** — pair each kept add against the weakest same-role/same-CMC-slot
   incumbent; compute the net deltas; size-preserving (refine-methodology §4).
6. **Emit the ranked candidate list** in the output-contract shape. Standalone, present
   it; under the orchestrator, write it to `draft.candidates`.

## Output contract

A ranked list; each entry:
`{add, cut, role_filled, synergy_reason, flexibility_reason, synergy_delta,
flexibility_delta, confidence, price_delta?}` — `price_delta` only when the budget axis
is on. Recommendations only; no acquisition, no availability resolution, no deck write.

## When to use

- Finding ranked, swap-paired upgrades for a deck with a known Assessment.
- Set-release recommendations for a deck ("what should I add from [set]?").
- The REFINE step of a building-decks session.

## When NOT to use

- **No Assessment yet** — run assessing-decks first; refining consumes the Assessment's
  gap list. **No Strategy** — distilling-strategy first (you need the does-not-wants).
- **Single-card "is X good here?"** — that's a direct card-evaluation (building-decks
  Operation 1), not a discovery pass.
- **Empirically confirming a swap** — that's simulating-games (a-posteriori); this skill
  is a-priori reasoning. The intended handoff: refine proposes → simulate confirms.
