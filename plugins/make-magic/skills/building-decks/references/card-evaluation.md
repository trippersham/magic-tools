# Card Evaluation

How to evaluate cards for decks using the tagger scripts and Claude reasoning.

## When to Use Scripts vs Claude Reasoning

| Scenario | Approach |
|----------|----------|
| "Is X good in Y?" (single card) | Claude reasoning + quick tag check |
| "What should I add from set Z?" (bulk) | Tagger pipeline: tag-set + scoring |
| "What's the weakest card to cut?" | Tagger scoring across deck cards |
| "Is trading X for Y worth it?" | Claude reasoning with tag comparison |

## Script Invocation

### Tag a single card
```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/card_tagger.py tag-card "<name>"
```
Returns: name, tags (the card's otag buckets), type_line, mana_cost, cmc, color_identity, oracle_text, art_crop, scryfall_uri, power_toughness, keywords, set. (Price is not on the output — served live via `scryfall_cache.py`.)

### Tag an entire set
```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/card_tagger.py tag-set <code> --output /tmp/<code>-tagged.json
```
Returns: JSON with all cards tagged. Use for bulk recommendation generation.

### Fetch a card (raw Scryfall data)
```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_cache.py get-card "<name>"
```

## Interpreting Tags

Each card's `tags` are its **otag buckets** — functional-category membership sourced straight
from the card dim (the crosswalk over its rolled-up oracle tags), **not** confidence-scored
regex labels. The bucket vocabulary is: `removal`, `ramp`, `draw`, `tokens`, `counters`,
`burn`, `tutor`, `sac`, `counterspells`, `flicker`, `typal`, `anthem`, `combat`, `protection`,
`stax`, `extra_combat`, `wincon`. Note the granularity: fine combat labels (deathtouch, double
strike, equipment) now collapse into the single `combat` bucket. Buckets map to strategy
keywords via `BUCKET_STRATEGY_SYNONYMS`. Examples:

| Bucket | Maps to strategies |
|--------|--------------------|
| ramp | ramp, mana, big mana, lands-matter |
| flicker | blink, etb, flicker, value |
| sac | aristocrats, sacrifice, graveyard |
| combat | combat, aggro, voltron, evasion |
| tokens | tokens, go-wide, aristocrats, sacrifice |

## Scoring Tiers

When scoring cards against a deck's strategy:

| Confidence | Score | Meaning |
|------------|-------|---------|
| Very high | >= 8 | Strong multi-axis alignment — likely an upgrade |
| High | >= 5 | Clear strategy fit — worth serious consideration |
| Medium | >= 3 | Partial fit — viable but not obvious |
| Low | < 3 | Weak alignment — only if nothing better exists |

## Evaluation Workflow

1. **Read the deck's Strategy** from Airtable (`get_record` on Decks table)
2. **Parse key mechanics** from the Strategy field
3. **Tag the candidate card(s)** using the tagger
4. **Score**: bucket→strategy synonym overlap (via `BUCKET_STRATEGY_SYNONYMS`) + oracle text keyword matching + strategy-specific deep patterns
5. **Compare** against existing deck cards at the same CMC slot / role
6. **Present** verdict with specific strategy alignment rationale

## The Flexibility Test (Operations 2 & 3)

Synergy fit is one axis; **flexibility across game-states** is a second, independent one.
This is a fact-informed LLM judgment, not a numeric bonus — there is no card-scoring tally
(see `references/quadrant-theory.md` for why that premise was retired).

When you add or cut a card, ask the flexibility question:

> **Is this card live in multiple game-states, or only when I'm already ahead?**

- **Multi-quadrant = prize.** A card that helps when developing, at parity, *and* when
  behind is a deck's MVP — it never sits dead in hand. Sutcliffe's core insight, and it holds
  in Commander even though the vacuum card-scoring did not.
- **Only-when-winning = trap.** A card that only does something once you're already ahead is
  the classic bad card — it's blank exactly when you needed help. Weight these down.

Read flexibility from the fact sheet's neutral facts — keyword census, type line, and
instant-speed flag are the strongest signals (an instant-speed answer is live in more states
than a sorcery-speed one; evasion is live whenever you have a board). Do not assign a quadrant
score; make the judgment.

Rules:
- Flexibility **augments**, never replaces, the synergy judgment — a flexible card with no
  strategy fit is still a bad recommendation.
- Keep the two reasons **separate** in the output: the **synergy reason** (why it fits the
  deck's plan) and the **flexibility reason** (which game-states it's live in). Never collapse
  them into one opaque verdict.

| Scenario | Effect |
|----------|--------|
| Two candidates tie on synergy; one is live in more game-states | The flexible one wins |
| High synergy, but only live when already ahead | Recommend on synergy; flag the only-when-winning risk |
| Flexible across states, near-zero synergy | Do NOT recommend — surface as "off-strategy filler" only if nothing better exists |
