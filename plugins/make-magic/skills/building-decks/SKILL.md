---
name: building-decks
description: >
  Build, optimize, and evaluate MTG Commander decks. TRIGGER when: user asks to
  "optimize a deck", "recommend cards", "evaluate a card for a deck", "propose swaps",
  "vet a trade", "find upgrades", "what's good for [deck]", "should I run [card] in [deck]",
  "build a deck around [commander]", or any question about deck strategy and card fit.
  Also trigger for "recommend from set", "what should I add from [set]", "upgrade suggestions".
  Also trigger for deck balance / Quadrant Theory: "is my deck balanced", "diagnose [deck]",
  "what's [deck] missing", "what quadrant is weak", "what should I shore up", "is [deck] too
  glass-cannon".
user-invocable: true
---

# Building Decks

<primary-constraint>
**Never evaluate cards without reading the deck's Strategy field first.**

Why: "Good card" is meaningless without strategy context. Lightning Bolt is excellent in a burn deck, mediocre in a blink deck. Storm-Kiln Artist is a staple in spellslinger, worthless in voltron. The Strategy field in Airtable defines what makes a card good for that specific deck. Skipping this step produces generic recommendations that sound helpful but actively harm deck coherence.

Instead: Always start by reading the deck's Strategy field via `mcp__airtable__get_record` on the Decks table (`tblIfqVuVHNQza1K3`), requesting the Strategy field (`fldvJRaoYfRZiM8zw`).
</primary-constraint>

<red-flags>
If you catch yourself thinking:
- "This card is generically powerful, so it's probably good here"
- "I'll recommend this staple because it's in lots of Commander decks"
- "The user knows their deck, I'll just evaluate card quality"

**STOP.** Read the Strategy field. Evaluate fit, not power level.
</red-flags>

## Prerequisites

- **Airtable MCP connector** — enabled via `/mcp`, authenticated with Airtable account
- **Decks table populated** — each deck needs a Strategy field filled per `references/strategy-schema.md`
- **uv** — scripts use PEP 723 inline metadata; invoke with `uv run --script`

## Operation Router

