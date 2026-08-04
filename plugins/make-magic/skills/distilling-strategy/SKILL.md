---
name: distilling-strategy
description: >
  Elicit or refresh a Commander deck's Strategy — the human-authored aim that every
  card-evaluation depends on. TRIGGER when: user says "I want to build a deck around
  [commander]", "help me figure out what this deck is", "what's my strategy", "start a
  new deck", "let's define the plan", "do my strategy notes still apply", or is starting
  a clean-slate build and needs a plan before any card work. Also trigger as the FRAME
  step of a building-decks session. SKIP for card evaluation, deck diagnosis, or card
  discovery — those are assessing-decks / building-decks / refining-decks tasks that
  CONSUME the Strategy this skill produces.
user-invocable: true
---

# Distilling Strategy

The Socratic front-door to deckbuilding. This skill turns a fuzzy intent ("I want a
Krenko goblins deck," "I think my Ozai deck is a burn deck?") into a written
**Strategy** — the human-authored aim, in the `strategy-schema.md` convention, that
every downstream skill reads and none of them overwrite.

<primary-constraint>
**A Strategy names both what the deck WANTS and what it explicitly DOES NOT WANT.**

Why: the wants tell refining-decks what to reach for; the DOES-NOT-WANTs are a **hard
pre-filter** it applies before it ranks a single card. A Strategy that only lists wants
produces a deck that drifts toward generic goodstuff — the exclusions are what keep it
coherent. Always elicit both. "What doesn't fit" is not an afterthought; it is half the
plan.
</primary-constraint>

<red-flags>
If you catch yourself about to:
- **Write a Strategy with no `What doesn't fit:` line** — STOP. Ask the exclusion
  question. A deck with no stated DOES-NOT-WANTs cannot be refined coherently.
- **Re-elicit a whole Strategy for a deck that already has one** — STOP. Existing decks
  get the "do these still apply?" DIFF, not a blank-slate interview. Read the current
  Strategy first and confirm/adjust it.
- **Commit the Strategy to the deck mid-session under the orchestrator** — STOP. Under
  building-decks you hand back the **FULL proposed Strategy** (or `None` = unchanged),
  held in `draft.strategy` — NEVER a partial fragment; the deck's committed Strategy
  changes only at COMMIT. Standalone, you may OFFER to persist.
- **Invent a color identity or commander the user didn't state** — STOP. Format,
  commander, and colors are elicited, never assumed.
</red-flags>

## What you produce

A **Strategy** in the `strategy-schema.md` convention — the aim in prose:

```
Commander: <name> (<color identity>)
Archetype: <primary> / <secondary if applicable>
Win conditions: <how the deck wins>
Key mechanics: <comma-separated keywords from the BUCKET_STRATEGY_SYNONYMS vocabulary>
Lines:
- <line of play>
What makes a card good here: <the WANTS — positive selection criteria>
What doesn't fit: <the DOES-NOT-WANTS — negative selection criteria, the hard filter>
```

Alongside the prose you name the **Focus Otags** — the handful of buckets/otag slugs
the deck is genuinely built around (`tokens`, `counters`, `anthem`, …), a curated
subset of intent, never the mechanical union of everything the cards happen to tag. And,
if the user states one, an **optional budget constraint** (a per-card or whole-deck
price ceiling) — captured here so refining-decks can turn on its conditional price axis.

Read the schema before you write:
<reference file="../building-decks/references/strategy-schema.md">
strategy-schema.md — the Strategy field convention, the `Key mechanics` vocabulary, the
worked reference examples (Sokka / Ozai / Shelob), and how the `Archetype:` line frames
the downstream pre-mortem.
</reference>

## The data surface: the `collection` CLI

Reads and writes of the deck's Strategy go through the backend-agnostic wrapper — the
same whether the source of record is local YAML or Airtable:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection status                        # announce the backend
${CLAUDE_PLUGIN_ROOT}/scripts/collection get-deck "<deck>" --field strategy
${CLAUDE_PLUGIN_ROOT}/scripts/collection set-strategy "<deck>" "<strategy text>"
${CLAUDE_PLUGIN_ROOT}/scripts/collection set-focus-otags "<deck>" tokens counters anthem
```
`set-strategy` / `set-focus-otags` forward to the `CollectionStore` port verbs
`set_strategy` / `set_focus_otags` — **the only write path.** Never raw Airtable CRUD.

## Two run modes

- **Standalone** — the user invokes this skill directly to write or refresh a Strategy.
  You do the elicitation, present the Strategy, and — with the user's approval — you may
  **persist** it (`set-strategy`, `set-focus-otags`).
- **Under building-decks (the FRAME state)** — you emit the **full proposed Strategy**
  that the orchestrator HOLDS in `draft.strategy`; it is **not** written to the deck.
  The committed Strategy changes only at the machine's COMMIT. Do not call `set-strategy`
  yourself in this mode — hand the full strategy back to the orchestrator. **Never hand
  back a partial fragment** — `draft.strategy` REPLACES the whole committed strategy at
  COMMIT, so a fragment would silently erase the rest (including the `What doesn't fit:`
  line). When nothing changed, hand back `None` (unchanged) — do NOT write a placeholder.

## Which mode of elicitation: clean-slate vs. diff

