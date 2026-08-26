# Deck Strategy Schema

The `Strategy` field on each Deck record in Airtable (`fldvJRaoYfRZiM8zw`) is the source of truth for what a deck **aims** to be — a human-authored aspiration. This document defines the format for writing and reading strategies.

> **`Strategy` vs `Focus Otags` vs `Assessment`.** `Strategy` (this field) = what the deck AIMS to be, in prose (the plan). `Focus Otags` (Decks table, multipleSelects or long text) = the otags/buckets the deck CARES about — its intended functional identity in the tag vocabulary, a curated subset (not the wide set the cards mechanically carry); skill/human-authored and written via the Airtable MCP, and the deterministic pipeline READS it but NEVER writes it. `Assessment` (Decks table, long text) = what the deck ACTUALLY is, isn't, and needs — a reasoning synthesis measuring actual card otags against `Focus Otags`, authored by building-decks Operation 5 and written via the Airtable MCP. Never conflate them: `Strategy` is the prose aim and `Focus Otags` the intended mechanics (both inputs); `Assessment` is the reality-vs-intent output. See `references/quadrant-theory.md`.

## The live format

Every live deck's `Strategy` follows this five-section format. These exact section headers are the schema — write them verbatim, uppercase, with the `•`-bulleted lists shown:

```
PRIMARY STRATEGY: <archetype / sub-archetype> (<colors>)

GAME PLAN: <one prose paragraph — the overall plan and win path>

KEY LINES:
• <line of play, at piloting altitude — see below>
• <line of play>

WANTS:
• <positive selection criterion>
• <positive selection criterion>

DOES NOT WANT:
• <negative selection criterion — the hard pre-filter refining-decks applies first>
• <negative selection criterion>
```

`PRIMARY STRATEGY` carries the **archetype** (it is the field that frames the pre-mortem — see the table below). `WANTS` are the positive selection criteria; `DOES NOT WANT` is the hard exclusion pre-filter. `GAME PLAN` is the one-paragraph win path.

### Optional trailing sections (freeform human notes — NOT part of the core schema)

Actively-worked decks sometimes carry extra tails after `DOES NOT WANT`. They are freeform human working notes, not structured fields — read them for context, never depend on their shape:

```
QUADRANT SHAPE (Winning / Losing / Parity / Development): <freeform notes>

═══ PICK UP HERE (last session <date>) ═══
JUST DID: <freeform>
NEXT MOVE: <freeform>
```

Treat anything past the five core sections as advisory prose. A driver/tagger consumer parses only the five sections above (in practice only `PRIMARY STRATEGY` / `KEY LINES` / `WANTS` / `DOES NOT WANT` carry machine-relevant vocabulary).

## `KEY LINES` is the load-bearing section — piloting altitude, category-framed

Two consumers read `KEY LINES` and both need more than an axis label:

- **Card selection** (refining-decks) — reaches for pieces that serve the line.
- **Driver authoring** (authoring-drivers) — translates each line's `OWN:` decision into a thin sim-pilot that makes exactly that call and defers the rest.

A bare axis ("sacrifice for value") is a **failure** at this altitude — it names the axis, not the line. It cannot tell a pilot *which* thing to sacrifice, *when*, or *what* to hold.

### A. Piloting altitude — concrete sequence + `OWN:` + `DEFER:`

Each `KEY LINE` must be specific enough to **pilot** the deck, not merely to select its cards. Every line carries three parts:

1. **The concrete sequence** — the real play, in order (the actual sequence of the line), not the abstract engine.
2. **`OWN:`** — the non-obvious decision a good pilot controls that a naive AI botches: the *noun* to cast/target/sacrifice/keep, and *WHEN*. Write it as a **human strategic decision in the deck's own terms**, **never** as an engine API call. The authoring skill maps the human decision onto the sim's hooks; keeping `KEY LINES` engine-agnostic keeps the Strategy the deck's source-of-truth aim, not a sim artifact.
3. **`DEFER:`** — what generic play (land drops, curve, combat, casting) to leave to the base AI. Naming what NOT to own is half the value: it is the never-regress discipline (a pilot that re-pilots land drops and combat plays *worse* than a competent baseline). Most of the game defers; own only the model gaps.

A line with genuinely no owned decision — a linear aggro/goodstuff deck a competent baseline pilots fine — records an **empty or "none" `OWN:` honestly**. Do not manufacture an owned decision where there isn't one.

### B. Category-framed, comprehensive — NOT card-enumerated-as-definition