| User intent | Operation |
|-------------|-----------|
| "Is X good in Y?", "Should I run [card] in [deck]?" | [1. Evaluate a Card](#1-evaluate-a-card-for-a-deck) |
| "What should I add from [set]?", "Recommend cards from [set]" | [2. Recommend from Set](#2-recommend-cards-from-a-set) |
| "What's the weakest card to cut?", "Propose a swap for [card]" | [3. Propose Swaps](#3-propose-swaps) |
| "Is trading X for Y good for deck Z?" | [4. Vet a Trade](#4-vet-a-trade) |
| "Is my deck balanced?", "diagnose [deck]", "what quadrant is weak?", "what's [deck] missing?" | [5. Diagnose Deck Balance](#5-diagnose-deck-balance-quadrant-theory) |

---

## 1. Evaluate a Card for a Deck

Ad-hoc, Claude-reasoned evaluation for single-card questions.

<evaluation-workflow>

**Step 1: Read the deck's Strategy**
```
mcp__airtable__get_record
  baseId: appw7QPMoqktrgDc1
  tableId: tblIfqVuVHNQza1K3
  recordId: <deck_record_id>
  fields: ["Name", "Strategy", "Color Identity", "Commander"]
```

If you only have the deck name, use `search_records` first:
```
mcp__airtable__search_records
  baseId: appw7QPMoqktrgDc1
  tableId: tblIfqVuVHNQza1K3
  filterByFormula: {Name} = "Ozai"
  fields: ["Name", "Strategy", "Color Identity", "Commander"]
```

**Step 2: Fetch the card from Scryfall**
```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_cache.py get-card "<card name>"
```

**Step 3: Tag the card's mechanics**
```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/card_tagger.py tag-card "<card name>"
```

**Step 4: Reason about fit**

Parse the deck's Strategy field for:
- Archetype and win conditions
- Key mechanics keywords
- "What makes a card good here" criteria
- "What doesn't fit" exclusion criteria

Compare against the card's:
- Mechanic tags (from tagger output)
- Oracle text (keyword matching)
- CMC slot and card type

<reference file="strategy-schema.md" section="Key Mechanics Vocabulary">
See strategy-schema.md for the keyword vocabulary and how tags map to strategy keywords.
</reference>

**Step 5: Present verdict**

Structure your response:
1. **Verdict** — Yes/No/Maybe with confidence
2. **Strategy alignment** — which specific mechanics/keywords match the deck's Key mechanics
3. **Role in deck** — what this card does for the deck's game plan
4. **Comparison** — is it better than existing options at this CMC/role?
5. **Caveats** — any anti-synergies or concerns

</evaluation-workflow>

---

## 2. Recommend Cards from a Set

Bulk operation for new set releases or set-specific recommendations.

<recommendation-workflow>

**Step 1: Load all deck strategies**
```
mcp__airtable__list_records
  baseId: appw7QPMoqktrgDc1
  tableId: tblIfqVuVHNQza1K3
  fields: ["Name", "Strategy", "Color Identity", "Commander"]
```

**Step 2: Tag the full set**
```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/card_tagger.py tag-set <set_code> --output /tmp/<set_code>-tagged.json
```

The tagger outputs JSON with each card's tags, type_line, mana_cost, cmc, color_identity, oracle_text, art_crop, scryfall_uri, price_usd.

**Step 3: Score cards against each deck**

For each card in the tagged set:
1. Filter by color identity — never recommend cards outside the deck's color identity
2. Look up each tag in `TAG_STRATEGY_SYNONYMS` to get strategy keywords
3. Count overlapping keywords with the deck's Key mechanics
4. Add oracle text keyword matches
5. Sum for total score
6. **Flexibility test (fact-informed judgment, not a numeric bonus):** ask whether the card is
   live in multiple game-states or only when already ahead. Multi-quadrant = prize;
   only-when-winning = trap. Keep the synergy reason and the flexibility reason separate — do
   not collapse them into one verdict.

<reference file="card-evaluation.md" section="The Flexibility Test (Operations 2 & 3)">
See card-evaluation.md for the flexibility test and quadrant-theory.md for the game-state framing.
</reference>

<reference file="card-evaluation.md" section="Scoring Tiers">
See card-evaluation.md for the scoring tier definitions (very high >= 8, high >= 5, medium >= 3).
</reference>

**Step 4: Present recommendations**

For each deck with matches:
```
### [Deck Name]
**Very High Confidence (score >= 8)**
- Card Name — [tags], [why it fits]

**High Confidence (score >= 5)**
- Card Name — [tags], [why it fits]

**Medium Confidence (score >= 3)**
- Card Name — [tags], [why it fits]
```

**Step 5: Optional — push to Chase Cards**

If approved, create Chase Card records for top recommendations:
```
mcp__airtable__create_record
  baseId: appw7QPMoqktrgDc1
  tableId: tblXsNtGgT7UQLPXZ
  fields:
    Card Name: <name>
    Target Decks: [<deck_record_id>]
    # Include Scryfall metadata...
```

<reference file="airtable-schema.md" section="Chase Cards table fields">
See airtable-schema.md for Chase Cards field IDs and structure.
</reference>

</recommendation-workflow>

---

## 3. Propose Swaps

Given a card to add, identify the weakest card to cut.

<swap-workflow>

**Step 1: Read the deck's Strategy and linked Cards**
```
mcp__airtable__get_record
  baseId: appw7QPMoqktrgDc1
  tableId: tblIfqVuVHNQza1K3
  recordId: <deck_record_id>
  # Cards field returns linked record IDs
```

Then fetch the linked cards:
```
mcp__airtable__list_records
  baseId: appw7QPMoqktrgDc1
  tableId: tbl3UgZZPJGQhEFo8
  filterByFormula: FIND(RECORD_ID(), "<comma-separated deck card IDs>")
  fields: ["Card Name", "Card Type", "CMC", "Oracle Text"]
```

**Step 2: Tag both the incoming card and existing deck cards**

For the incoming card:
```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/card_tagger.py tag-card "<incoming card>"
```

For existing deck cards, either:
- Tag individually for small sets
- Write to temp file and use `tag-file` for large sets

**Step 3: Score all cards against the deck's strategy**

Same scoring as Operation 2 — tag→synonym overlap + keyword matches.

**Step 4: Identify the lowest-scoring card in a similar role**

Similar role means:
- Same CMC bracket (0-2, 3-4, 5-6, 7+)
- Same card type category (creature, instant/sorcery, artifact, enchantment)

**Flexibility test (see Operation 2 / `card-evaluation.md`):** ask whether the incoming card
is live in multiple game-states or only when already ahead, and prefer to **cut a card that
is live in fewer states** — do not cut the deck's only answer for a game-state it's already
thin on. A swap that adds a flexible, multi-state card while cutting an only-when-winning one
is strictly better than a same-role trade. Keep synergy and flexibility rationales separate.

**Step 5: Present the swap proposal**

```
## Proposed Swap

**Add:** [Incoming Card] — score: X, fits because [strategy alignment]

**Cut:** [Weakest Card] — score: Y, underperforms because [weak alignment]

**Comparison:**
- Both are [CMC] [type]
- Incoming card adds [mechanics/keywords]
- Cut card only provides [weaker contribution]
```

</swap-workflow>

---

## 4. Vet a Trade

"Is trading X for Y a net improvement for deck Z?"

<trade-workflow>

**Step 1: Read the deck's Strategy**

Same as Operation 1.

**Step 2: Tag and score both cards**

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/card_tagger.py tag-card "<card giving up>"
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/card_tagger.py tag-card "<card receiving>"
```

Score both against the deck's strategy.

**Step 3: Compare prices**

Prices are included in the tagger output (from Scryfall cache). If not available, fetch directly:
```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_cache.py get-card "<name>"
```

**Step 4: Present trade verdict**

```
## Trade Verdict

**Giving up:** [Card A] — score: X, price: $Y
**Receiving:** [Card B] — score: X', price: $Y'

**Strategy fit delta:** [+/- points] — [better/worse] for [deck]
**Price delta:** [+/- $amount] — [gain/loss] in value

**Recommendation:** [Accept/Decline/Even] — [rationale]
```

</trade-workflow>

---

## 5. Diagnose Deck Balance (Quadrant Theory)

Whole-deck **pre-mortem**: does the deck have a *plan* for every game-state it needs, given
its archetype? This is a reasoning task, **not a card-scoring tally**. The quadrants are
questions about the deck's plan — Development (don't fall behind), Parity (break a stall),
Winning (what/how-fast/how-interruptible is the actual win), Losing (the out when behind). You
answer them by reasoning over the deck's **Strategy** plus a **neutral fact sheet** from
`deck_factsheet.py`. The output is a reasoning-authored **`Assessment`** — a narrative
pre-mortem plus a shopping list, **never a percentage table** — which you write back to the
deck's `Assessment` field in Airtable.

**`Strategy` vs `Focus Otags` vs `Assessment`** — three distinct fields on the Decks table,
and you must keep them apart:

- **`Strategy` = what the deck AIMS to be.** A human-authored aspiration (the win condition,
  archetype, key mechanics). It is the *plan* in prose, an input you read but never overwrite
  here.
- **`Focus Otags` = the otags/buckets the deck CARES about.** The deck's intended functional
  identity, expressed in the tag vocabulary — bucket names (`counters`, `tokens`, `removal`,
  `ramp`, …) and/or specific otag slugs. This is a **curated subset**: the cards underneath
  carry a much *wider* set of otags, so `Focus Otags` captures the deck's INTENT, not the
  mechanical union of everything it happens to tag. It is skill/reasoning-authored (or
  human-authored) and written to `Focus Otags` via the Airtable MCP.
- **`Assessment` = what the deck ACTUALLY is, isn't, and needs.** A reasoning SYNTHESIS *you*
  produce, measuring the actual card otags AGAINST `Focus Otags`: coverage of what you care
  about, thin/unprotected focus items, and off-plan noise. It is the *reality* measured
  against the intent.

Both `Focus Otags` and `Assessment` are **written by this skill** — you author them and write
them via the Airtable MCP (`mcp__airtable__*`), the same write path any skill uses. **The
deterministic pipeline never writes them; it only READS `Focus Otags`.** The engine's
`susceptibility` + `otag_buckets` (from `deck_factsheet`, from the cards' engine-written
`⚙ Buckets`/`⚙ Otags`) are the *wide, actual* inputs your synthesis reasons over.
Susceptibility is an INPUT to the Assessment, not a standalone Airtable field.

<reference file="quadrant-theory.md" section="The reframe: quadrants are questions, not buckets">
Read quadrant-theory.md for the pre-mortem method, the deterministic/reasoning split, the Strategy / Focus Otags / Assessment triad, the otag_buckets + susceptibility lead signals (the Assessment inputs), the actual-vs-focus signals (coverage_of_focus / thin_focus / off_focus), the fact-sheet field reference, and the limitations (notably: cEDH is out of scope, and why the card-scoring premise was retired).
</reference>

<diagnose-workflow>

**Step 1: Read the deck's Strategy → archetype and win condition**

Same `get_record` as Operation 1 (the `<primary-constraint>` applies — never diagnose
without the Strategy), but add `Focus Otags` to the `fields` list so you read the intended
identity alongside the aim (Step 3b). The Strategy *is* the plan: extract the win condition
and the `Archetype:` line, which frames the per-game-state expectations (see
`references/strategy-schema.md`).

**Step 2: Get the EXACT current decklist**

Pull the deck's linked Cards (same mechanism as Operation 3, Step 1) — do not work from
memory. Write the card names to a decklist file for the fact sheet.

**Step 3: Run the fact sheet**
```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/deck_factsheet.py factsheet <decklist> --output /tmp/<deck>-facts.json
```
This emits **neutral facts only** — curve, ramp/fixing, a keyword census, card advantage,
instant-speed, plus the two otag-derived fields that carry the diagnosis: **`otag_buckets`**
(a multi-label oracle-tag bucket → nonland-card count map — `removal`, `ramp`, `draw`,
`tokens`, `counters`, `burn`, `tutor`, `sac`, `counterspells`, `flicker`, `typal`, `anthem`,
`combat`, `protection`, …) and **`susceptibility`** (data-grounded, count-cited weakness
signals). It assigns **no** quadrant, role, or wincon. Any `missing` names are reported —
resolve or note them.

**Graceful degradation:** if the otag layer is unavailable, `otag_buckets` is `{}` and
`susceptibility` contains a single `otag layer unavailable: …` string. When you see that,
fall back to the structured facts (curve, ramp, instant-speed, keywords) and lean harder on
the Strategy — do NOT treat empty buckets as "the deck does nothing."

**Step 3b: Read or propose the deck's `Focus Otags` → its intended functional identity**

Read the deck's `Focus Otags` field alongside the Strategy (add it to the `get_record`
`fields` list in Step 1). `Focus Otags` is the curated set of buckets/otags the deck is
**built around** — its intended identity in the tag vocabulary, distinct from the wide,
actual set the cards mechanically carry.

- **If `Focus Otags` is already set**, use it as-is — it is the intent you measure against.
- **If it is empty**, propose one: read the fact sheet's `otag_buckets` (the wide actual set)
  and the Strategy's `Key mechanics` line, then **curate down** to the handful of buckets/otag
  slugs the deck is genuinely built around (e.g. a tokens/counters go-wide deck's focus is
  `tokens`, `counters`, `anthem` — not the incidental `ramp` and `removal` every deck runs).
  This is a subset expressing intent, never the mechanical union. Present the proposed focus
  to the user, then write it (Step 3c). If you cannot confidently curate one, proceed without
  it — the Assessment degrades gracefully (Step 7).

**Step 3c: Write `Focus Otags` to the Decks field (via Airtable MCP)**

Persist the curated focus to the deck's `Focus Otags` field using the normal MCP write path.
This field is **skill-authored** (or human-authored) — the deterministic pipeline READS it but
NEVER writes it. If the field does not yet exist on the Decks table, create it first
(`multipleSelects` for pick-list buckets, or `multilineText` for freeform bucket + otag-slug
lists), then populate it:

```
# Create the field once if it is not already on the Decks table:
mcp__airtable__create_field
  baseId: appw7QPMoqktrgDc1
  tableId: tblIfqVuVHNQza1K3
  name: Focus Otags
  type: multipleSelects   # or multilineText for freeform bucket + otag-slug lists

# Write / refresh the curated focus:
mcp__airtable__update_records
  baseId: appw7QPMoqktrgDc1
  tableId: tblIfqVuVHNQza1K3
  records:
    - id: <deck_record_id>
      fields:
        Focus Otags: <curated buckets/otag slugs — the deck's intended identity>
```

Never write `Focus Otags` from the mechanical union of `otag_buckets` — that would make it the
actual set, not the intended one. Curate to intent.

**Step 4: Reason the per-quadrant plan (from facts + Strategy)**

For each game-state, state the deck's plan — from the engine, not a count. Lead with
**`susceptibility`** (the deck's measurable resilience gaps — each signal cites the counts
driving it) and the **`otag_buckets`** distribution — together they are the measurable core
of "Losing is the hardest quadrant." Read a game-state's plan off the buckets that serve it
(e.g. `ramp`+curve for Development; `draw`+`tutor`+the engine buckets for Parity;
`removal`+`counterspells`+`protection`+`sac` for Losing), never a single count.

- **Development** — plan not to fall behind early? (`ramp` bucket, curve, early `removal`)
- **Parity** — plan to break a stall / grind ahead? (`draw`/`tutor` + the payoff buckets)
- **Winning** — what *is* the win, and is it resilient / fast / interruptible? (from Strategy)
- **Losing** — the out when behind / swept / raced? (`susceptibility` + `protection`/`removal`)

**When `coverage.uncategorized_pct` is high, weight the Strategy over the counts** — the
deck's value is synergy-carried and invisible even to the otag buckets. Cite the fact sheet's
numbers (a `susceptibility` signal, a bucket count) only as supporting evidence, never as a
verdict.

**Step 5: Name the loss-condition**

State the game-state where, if the game goes there, the deck loses — threat-relative to the
pod, not a flat low bar. This is the pre-mortem's payload.

**Step 6: Prescription + owned fills (read-only)**

Name the card **type** that plugs the hole. Then query the Cards inventory for
in-color-identity cards of that type that are owned/available (`Number in Library > 0`), and
flag any already on the Chase list. Prefilter by color identity + a text keyword before
fetching to keep the scan tractable.

<reference file="airtable-schema.md" section="Cards table fields">
See airtable-schema.md for the Cards table fields (Number Owned, Number in Library, Oracle Text) and Chase Cards structure.
</reference>

**Step 7: Synthesize the Assessment** (narrative + shopping list, NOT a percentage table)

This is the payload. Reason the fact-sheet inputs (`susceptibility` + `otag_buckets`, the wide
actual set) against **both** the `Strategy` (prose aim) **and** `Focus Otags` (intended
identity) into an `Assessment` — what the deck ACTUALLY is, isn't, and needs. The Assessment
now measures actual-vs-focus along three axes:

- **Coverage of focus** — for each bucket/otag in `Focus Otags`, does the actual card set
  (`otag_buckets`) back it up? A focus item with few cards behind it is intent the deck hasn't
  paid for. *(Once the pipeline exposes it, this is the `coverage_of_focus` signal — the
  mapping of each `Focus Otags` item to its actual card count.)*
- **Thin / unprotected focus** — a focus item that is present but shallow, or a payoff the
  deck cares about that has no protection/answer defending it (cross-reference
  `susceptibility`). This is susceptibility scoped to what the deck *intends*. *(Pipeline
  signal: `thin_focus`.)*
- **Off-focus noise** — prominent card otags/buckets that sit OUTSIDE `Focus Otags`: mechanical
  weight the deck spends on things it doesn't claim to care about. *(Pipeline signal:
  `off_focus`.)*

These signals are the pipeline's future READ-only outputs derived from `Focus Otags` +
`otag_buckets`; describe them plainly from the two sets until they land in the fact sheet. Use
this shape:

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

Present it to the user, and — because it is the deck's living reality-check — persist it.

**Step 8: Write the Assessment to the Decks `Assessment` field (via Airtable MCP)**

Persist the synthesis to the deck's `Assessment` field (Decks table, long text) using the
normal MCP write path. If the `Assessment` field does not yet exist on the Decks table, create
it first, then populate it:

```
# Create the field once (long text) if it is not already on the Decks table:
mcp__airtable__create_field
  baseId: appw7QPMoqktrgDc1
  tableId: tblIfqVuVHNQza1K3
  name: Assessment
  type: multilineText

# Write / refresh the synthesis:
mcp__airtable__update_records
  baseId: appw7QPMoqktrgDc1
  tableId: tblIfqVuVHNQza1K3
  records:
    - id: <deck_record_id>
      fields:
        Assessment: <the pre-mortem synthesis from Step 7>
```

Never overwrite `Strategy` — `Strategy` is the human-authored aim (an input); `Focus Otags` is
the deck's intended identity (skill/human-authored, pipeline read-only); `Assessment` is
*your* reasoning-authored reality-check. They are three separate fields and serve separate
purposes.

**Graceful degradation:** if `Focus Otags` is unset and you could not confidently propose one,
synthesize the `Assessment` from `susceptibility` + `otag_buckets` + Strategy alone (the
pre-focus method) — just omit the coverage-of-focus / thin-focus / off-focus lines. If the
otag layer itself is unavailable (empty `otag_buckets`, `susceptibility` carrying the `otag
layer unavailable` signal), still synthesize and write an `Assessment` — just build it from
the structured facts (curve, ramp, instant-speed, keywords) plus the Strategy. The Assessment
is thinner and less richly grounded, but it is still written from structured facts +
reasoning, not skipped.

</diagnose-workflow>

---

## Critical Constraints

<constraint name="color-identity">
**Never recommend cards outside the deck's color identity.**

Why: Commander format rules require every card to be within the commander's color identity. A card with {U} in its mana cost or rules text cannot go in a Golgari (BG) deck. The tagger outputs `color_identity` for every card — always filter against the deck's Color Identity field.
</constraint>

<constraint name="runtime-strategy">
**Strategy lives in Airtable, not in this skill.**

Why: Strategies evolve. Decks get rebuilt. Hardcoding strategy keywords produces stale recommendations. Always read the Strategy field from the Decks table at runtime.

See `references/strategy-schema.md` for the strategy field convention and keyword vocabulary.
</constraint>

<constraint name="dfc-handling">
**For double-faced cards, check `card_faces[0]` when top-level fields are null.**

Why: Scryfall returns `null` for `image_uris`, `mana_cost`, and `oracle_text` at the top level for DFCs. The data lives in `card_faces[0]`. The tagger handles this automatically, but if fetching raw Scryfall data, check both locations.
</constraint>

<constraint name="get-vs-list">
**Use `get_record` for single deck lookups, not `list_records` + filter.**

Why: `get_record` is faster and returns exactly one record. `list_records` with a filter returns a paginated list and costs more tokens. When you have the record ID, use `get_record`.

<reference file="airtable-patterns.md" section="Query Optimization">
See airtable-patterns.md for efficiency patterns.
</reference>
</constraint>

---

## Reference Guides

| When you need to... | Read |
|---------------------|------|
| Understand strategy field format and keyword vocabulary | [references/strategy-schema.md](references/strategy-schema.md) |
| Diagnose deck balance (Quadrant Theory pre-mortem, fact-sheet fields) | [references/quadrant-theory.md](references/quadrant-theory.md) |
| Invoke tagger scripts or interpret scoring tiers | [references/card-evaluation.md](references/card-evaluation.md) |
| Look up Airtable table/field IDs | [references/airtable-schema.md](references/airtable-schema.md) |
| Optimize Airtable queries or handle edge cases | [references/airtable-patterns.md](references/airtable-patterns.md) |
