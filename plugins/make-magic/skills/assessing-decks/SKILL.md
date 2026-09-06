---
name: assessing-decks
description: >
  Diagnose a Commander deck's balance with Quadrant Theory — a reasoning-led pre-mortem
  over a neutral fact sheet — and score its power with CRISPI + the Commander Bracket.
  TRIGGER when: user asks "is my deck balanced", "diagnose [deck]", "what's [deck] missing",
  "what quadrant is weak", "what should I shore up", "is [deck] too glass-cannon", "where
  does [deck] fall apart", "assess [deck]". ALSO trigger for power-level questions: "what's
  my CRISPI score", "how powerful is [deck]", "what Commander Bracket is [deck]", "score
  [deck]", "how fast/consistent/resilient is [deck]". Also trigger as the ASSESS step of a
  building-decks session. SKIP for single-card evaluation, card discovery / upgrade
  suggestions (that's refining-decks), or empirical win-rate testing (that's
  simulating-games). This skill produces the Assessment + CRISPI those steps consume.
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
  narrative pre-mortem, not a tally. (This is about the QUADRANTS; CRISPI's Step-9
  axes — S/C/I/R 1–10 + PI — are a separate, legitimate cEDH-anchored power score
  computed by a deterministic engine, NOT the retired quadrant tally. Don't conflate them.)
- **Diagnose from card memory** — STOP. Run the `factsheet` verb; reason over the actual
  `otag_buckets` + `susceptibility`, not a remembered decklist.
- **Read empty `otag_buckets` as "the deck does nothing"** — STOP. That's the otag layer
  being unavailable, OR a high-synergy deck invisible to buckets. Fall back to structured
  facts + Strategy.
- **Overwrite the Strategy** — STOP. Strategy is a human-authored INPUT you read; you
  write the `Assessment` (and may propose `Focus Otags`), never the Strategy.
- **Diagnose a stale decklist** — STOP. Always read the CURRENT deck (`get-deck`) at the
  top of the run; the store holds the live cards. If REFINE just changed the deck, your
  prior Assessment is stale — re-diagnose the current deck, don't reuse the old text.
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

## Cold start: from a decklist file to a CRISPI score

New here, with just a decklist file (a precon export, a pasted list) and no populated
backend? This is the shortest path to a real **CRISPI** score. A standalone CRISPI does
**not** require a Strategy — the `<primary-constraint>` Strategy gate applies to the full
Assessment / quadrant pre-mortem, not to CRISPI, which is a deterministic power score you
can run on any deck. (For the full Assessment you still need a Strategy — distilling-strategy
first.)

```bash
# 1. Pin the local backend (one time; persists, won't re-prompt).
${CLAUDE_PLUGIN_ROOT}/scripts/collection onboard --backend local

# 2. Hydrate the card lake (see Prerequisites — first run downloads ~140MB and takes a
#    few minutes; re-runs are a fast no-op). WITHOUT this, scoring verbs refuse loudly.
${CLAUDE_PLUGIN_ROOT}/scripts/collection hydrate-lake

# 3. Import the list as an ephemeral local draft. A file path auto-sniffs as plaintext
#    (--source plaintext forces it). For a precon-style `#`-header export the header
#    becomes the deck name ("# Witherbloom Pestilence — total=100" → that name).
${CLAUDE_PLUGIN_ROOT}/scripts/collection import-deck <file> --source plaintext

# 4. Score it. --commander-dependence is required; --fundamental-turn is optional
#    (omit to auto-estimate, supply to override).
${CLAUDE_PLUGIN_ROOT}/scripts/collection crispi "<deck name>" \
  --fundamental-turn <N> --commander-dependence <low|med|high>
```

**Import notes (the friendly guardrails):**
- **Duplicate lines sum.** MTGJSON precon exports emit one line per printing, so `1 Swamp`
  eight times is 8 Swamp — the importer sums them; a 100-card list stays 100.
- **Commander detection is loud, never silent.** A `Commander:`/`[Commander]` section wins;
  else pass `--commander "<name>"`; else the importer tries the first-line legendary *when
  the lake is up to confirm it*. If a Commander-format list lands with **0 commanders**, you
  get a hard stderr warning — and `crispi` will **refuse** the deck until you re-import with
  `--commander`. Set the commander before scoring a commander-format deck.
- **Re-importing the same list** mints another same-named draft (the dup-name walls are
  intact); a later bare-name reference is then ambiguous. Address a specific copy with
  `--id <prefix>`, or archive the extra — the import prints the exact commands.

The two reasoning inputs (`--fundamental-turn`, `--commander-dependence`) are defined
authoritatively in **Step 9** below — read those definitions before you supply them.

**Speed and drivers (the cold-start default).** The cold-start `crispi` above gives an
honest **closed-form** Speed with no driver required — that is the deterministic a-priori
answer and it is correct for the majority of decks (go-wide, aggro, goodstuff — CP7 pilots
them fine). Speed only gets *sharper* by simulating for a deck with a real in-deck win line
to pilot: author a **DRIVE** driver via **`authoring-drivers`**, run it through
**`simulating-games`**, and Speed will consume the driven-goldfish clock instead of the
closed form. `crispi`'s stderr flags `driver_recommended` when that would help. You do NOT
need a driver to get a CRISPI score — only to replace a closed-form Speed with a simulated
one on a driver-worthy deck.

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
`set-focus-otags` / `set-assessment` are the **only write path** — the local decks store
applies them to the typed `Deck`. They **commit through** the target: on a SYNCED deck
they write to its source of record; on an EPHEMERAL building-decks draft the same call
stays purely local. You do not choose a mode — the target's ephemerality decides. Never
raw Airtable CRUD.

## Prerequisites

- **uv** — the CLI and the fact-sheet engine run via `uv run`.
- **A hydrated card lake** — scoring verbs (`factsheet`, `crispi`) read enrichment (oracle
  ids + the rolled-up oracle-tag closure) from a local card lake. Bootstrap it once with
  `collection hydrate-lake` (respects `MAKE_MAGIC_DATA_DIR`). **First run** downloads the
  Scryfall `oracle_cards` bulk (~140MB, a few minutes) and loads the full oracle-tag dataset
  (~84-92% coverage); re-runs are a fast no-op (cursor-gated, no-clobber). This is a
  **friendly guardrail, not a failure mode**, and the two scoring verbs handle a missing/
  degraded otag layer DIFFERENTLY:
  - `crispi` **refuses loudly** — exiting nonzero with remediation text naming
    `collection hydrate-lake` — when the lake is absent/stub, the deck stays low-coverage
    after enrichment, OR the oracle-tag closure is unavailable or **snapshot-degraded** for
    the deck (below the coverage floor). Its whole output is a confident score, so it never
    scores off a blank/near-blank otag signal.
  - `factsheet` still **refuses** on an absent/stub lake or a name-only deck, but when only
    the otag closure is unavailable it **degrades gracefully** to structured facts with the
    `otag layer unavailable` marker (see Step 3's Graceful degradation) rather than refusing.
- **`MAKE_MAGIC_DATA_DIR`** — the store root; all lake paths and the local decks store
  resolve off it. Set it to a stable location (a throwaway dir gets an unhydrated lake and
  triggers the refusal above).
- **A populated backend** — for the full Assessment / quadrant pre-mortem, the deck must be
  present with a Strategy filled (per `strategy-schema.md`); no Strategy → distilling-strategy
  first. (A standalone CRISPI needs only an imported deck + a hydrated lake — no Strategy; see
  **Cold start** above.)

## Two run modes — same write path, different target

The write path is identical in both modes: you `set-assessment` (and `set-focus-otags`
if you proposed a focus). What differs is only the target deck.

- **Standalone** — the user asks to diagnose a real (synced) deck. You read its Strategy
  + Focus Otags via the CLI, produce the Assessment, present it, and — with approval —
  `set-assessment` (+ `set-focus-otags`). Commit-through writes it to the source of record.
- **Under building-decks (the ASSESS step)** — the orchestrator points you at the
  session's target deck, typically an EPHEMERAL exploration draft. You read the CURRENT
  deck with `get-deck` and `set-assessment` onto THAT draft, exactly as standalone;
  because it's ephemeral the write stays local until the orchestrator promotes it. There
  is no "hand it back and hold it."

**Staleness is derived, not flagged.** Always diagnose the CURRENT `get-deck` cards. If
REFINE just changed the deck, the deck moved and your prior Assessment is stale by
definition — simply re-diagnose the current deck and re-write it. There is no stale flag
to clear and no held value to reuse; the store always serves the live deck.

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
work from it, not from memory. `get-deck` always serves the live cards from the store, so
under the orchestrator (where REFINE may have just changed the deck) the Step-1 read is
already the current deck — no separate working-deck to fetch.

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

**Step 8 — Persist the Assessment.** With approval, write it via the CLI:
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection set-assessment "<deck>" "<the Step-7 synthesis>"
```
`set-assessment` STAMPS assessment freshness on the deck row (it records the post-write
deck version), so a later content edit makes the assessment derivably `stale` — that stamp
is what the building-decks orchestrator reads to route back to ASSESS. Never overwrite
`Strategy`. This is the same call standalone and under the orchestrator;
on a real deck it commits through to the source of record, and on an ephemeral
building-decks draft it stays local until the orchestrator promotes it.

**Graceful degradation:** if `Focus Otags` is unset and you couldn't propose one,
synthesize from `susceptibility` + `otag_buckets` + Strategy alone (omit the
coverage/thin/off-focus lines). If the otag layer itself is unavailable, still synthesize
and write an Assessment from the structured facts (curve, ramp, instant-speed, keywords)
+ Strategy — thinner, but not skipped.

---

## Step 9 — CRISPI power score + Commander Bracket (the quantitative companion)

The quadrant pre-mortem answers *"does the deck have a plan for every game-state?"* CRISPI
answers a different, complementary question: *"how powerful is it, on a cEDH-anchored
scale, and what Commander Bracket is it?"* Run it after the pre-mortem — it reuses the same
fact sheet, and the reads you already did (curve, ramp, wincon, susceptibility) are exactly
what you need to supply its two inputs.

**CRISPI is a deterministic engine with exactly two reasoning inputs — you supply only those
two; the engine computes everything else.** It is a *static, a-priori* analysis that does NOT
depend on a deck being pilotable by Forge, so it is a load-bearing complement to
`simulating-games`, not a redundant one (a combo/turbo deck Forge under-pilots can still
score Speed 9 / Resilience 9 here).

**The two inputs you reason (from the pre-mortem you just did):**
- **`--fundamental-turn <N|N.5>`** — goldfish the deck: the turn it *completes* a first
  elimination (or wins outright) in ≥50% of goldfish games, no disruption — the score times
  the *median game*, not the fast high-roll. Half-steps are a real read of variance (a line
  that kills turn 4 on curve / turn 5 through a brick → `4.5`); if torn, take the slower turn.
  This is the entire Speed axis. (OPTIONAL at the CLI — omit it and the engine auto-computes
  the estimate; supply it to override.) When auto-computed, the engine returns a **deterministic
  closed-form** own-turn by default. It **escalates to a driven goldfish** (a real, sim-backed
  Speed) for exactly one kind of deck: one with a **DRIVE**-class authored driver — a genuine
  in-deck win line to pilot. A **THIN** driver (bare CP7 — most go-wide/aggro/goodstuff decks)
  keeps the deterministic closed form, because a goldfish driven by CP7 adds nothing over it.
  The stderr provenance line reports `via tier1` (closed form) or `via tier2` (driven goldfish),
  and flags `driver_recommended` when a DRIVE driver would sharpen a deck that lacks one — your
  cue to author one via `authoring-drivers` / `simulating-games` and re-score.
- **`--commander-dependence <low|med|high>`** — how the deck plays commander-less:
  `low` = runs fine without it (80%+ capacity; a goodstuff/combo-in-the-99 pile),
  `med` = the format default (matters, still executes, 50–80%),
  `high` = brought to its knees (<50%; the engine/win/mana lives in the command zone).

**Run it:**
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection crispi "<deck>" --fundamental-turn <N> --commander-dependence <low|med|high>
```
It returns the four axes (each a 1–10 value + a short rationale + the cards that drove it),
the **Performance Index** (their average), and the **Commander Bracket (1–5)** with the
signals that set it. Keep **all four axes**, not just the PI — a 9/3/3/9 fast-fragile deck
and a 6/7/7/6 balanced deck share a PI but are different decks.

**Present** the axes + PI + Bracket to the user alongside the Assessment. Then **persist both**:
```bash
# structured result (a derived stamp, like sim — goes stale when the deck changes).
# Pipe the crispi JSON via stdin (--result -): a real result carries apostrophes in
# rationales/card names that break inline shell quoting.
${CLAUDE_PLUGIN_ROOT}/scripts/collection crispi "<deck>" --fundamental-turn <N> --commander-dependence <t> \
  | ${CLAUDE_PLUGIN_ROOT}/scripts/collection stamp-crispi "<deck>" --result -
```
and fold a one-line summary into the Assessment prose you write in Step 8, e.g.
`CRISPI 6.25 · S7/C6/I7/R5 · Bracket 3`. The structured stamp is what a later
building-decks VALIDATE step can cross-check against a sim verdict (a change that drops on
BOTH sim and CRISPI is true worsening; one that drops on sim but holds on CRISPI is Forge
archetype under-representation).

<cedh-anchoring-note>
**cEDH scope — CRISPI vs the quadrant diagnosis are different.** The quadrant pre-mortem
keeps cEDH **out of scope** (game stages aren't well-defined there — see "When NOT to use").
CRISPI is the opposite by design: it is **cEDH-anchored** — cEDH-optimized lists *define*
8/9/10 on every axis, and grading a non-cEDH deck on that scale is intended and fine (a
precon centers ~CRISPI 5 / Bracket 2). So "cEDH is out of scope" applies to the quadrant
reasoning, NOT to CRISPI. Run CRISPI on any deck, including a low-power one; do not refuse it
on the cEDH-scope grounds that apply to the pre-mortem.
</cedh-anchoring-note>

**Graceful degradation:** CRISPI's accuracy depends on otag coverage. If the otag layer is
degraded/unavailable the axes read low across the board — note the caveat rather than
reporting a misleadingly-low score as fact.

---

## Output contract

- **Assessment** — the Step-7 narrative pre-mortem (is / isn't / needs): per-quadrant
  plan, loss condition, prescription, owned fills. Never a percentage table.
- **(if proposed) Focus Otags** — the curated bucket/otag slug list.
- **The concrete needs** (prescription + owned fills) that refining-decks turns into
  ranked candidates.
- **CRISPI score + Commander Bracket** (Step 9) — the four axes + PI + Bracket, with the
  one-line summary folded into the Assessment prose.

**Persistence:** you write the Assessment via `set-assessment` (+ `set-focus-otags`) and the
structured CRISPI result via `stamp-crispi`. On a real (synced) deck the assessment commits
through to the source of record; the CRISPI stamp is a local derived-output stamp (like sim).
On an ephemeral building-decks draft everything stays local until the orchestrator promotes it.

## When to use

- Diagnosing an existing deck's balance / resilience / gaps.
- The ASSESS step of a building-decks session (including re-diagnosing after a REFINE
  loop-back — you simply re-read the current deck and re-write the Assessment).

## When NOT to use

- **No Strategy on the deck** — go to distilling-strategy first; you cannot diagnose
  without the aim.
- **Discovering / ranking upgrade candidates** — that's refining-decks (it CONSUMES this
  Assessment).
- **Empirically testing a win-rate** — that's simulating-games; this skill is a-priori
  reasoning, not games.
- **cEDH / high-power combo — for the QUADRANT pre-mortem only.** Quadrant theory breaks
  down there (game stages aren't well-defined); say so, don't force the pre-mortem. This
  does NOT apply to the Step-9 **CRISPI** score, which is cEDH-anchored by design — run
  CRISPI on any deck regardless of power level.
