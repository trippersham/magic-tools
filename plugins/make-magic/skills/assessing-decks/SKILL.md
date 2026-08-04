---
name: assessing-decks
description: >
  Diagnose a Commander deck's balance with Quadrant Theory — a reasoning-led pre-mortem
  over a neutral fact sheet. TRIGGER when: user asks "is my deck balanced", "diagnose
  [deck]", "what's [deck] missing", "what quadrant is weak", "what should I shore up",
  "is [deck] too glass-cannon", "where does [deck] fall apart", "assess [deck]". Also
  trigger as the ASSESS step of a building-decks session. SKIP for single-card
  evaluation, card discovery / upgrade suggestions (that's refining-decks), or empirical
  win-rate testing (that's simulating-games). This skill produces the Assessment those
  steps consume.
user-invocable: true
---

# Assessing Decks

Whole-deck **pre-mortem**: does the deck have a *plan* for every game-state it needs,
given its archetype? This is a **reasoning task over a neutral fact sheet**, not a
card-scoring tally. The quadrants are questions about the deck's plan — Development
(don't fall behind), Parity (break a stall), Winning (what/how-fast/how-interruptible is
the actual win), Losing (the out when behind). You answer them by reasoning over the
deck's **Strategy** plus a **neutral fact sheet** from the `factsheet` verb. The output
is a reasoning-authored **`Assessment`** — a narrative pre-mortem plus a shopping list,
**never a percentage table**.

<primary-constraint>
**Never diagnose without reading the deck's Strategy first.**

Why: a quadrant is a contextual *role*, and role is emergent from the deck's plan. "Is
this deck thin on Losing?" is unanswerable without knowing the archetype — an aggro
deck legitimately runs thin on Losing (speed is the plan); a control deck that thin has
folded. The Strategy defines the expectations you bring to the pre-mortem. Diagnosing
without it produces generic "add more removal" advice that ignores what the deck is
trying to do.

Instead: always start by reading the Strategy via the backend-agnostic `collection` CLI
(`get-deck "<deck>" --field strategy`, or the whole deck). If the deck has no Strategy,
send the user to **distilling-strategy** first.
</primary-constraint>

<red-flags>
If you catch yourself about to:
- **Report a percentage bar chart** (`Winning 12%, Losing 30%`) — STOP. The
  card-scoring premise was retired (see quadrant-theory.md). The Assessment is a
  narrative pre-mortem, not a tally.
- **Diagnose from card memory** — STOP. Run the `factsheet` verb; reason over the actual
  `otag_buckets` + `susceptibility`, not a remembered decklist.
- **Read empty `otag_buckets` as "the deck does nothing"** — STOP. That's the otag layer
  being unavailable, OR a high-synergy deck invisible to buckets. Fall back to structured
  facts + Strategy.
- **Overwrite the Strategy** — STOP. Strategy is a human-authored INPUT you read; you
  write the `Assessment` (and may propose `Focus Otags`), never the Strategy.
- **Commit the Assessment mid-session under the orchestrator** — STOP. Under
  building-decks the Assessment is a draft, held until COMMIT. Standalone, you may offer
  to persist.
</red-flags>

## Strategy vs. Focus Otags vs. Assessment — keep them apart

Three distinct fields on the Decks table:

- **`Strategy` = what the deck AIMS to be.** Human-authored prose (win condition,
  archetype, key mechanics). An **input** you read, never overwrite.
- **`Focus Otags` = the otags/buckets the deck CARES about.** The intended functional
  identity in the tag vocabulary — a **curated subset** (the cards underneath carry a
  much wider set). Skill/human-authored; the pipeline READS it but never writes it. An
  input the diagnosis measures against.
- **`Assessment` = what the deck ACTUALLY is, isn't, and needs.** A reasoning SYNTHESIS
  *you* produce — the actual card otags measured AGAINST `Focus Otags` and Strategy. The
  **output** of this skill.

<reference file="../building-decks/references/quadrant-theory.md">
quadrant-theory.md — the pre-mortem method, the deterministic/reasoning split, the
Strategy / Focus Otags / Assessment triad, the `otag_buckets` + `susceptibility` lead
signals (the Assessment inputs), the actual-vs-focus signals (coverage_of_focus /
thin_focus / off_focus), the full fact-sheet field reference, and the limitations
(cEDH is out of scope; why the card-scoring premise was retired).
</reference>

## The data surface: the `collection` CLI

Every read and the Assessment/Focus-Otags writes go through the backend-agnostic
wrapper — identical in local YAML or Airtable:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection status                        # announce backend
${CLAUDE_PLUGIN_ROOT}/scripts/collection get-deck "<deck>"             # strategy + focus_otags + assessment + cards[]
${CLAUDE_PLUGIN_ROOT}/scripts/collection factsheet "<deck>"            # the neutral fact sheet
${CLAUDE_PLUGIN_ROOT}/scripts/collection list-inventory               # owned cards, for the shopping list
${CLAUDE_PLUGIN_ROOT}/scripts/collection list-chase                   # cards already on the chase list
${CLAUDE_PLUGIN_ROOT}/scripts/collection set-focus-otags "<deck>" tokens counters anthem
${CLAUDE_PLUGIN_ROOT}/scripts/collection set-assessment "<deck>" "<the pre-mortem synthesis>"
```
`set-focus-otags` / `set-assessment` forward to the `CollectionStore` port verbs
`set_focus_otags` / `set_assessment` — **the only write path.** Never raw Airtable CRUD.

## Prerequisites

- **uv** — the CLI and the fact-sheet engine run via `uv run`.
- **A populated backend** with the deck present and a Strategy filled (per
  `strategy-schema.md`). No Strategy → distilling-strategy first.

## Two run modes

- **Standalone** — the user asks to diagnose a deck. You load the committed Strategy +
  Focus Otags from the deck (via the CLI), produce the Assessment, present it, and —
  with approval — may **persist** it (`set-assessment`, and `set-focus-otags` if you
  proposed a focus).
- **Under building-decks (the ASSESS state)** — the Assessment is written to
  `draft.assessment`, HELD, not on the deck until COMMIT. If the working deck changed
  (REFINE looped back), you re-diagnose the working deck, not the committed one. Do not
  call `set-assessment` yourself in this mode — hand the Assessment back.
- **Under the orchestrator, if your output (`assessment`) is flagged in `draft.stale`**,
  you are being asked to RECOMPUTE it — re-diagnose from the current working deck, do not
  reuse the held value. The orchestrator clears the flag once you hand back the recompute.

---

## Diagnose workflow

**Step 1 — Read the deck's Strategy → archetype and win condition.**
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection get-deck "<deck>"
```
The JSON carries `strategy`, `focus_otags`, `assessment`, and `cards[]` together. The
`<primary-constraint>` applies — never diagnose without the Strategy. Extract the win
condition and the `Archetype:` line, which frames the per-game-state expectations (see
strategy-schema.md's archetype table).

**Step 2 — Use the exact current decklist.** The `cards[]` from Step 1 is the deck —
work from it, not from memory. (Under the orchestrator with a changed working deck,
diagnose the `working_deck`.)

**Step 3 — Run the fact sheet.**
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection factsheet "<deck>"
```
The `factsheet` verb reads the deck from the active backend and runs the offline
fact-sheet engine (the underlying `deck_factsheet.py factsheet <decklist>` is available
directly if you already have a decklist file). It emits **neutral facts only** — curve,
ramp/fixing, keyword census, card advantage, instant-speed, plus the two otag-derived
fields that carry the diagnosis:
- **`otag_buckets`** — a multi-label oracle-tag bucket → nonland-card count map
  (`removal`, `ramp`, `draw`, `tokens`, `counters`, `burn`, `tutor`, `sac`,
  `counterspells`, `flicker`, `typal`, `anthem`, `combat`, `protection`, …).
- **`susceptibility`** — data-grounded, count-cited weakness signals (e.g. "board wipes:
  N payoff cards, M sweepers, no recursion").

It assigns **no** quadrant, role, or wincon. Report any `missing` names — resolve or note
them. Optionally read the **mana curve** here (`shape.cmc_histogram`, `shape.avg_cmc`,
`shape.top_end_count`) for a curve-dead-zone read.

**Graceful degradation:** if the otag layer is unavailable, `otag_buckets` is `{}` and
`susceptibility` holds a single `otag layer unavailable: …` string. Fall back to the
structured facts (curve, ramp, instant-speed, keywords) and lean harder on the Strategy —
never treat empty buckets as "the deck does nothing."

**Step 3b — Read or propose the deck's `Focus Otags`.** It's in the Step-1 JSON as
`focus_otags` (or `get-deck "<deck>" --field focus_otags`). `Focus Otags` is the curated
set the deck is **built around** — its intended identity, distinct from the wide actual
set.
- **Already set** → use it as-is (the intent you measure against).
- **Empty** → propose one: read the fact sheet's `otag_buckets` (the wide actual set) +
  the Strategy's `Key mechanics`, then **curate down** to the handful the deck is
  genuinely built around (a tokens/counters go-wide deck's focus is `tokens counters
  anthem`, not the incidental `ramp`/`removal`). Present it; then write it (Step 3c). If
  you can't confidently curate one, proceed without it (the Assessment degrades — Step 7).

**Step 3c — Write `Focus Otags` (standalone; hand back under the orchestrator).**
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection set-focus-otags "<deck>" tokens counters anthem
```
One slug per argument. Never write it from the mechanical union of `otag_buckets` — that
makes it the actual set, not the intended one. Under the orchestrator, hand the proposed
focus back rather than writing it.

**Step 4 — Reason the per-quadrant plan (from facts + Strategy).** For each game-state,
state the deck's plan — from the engine, not a count. Lead with **`susceptibility`**
(the measurable resilience gaps, each count-cited) and the **`otag_buckets`**
distribution — together the measurable core of "Losing is the hardest quadrant." Read a
game-state's plan off the buckets that serve it, never a single count:
- **Development** — plan not to fall behind early? (`ramp` bucket, curve, early `removal`)
- **Parity** — plan to break a stall / grind ahead? (`draw`/`tutor` + payoff buckets)
- **Winning** — what *is* the win; is it resilient / fast / interruptible? (from Strategy)
- **Losing** — the out when behind / swept / raced? (`susceptibility` + `protection`/`removal`)

**When `coverage.uncategorized_pct` is high, weight the Strategy over the counts** — the
value is synergy-carried and invisible even to the buckets. Cite fact-sheet numbers as
supporting evidence, never as the verdict.

**Step 5 — Name the loss-condition.** State the game-state where, if the game goes there,
the deck loses — threat-relative to the pod, not a flat low bar. This is the pre-mortem's
payload.

**Step 6 — Prescription + owned fills (read-only).** Name the card **type** that plugs
the hole. Then read inventory + chase and filter in reasoning:
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection list-inventory
${CLAUDE_PLUGIN_ROOT}/scripts/collection list-chase
```
Keep only in-color-identity cards of the prescribed type that are owned/available; flag
any already on the chase list. (These are the concrete *needs* refining-decks turns into
ranked candidates.)

**Step 7 — Synthesize the Assessment** (narrative + shopping list, NOT a percentage
table). Reason the fact-sheet inputs (`susceptibility` + `otag_buckets`, the wide actual
set) against **both** the `Strategy` (prose aim) **and** `Focus Otags` (intended
identity) along three axes:
- **Coverage of focus** — for each `Focus Otags` item, does the actual card set back it
  up? A focus item with few cards behind it is intent the deck hasn't paid for.
- **Thin / unprotected focus** — a focus item present but shallow, or a payoff the deck
  cares about with no protection defending it (cross-reference `susceptibility`).
- **Off-focus noise** — prominent actual buckets OUTSIDE `Focus Otags`: mechanical weight
  spent on things the deck doesn't claim to care about.

Use this shape:

```
## [Deck] — Quadrant Pre-Mortem (archetype: X · synergy-driven: low|med|high from coverage%)
Focus: <the deck's Focus Otags, e.g. tokens · counters · anthem>
Buckets: <top otag_buckets (actual), e.g. tokens 8 · counters 5 · ramp 6 · removal 4>
Coverage of focus: <each focus item → actual count; flag items the cards don't back up>
Thin/unprotected focus: <focus payoffs that are shallow or undefended; from susceptibility>
Off-focus noise: <prominent actual buckets outside the focus>
Susceptibility: <each susceptibility signal, count-cited; or "none flagged">
[if uncategorized % high] NOTE: X% synergy-carried & invisible to buckets → weight Strategy over numbers.
[if otag layer unavailable] NOTE: otag layer unavailable → structured facts only; lean on Strategy.
[if Focus Otags unset] NOTE: no focus set → Assessment from structured facts + Strategy only.
- Development — <plan not to fall behind early> — ok|risk
- Parity — <plan to break a stall / grind ahead> — ok|risk
- Winning — <the actual win from Strategy: what, how fast, how interruptible> — ok|risk
- Losing — <the out when behind/swept/raced> — ok|risk
Loss condition: <game-state where, if it goes there, you lose — threat-relative>
Prescription: add <card TYPE> for [quadrant / thin focus item]; trim [off-focus noise]
Owned fills: <inventory cards that plug it> (+ chase flags)
```

Present it to the user.

**Step 8 — Persist (standalone) or hand back (under the orchestrator).**
Standalone, with approval:
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection set-assessment "<deck>" "<the Step-7 synthesis>"
```
Never overwrite `Strategy`. Under building-decks, write the Assessment to
`draft.assessment` and hand it back — the machine holds it until COMMIT.

**Graceful degradation:** if `Focus Otags` is unset and you couldn't propose one,
synthesize from `susceptibility` + `otag_buckets` + Strategy alone (omit the
coverage/thin/off-focus lines). If the otag layer itself is unavailable, still synthesize
and write an Assessment from the structured facts (curve, ramp, instant-speed, keywords)
+ Strategy — thinner, but not skipped.

---

## Output contract

- **Assessment** — the Step-7 narrative pre-mortem (is / isn't / needs): per-quadrant
  plan, loss condition, prescription, owned fills. Never a percentage table.
- **(if proposed) Focus Otags** — the curated bucket/otag slug list.
- **The concrete needs** (prescription + owned fills) that refining-decks turns into
  ranked candidates.

**Persistence:** standalone, offer `set-assessment` (+ `set-focus-otags`) after approval.
Under building-decks, hand the Assessment back as `draft.assessment` — never write
mid-session.

## When to use

- Diagnosing an existing deck's balance / resilience / gaps.
- The ASSESS step of a building-decks session (including re-diagnosing a changed working
  deck after a REFINE loop-back).

## When NOT to use

- **No Strategy on the deck** — go to distilling-strategy first; you cannot diagnose
  without the aim.
- **Discovering / ranking upgrade candidates** — that's refining-decks (it CONSUMES this
  Assessment).
- **Empirically testing a win-rate** — that's simulating-games; this skill is a-priori
  reasoning, not games.
- **cEDH / high-power combo** — out of scope; quadrant theory breaks down there
  (game stages aren't well-defined). Say so; don't force it.
