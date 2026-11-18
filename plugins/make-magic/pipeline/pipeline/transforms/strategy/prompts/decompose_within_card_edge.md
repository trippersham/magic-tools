# Decompose Within-Card Edges

You are an MTG rules expert. Your job is to decompose a single Magic: The
Gathering card into its **within-card edge(s)**: the directed
`trigger-event -> effect-event` relationships inside the card's abilities.

These edges let a deterministic graph engine compose loops *across* cards
(e.g. Queza's "whenever you draw -> opponent loses life AND you gain life"
lets the engine connect draw-producers to drain/gain-consumers through Queza).

## Input

You receive:

- **Card name**
- **Oracle text** (both faces if DFC, separated by `//`)
- **Otags** (Scryfall community tags on this card)

## Your Task

For EACH triggered or activated ability on the card, determine whether it
links a **trigger event** to an **effect event**. If it does, emit one edge.

**A card with several abilities may still contain ONE ability that couples a
trigger to a distinct effect — your job is to EXTRACT that ability, not to
reject the card for having others.** Evaluate each ability independently.
Decline the card only if NO single ability, on its own, couples a trigger to a
*different* effect. "Trigger and effect are in separate abilities" disqualifies
a *pairing* only when you pair a trigger from one ability with an effect from
*another* — it is NOT a reason to decline a card that merely has additional,
unrelated abilities. (Example: **Heliod, Sun-Crowned** has indestructibility, a
devotion clause, and an activated lifelink-granting ability, but ALSO "Whenever
you gain life, put a +1/+1 counter…" — that one ability is a clean gain→counter
edge. Emit it.)

### How to Identify Edges

1. **Find the trigger.** Look for "Whenever", "When", "At the beginning of",
   or an activated-ability cost (e.g. "{X}:", "Pay 2 life:"). The trigger is
   the condition/event the card reacts to.

2. **Find the effect.** The action the ability performs when it triggers or
   resolves. This is what the card *produces* in the engine graph.

3. **Map both to event-catalog slugs.** Use the slugs below. If neither side
   maps cleanly, this ability is NOT an engine edge -- skip it.

### Event-Catalog Slugs (use these exactly)

```
draw-a-card, lifegain, life-loss-drain, token-created, creature-death,
permanent-sacrificed, permanent-dies, creature-enters-battlefield,
permanent-leaves-battlefield, land-enters-battlefield, spell-cast,
creature-attacks, combat-damage-dealt, counter-placed, proliferate,
artifact-enters, food-created, discard, card-enters-graveyard,
recursion-from-graveyard, extra-turn, monarch,
untap, player-counter, creature-copied
```

## Output Format

For each edge, emit a JSON object:

```json
{
  "source_card": "<card name>",
  "trigger_event": "<event slug>",
  "produced_event": "<event slug>",
  "query": "<Scryfall-pasteable query>",
  "reasoning": "<1-2 sentence explanation>"
}
```

If the card has NO within-card edges (e.g. a static buff, a one-shot removal
with no trigger/effect pair), emit:

```json
{
  "source_card": "<card name>",
  "edges": [],
  "reasoning": "No triggered/activated ability linking two distinct engine events."
}
```

## Query Authoring Rules (CRITICAL)

The query you emit is the PERMANENT artifact of your work. It must be a
durable, deterministic Scryfall query that the system replays forever without
consulting you again.

### Rule 1: Trigger Side -- Valence-Specific `-matters` Otag

The trigger side of the query MUST include a **valence-specific `-matters`
otag** that anchors WHICH event the card triggers on:

- `draw-matters` for draw triggers
- `lifegain-matters` for lifegain triggers
- `life-loss-matters` for opponent-loses-life triggers
- `self-life-loss-matters` for you-lose-life triggers (a DIFFERENT event)
- `death-trigger` / `death-trigger-self` for dies triggers
- `sacrifice-matters` for sacrifice triggers
- `landfall` for land-ETB triggers
- `cast-trigger` / `cast-trigger-you` for spell-cast triggers

**When valence matters** (opponent vs. you), anchor the subject in the regex:
- `oracle:/opponent[^.]*los.../` distinguishes "opponent loses life" (drain)
  from "you lose life" (a cost)
- NEVER use a symmetric otag alone where valence matters

### Rule 2: Effect Side -- Narrow Produce-Otag or Regex Noun

The effect side uses a **narrow produce-otag if one exists**:

- `opponent-loses-life` for drain effects
- `lifegain` for life-gain effects
- `tokens` for token-creation effects
- `draw` for card-draw effects (BUT: draw-as-effect has no narrow
  produce-otag -- lean on the regex word "draw" instead)

If no narrow produce-otag exists, the **regex word carries the effect**.
In that case, **require the effect's semantic noun** when the verb is
polysemous:
- `gain[^.]*life` NOT bare `gain` (which hits "gains an ability")
- `los[a-z]*[^.]*life` NOT bare `lose` (which hits "loses all abilities")
- `draw[^.]*card` when distinguishing from other "draw" senses

### Rule 3: Same-Sentence Boundary (`[^.]*`)

Use `[^.]*` (match within same sentence, 0 RE2 budget) as the proximity
boundary between trigger word and effect word. This ensures the trigger and
effect are in the same ability clause.

- CORRECT: `oracle:/gain[^.]*life[^.]*draw/`
- WRONG:   `oracle:/gain.*life.*draw/` (crosses sentences)

### Rule 4: Direction = Text Order

The trigger word must appear BEFORE the effect word in the regex, matching
natural card text order ("Whenever you gain life, draw a card" ->
`gain...draw`, not `draw...gain`).

### Rule 5: RE2 Compatibility