`KEY LINES` and their `OWN:` clauses are framed by **category / mechanic**, with specific cards named only as **NON-EXHAUSTIVE members** ("outlets — Zuran Orb, Szarel, sac-a-land effects; recursion — Life from the Loam, Crucible, Splendid Reclamation"). The definition is the mechanic ("ANY outlet that can sacrifice a land"), not the card.

**Exception:** a discrete infinite/deterministic combo (e.g. Mikaeus + Triskelion) IS its specific pieces — name them.

This carries a matching **consumption rule** for `authoring-drivers`: a driver's guard must match the category **COMPREHENSIVELY**, never one card as a stand-in. Two acceptable implementations, enumerated often PREFERRED:

- **enumerated name-set** — `getName()` matched against the FULL set of every card in that category in the decklist (knowable at design time, precise); OR
- **semantic match** — cost / rule-prefix / `Outcome` — where it reliably covers the category.

The BUG is matching ONE card as a proxy for the category: it misses comparable members and silently breaks on a swap. Enumerated name-sets are drift-safe because a swap changes `deck_version` → `driver_valid` flips false → re-author picks up the new member. Combo pieces stay name-matched.

## Reference examples (real live strategies)

These are the actual live `Strategy` fields (read via `collection get-deck "<deck>" --field strategy`), with their `KEY LINES` lightly enriched to piloting altitude + category framing. `WANTS` / `DOES NOT WANT` are kept close to the live text.

### Ozai — Big Mana Burn / Firebending (Rakdos)

```
PRIMARY STRATEGY: Big Mana Burn / Firebending (Rakdos)

GAME PLAN: Accumulate mana through firebending creatures and Ozai's unique ability to
retain unspent mana as red mana. Channel accumulated mana into devastating X-cost burn
spells for game-ending turns.

KEY LINES:
• Mana-hoard into X-burn: attack with firebending creatures → bonus mana → Ozai retains
  the unspent pool as red → sink it into an X-cost finisher at a face.
  OWN: hold the X-burn for a lethal X, not a "safe" value X; prefer building the pool
  another turn over spending it on a midrange body when a payoff is in hand.
  DEFER: land drops, curve, combat, which land to play.
  members (examples, NOT the definition): finishers — Crackle with Power, Torment of
  Hailfire, ANY X-cost burn/damage spell; enablers — firebending creatures, mana
  doublers, cost reducers.
• Impulse-fuel pressure: impulse-draw to refuel while chipping life totals.
  OWN: spend impulse-exiled cards the turn they are available (they expire); point burn
  at a face over a trade unless the creature is lethal-relevant.
  DEFER: blocks, generic sequencing.

WANTS:
• Mana generation / mana doubling
• X-cost burn spells and damage dealers
• Firebending creatures (attack → mana)
• Cost reduction for big spells
• Direct damage effects
• Impulse draw / card advantage

DOES NOT WANT:
• Defensive or slow cards
• Cards that don't advance the mana or damage plan
```

### Sokka — Spellslinger / Ally Tokens (Jeskai)

```
PRIMARY STRATEGY: Spellslinger / Ally Tokens (Jeskai)

GAME PLAN: Cast noncreature spells to generate 1/1 Ally tokens via Sokka, while prowess
and menace make them increasingly lethal. Lesson spells provide flexible interaction
while triggering token generation.

KEY LINES:
• Spell-chain the wide board: cast the cheapest noncreature spells FIRST → each triggers
  Sokka's Ally token + prowess across the team → close by going wide with menace.
  OWN: order cantrips before the payoff to stack the trigger count; on a crack-back turn
  hold up an instant rather than tapping out when a counter/bounce is live.
  DEFER: land drops, which land to play, combat math, blocks.
  members (examples, NOT the definition): triggers — ANY cheap instant/sorcery, cantrips,
  Lesson spells; payoffs — prowess/menace/anthem effects.
• Lessons as modal interaction: cast a Lesson for removal/tempo that ALSO fires the token
  trigger.
  OWN: pick the Lesson that answers the live threat while still adding to the spell count;
  don't fetch a Lesson that only replaces itself when interaction is needed.
  DEFER: generic development.

WANTS:
• Cheap noncreature spells (instants, sorceries, artifacts)
• Prowess / magecraft / cast triggers
• Token anthem effects
• Card draw / cantrips to maintain spell flow
• Cost reduction for spells

DOES NOT WANT:
• Expensive creatures that don't generate value from spellcasting
• Cards that only reward creature-heavy strategies
```

### World Reclaimer — Lands-matter / Sacrifice

