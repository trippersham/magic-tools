---
name: building-decks
description: >
  Build, optimize, and evaluate MTG Commander decks. TRIGGER when: user asks to
  "optimize a deck", "recommend cards", "evaluate a card for a deck", "propose swaps",
  "vet a trade", "find upgrades", "what's good for [deck]", "should I run [card] in [deck]",
  "build a deck around [commander]", or any question about deck strategy and card fit.
  Also trigger for "recommend from set", "what should I add from [set]", "upgrade suggestions".
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
| Invoke tagger scripts or interpret scoring tiers | [references/card-evaluation.md](references/card-evaluation.md) |
| Look up Airtable table/field IDs | [references/airtable-schema.md](references/airtable-schema.md) |
| Optimize Airtable queries or handle edge cases | [references/airtable-patterns.md](references/airtable-patterns.md) |