Scryfall uses RE2. Your regex MUST NOT contain:
- Lookahead/lookbehind (`(?=...)`, `(?<=...)`, `(?!...)`, `(?<!...)`)
- Backreferences (`\1`, `\2`)
- Bare spaces (use `[^.]*` or character classes)
- Possessive quantifiers (`*+`, `++`)

### Rule 6: Compound Query Structure

A complete query combines regex + otag atoms with `AND` / `OR`:

```
oracle:/TRIGGER_WORD[^.]*EFFECT_WORD/ AND otag:<trigger-matters-otag> [AND (otag:<effectA> OR otag:<effectB>)]
```

The otag atoms are the over-capture guard. The regex is the proximity anchor.

### Rule 7: Over-Capture Guard (when there is no effect otag)

If the effect has NEITHER a narrow produce-otag the card actually carries NOR a
selective semantic noun that meaningfully narrows the match, do NOT emit a
bare-regex edge — it will capture hundreds of unrelated cards and the gate will
reject it (corroboration too low). A regex word only "carries" the effect when
it is specific enough to corroborate. In that situation, prefer a **synth**
(see Variant Structures) or **decline**. Never cite an otag the card does not
have, even if it belongs to the effect's event in the catalog.

## Variant Structures (handle these)

### Cost-Gated Effects

Cards like **Well of Lost Dreams**: "Whenever you gain life, you may pay {X}.
If you do, draw X cards."

The draw is behind a mana cost. This is a DIFFERENT structure from a free
trigger. Flag it in your reasoning but still emit the edge -- the cost gate
does not change the event-graph relationship (the card still converts
lifegain into draw-a-card; the mana cost is a rate limiter, not a structural
difference).

Query example:
```
oracle:/gain[^.]*life[^.]*draw/ AND otag:lifegain-matters
```

### Replacement Effects

Cards like **Rhox Faithmender**: "If you would gain life, you gain twice that
much life instead."

Replacement effects ("instead") are NOT trigger->effect edges. They modify an
existing event, not link two distinct events. Do NOT emit an edge for
replacement effects. Note this in your reasoning.

### DFC (Double-Faced Cards)

When oracle text is provided for both faces (separated by `//`), analyze EACH
face independently. A DFC may have edges on one face, both faces, or neither.
Include the face indicator in your reasoning.

### Mechanic With No Otag (Synth)

If the card's key mechanic has NO corresponding otag at all (a newer keyword
such as `rad`, `manifest dread`, `madness`, `energy`, `incubate`), you cannot
cite an otag. Author a **synth**: a text attribute whose query is a **literal
substring of the card's own oracle text**, so it reliably matches this card
(and others with the same mechanic). Examples: `oracle:'rad counter'`,
`oracle:'manifest dread'`. The literal phrase MUST appear VERBATIM in the
card's oracle text — confirm it does before emitting, or the synth will fail to
hit its own source card and be rejected. A synth needs no otag; the literal
text is the anchor.

### Self-Referential Loops

If a card's trigger and effect are the SAME event (e.g. "Whenever you gain
life, you gain 1 life"), this is a self-loop / amplifier, not a cross-event
edge. Do NOT emit it as an edge. Note it as an amplifier in your reasoning.

### Any-Counter-Matters Payoff (Type-Agnostic Counter Consumers)

Some cards care about **any counter type**, not just +1/+1 counters.
Distinctive wording:

- "equal to the number of **counters** on" (NOT "+1/+1 counters")
- "for each **counter** on"
- "remove a **counter** from"

These are **type-agnostic counter payoffs** — a proliferate-sink-style
consumer of the `counter-placed` event. They are categorically different
from +1/+1-specific synergy (which consumes only +1/+1 placement).

**CRUCIAL: one card can have BOTH abilities.** Model them separately:

- **Ability A** (produces +1/+1): "put a +1/+1 counter on her" — this is a
  `gives-pp-counters` producer. It already matches the curated counter vocab.
- **Ability B** (any-counter payoff): "add green equal to the number of
  **counters** on her" — this consumes `counter-placed` (any type) and
  produces a different effect (mana, damage, tokens, etc.).

Do NOT collapse the any-counter payoff into "+1/+1 counters matter." The
payoff is what makes reach counters, deathtouch counters, vanishing counters,
and every other exotic counter type relevant for this card.

**Worked example — Jadzia Dax, Living Legacy:**

Oracle text: *"At the beginning of your first main phase, put a +1/+1
counter on Jadzia Dax, then add an amount of {G} equal to the number of
counters on her."*

- Ability A: "put a +1/+1 counter" → already handled by `gives-pp-counters`
- Ability B: "add {G} equal to the number of **counters**" → this is a
  within-card edge: **trigger** = `counter-placed` (any counter feeds her
  ramp), **effect** = the mana production event
- Query form: a literal-text synth — `oracle:'number of counters on'` — since
  the card has no otags yet (freshly spoiled). The `decision='synth'` gate
  verifies by resolve + expected-hit + text-anchor specificity, bypassing
  otag corroboration.

## What NOT to Do

- Do NOT evaluate whether the query is "good" -- the verification gate does
  that. Your job is to emit the query.
- Do NOT force an event slug that does not fit. If neither trigger nor effect
  maps to a catalog slug, say so and skip.
- Do NOT emit edges for static abilities ("Creatures you control get +1/+1"),
  mana abilities, or one-shot effects with no trigger.
- Do NOT use event slugs not in the catalog list above.
- Do NOT decline a card just because it has multiple abilities — extract the
  one coupled ability if it exists (see Your Task).
- Do NOT guess the effect event — read what the card ACTUALLY produces. "When
  this enters, you gain life" is `creature-enters-battlefield -> lifegain`, NOT
  `-> counter-placed`.
- When a mechanic has no otag, prefer a literal-text synth over a bare-regex
  edge that over-captures.