```
PRIMARY STRATEGY: Lands-matter / Sacrifice

GAME PLAN: Exploit lands entering and leaving the graveyard. Szarel rewards sacrificing
permanents by distributing +1/+1 counters. The deck generates massive value from landfall
triggers and graveyard recursion.

KEY LINES:
• Lands-as-fodder recursion: sac an EXPENDABLE land (a cracked fetch, or a basic beyond
  this turn's drop) to ANY land-eating sacrifice outlet → return it with ANY
  graveyard→battlefield land-recursion effect → re-fire ANY landfall / land-ETB payoff.
  OWN: to any outlet that can sacrifice a land, feed a fetched/surplus land — NEVER a land
  tapped for a payoff this turn; hold land-sac activations for end-of-turn / in response
  to removal.  DEFER: land drops, curve, combat, casting.
  members (examples, NOT the definition): outlets — Zuran Orb, Szarel, sac-a-land effects;
  recursion — Life from the Loam, Crucible, Splendid Reclamation, Ramunap Excavator;
  payoffs — landfall / land-ETB triggers.
• Explosive mass recursion: Splendid Reclamation (or a mass land-return) after a graveyard
  full of lands for a one-shot landfall/mana burst.
  OWN: hold the mass return until the graveyard land count and on-board landfall payoffs
  make it a swing, not a ramp spell; don't fire it into an empty board.
  DEFER: generic sequencing.

WANTS:
• Cards that play lands from graveyard
• Landfall payoffs
• Sacrifice outlets (especially for permanents)
• Land recursion / land ramp
• +1/+1 counter synergies (secondary)

DOES NOT WANT:
• High CMC without immediate board impact
• Non-permanent spells that don't advance the engine
```

## The `PRIMARY STRATEGY:` line frames the pre-mortem

Besides feeding synergy judgment, `PRIMARY STRATEGY` frames what "healthy" looks like per game-state in Operation 5 (Diagnose Deck Balance). It sets the **expectations you bring to the pre-mortem** — it does not score or tally coverage. Map the primary archetype to its expectations:

| `PRIMARY STRATEGY` contains | Pre-mortem expectation |
|-----------------------------|------------------------|
| aggro, burn, voltron, go-wide beatdown | May run thin on Losing — speed is the plan; note it, don't auto-flag it |
| midrange, value, goodstuff, fight/theft | Wants a plan in all four game-states |
| control, stax, spellslinger-control | **Must be deep on Losing + Parity**, or it folds |
| combo, storm, cheat-into-play | Needs Development setup + a Winning payoff; protection reads as the Losing plan |

A deck's plan is judged **against its archetype's expectations**, not against flat even coverage. See `references/quadrant-theory.md` for the pre-mortem method and the reading heuristics.

## How the Tagger Uses Strategy (what the code REALLY does)

The card tagger does **not** parse the `Strategy` text and does **not** read a `Key mechanics:` line — no live deck has one, and no code looks for one. It consumes two **pre-supplied fields on the deck input dict** — `primary_strategy` (a free-text string) and `synergy_keywords` (a list) — assembled by the caller from the deck's intent (the `PRIMARY STRATEGY` archetype and the Focus Otags / `WANTS` vocabulary), not extracted by the tagger itself.

Grounded in `plugins/make-magic/scripts/card_tagger.py`:

1. **Bucket→strategy synonym overlap** (`BUCKET_STRATEGY_SYNONYMS`, `card_tagger.py:90-108`). Each otag **bucket** (`sac`, `flicker`, `burn`, …) maps to strategy synonym keywords. `compute_tag_strategy_overlap` (`card_tagger.py:259-279`) intersects each card's bucket synonyms with the deck's **`synergy_keywords`** — read at `card_tagger.py:299` and passed in at `:302-304` — NOT against a `Key mechanics` line. Overlap yields `len(overlap) * 1.5` per bucket.
2. **`primary_strategy` substring deep patterns** (`card_tagger.py:296-298`, then `:317, :328, :336, :347, :375, :385, :404, :425, :446`). The lowercased `primary_strategy` string is scanned for substrings (`"lands-matter"`, `"spellslinger"`, `"burn"`, `"firebending"`, `"voltron"`, `"lesson"`, …) to add oracle-text-signal bonuses. This is a substring scan of a free-text field, not a parse of a named line.

Nothing else parses named Strategy sections. The pipeline transforms treat `Strategy` as opaque free text: `pipeline/pipeline/contracts/models.py:220-222` declares it `strategy: str | None` "Free-text strategy"; `deck_factsheet.py` and `crispi.py` do not read it. So the `WANTS` / `DOES NOT WANT` / `KEY LINES` vocabulary reaches the tagger only indirectly, through whatever `synergy_keywords` / `primary_strategy` the caller derives from the Strategy — there is no automatic field-to-keyword parse.

The actual per-deck strategies live in Airtable, not here. Read them at runtime via `collection get-deck "<deck>" --field strategy`.