Open by reading the deck (if one is named) and announcing the backend:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection status
${CLAUDE_PLUGIN_ROOT}/scripts/collection get-deck "<deck>" --field strategy   # if a deck exists
```

- **Empty / no deck named → clean-slate elicitation** (full interview, below).
- **Strategy already present → the "do these still apply?" DIFF** (confirmation, below).

---

## Clean-slate elicitation (a new deck, no Strategy yet)

A structured Socratic interview. Ask in order; let the answers narrow the next question.
Don't dump all questions at once — this is a conversation, not a form.

**1. The frame — what are we building?**
- **Format** — Commander is the house default; confirm it (or capture the format the
  user names).
- **Commander** — the legendary the deck is built around. This fixes the **color
  identity** (every card must fall inside it — a hard rule downstream).
- **Colors** — read off the commander; confirm with the user if they had a different
  identity in mind (they may be choosing a commander to fit colors, or vice-versa).
- **Archetype** — aggro / midrange / control / combo / aristocrats / spellslinger /
  voltron / go-wide / stax / … The archetype frames what "healthy" looks like per
  game-state downstream, so pin it explicitly (see the `Archetype:` table in
  strategy-schema.md).

**2. The wants — what makes a card good here?**
- **Win conditions** — how does this deck actually win? Name the payoff(s).
- **Key mechanics** — the engine keywords, drawn from the `BUCKET_STRATEGY_SYNONYMS`
  vocabulary (`spellslinger`, `blink`, `aristocrats`, `counters`, `burn`, `combat`,
  `ramp`, …). These become the `Key mechanics:` line and seed the Focus Otags.
- **Lines of play** — the two or three sequences the deck wants to enact.

**3. The DOES-NOT-WANTs — what doesn't fit? (do not skip)**
Ask directly: *"What should this deck deliberately NOT do?"* Draw out the exclusions —
wrong-axis mechanics (auras in a spellslinger deck), tempo mismatches (slow value engines
in an aggro deck), anything the user actively wants to keep out. These become the `What
doesn't fit:` line and are a **hard pre-filter** in refining-decks. A vague "nothing off
strategy" is not enough — get specifics.

**4. Constraints (optional).**
- **Budget** — is there a price ceiling (per card, or whole deck)? If yes, capture it —
  it turns on refining-decks' conditional price axis. If the user doesn't raise budget,
  don't invent one; leave it unset (the price axis stays off).
- Any other hard constraints (owned-cards-only, no-reprints, theme restrictions).

**5. Curate the Focus Otags.**
From the `Key mechanics` and the archetype, name the handful of buckets/otag slugs the
deck is genuinely built around — the **intended identity**, a curated subset (a
tokens/counters go-wide deck's focus is `tokens counters anthem`, not the incidental
`ramp`/`removal` every deck runs). This is intent, never the mechanical union.

**6. Write the Strategy + emit a skeleton (clean-slate under the orchestrator).**
Assemble the `strategy-schema.md` block from the answers. For a truly-new deck the
orchestrator wants a **rough shell** to enter ASSESS: from commander/colors/archetype,
propose a skeleton decklist (the obvious staples + payoffs for the named mechanics, in
color identity) — a starting point, not a finished deck. The skeleton is `working_deck`
in the draft; the full Strategy is `draft.strategy`.

---

## The "do these still apply?" DIFF (an existing deck)

Do **not** re-run the full interview. Confirm or adjust the Strategy the deck already
has.

**1. Read the current Strategy** (and Focus Otags):
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection get-deck "<deck>"   # strategy + focus_otags + cards[]
```

**2. Present it back and diff.** Show the user the current `Archetype`, `Win
conditions`, `Key mechanics`, `What makes a card good here`, and `What doesn't fit`, and
ask, point by point, *"do these still apply?"* Surface where the **current decklist**
has drifted from the stated aim (a spellslinger Strategy with a pile of creatures added
since is a signal the aim moved — flag it, don't silently rewrite).

**3. Weave any adjustments into the FULL Strategy.** If the user adjusts — new
sub-archetype, a mechanic added or dropped, a new exclusion — apply those changes into
the **whole** Strategy block and re-present the complete updated block (not just the
changed lines). Re-confirm the DOES-NOT-WANTs specifically; they drift the most quietly.

**4. Hand back the FULL strategy — or `None`.** Under the orchestrator: if the user
ADJUSTED, hand back the **full** strategy WITH the adjustments woven in as
`draft.strategy` (held) — the complete block, never a fragment. If **nothing changed**,
hand back `None` (unchanged) — do **NOT** write a "confirmed as-is" placeholder.
**Never hand back a partial fragment as `draft.strategy`:** it REPLACES the entire
committed strategy at COMMIT, silently erasing every clause you left out (including the
`What doesn't fit:` DOES-NOT-WANT line). Standalone, offer to persist the
confirmed/adjusted Strategy (`set-strategy`) and any Focus Otags change (`set-focus-otags`).

---

## Output contract

You hand off:
- **Strategy** — the `strategy-schema.md` prose block: format, commander, colors,
  archetype, win conditions, key mechanics, lines, **wants** (`What makes a card good
  here`), **does-not-wants** (`What doesn't fit`).
- **Focus Otags** — the curated bucket/otag slug list.
- **Budget constraint** — present only if the user stated one; otherwise absent (the
  downstream price axis stays off).
- **(clean-slate only) a skeleton working deck** — the rough shell for ASSESS.

**Persistence:** standalone, offer `set-strategy` / `set-focus-otags` after approval.
Under building-decks, hand back the FULL proposed Strategy (or `None` = unchanged) and
let the machine hold it in `draft.strategy` until COMMIT — never a partial fragment,
never write mid-session.

## When to use

- Starting a brand-new deck and needing a plan before any card work.
- Refreshing an existing deck's aim ("does my strategy still apply?").
- The FRAME step of a building-decks session.

## When NOT to use

- **Evaluating a card / diagnosing balance / discovering upgrades** — those CONSUME a
  Strategy; they are assessing-decks / building-decks / refining-decks. Come here only
  to author or refresh the Strategy itself.
- **The Strategy is already correct and current** — nothing to distill; proceed straight
  to the analytical skill.
