# Deck Strategy Schema

The `Strategy` field on each Deck record in Airtable (`fldvJRaoYfRZiM8zw`) is the source of truth for what a deck optimizes for. This document defines the convention for writing and reading strategies.

## Convention

Each strategy entry follows this structure:

```
Commander: <name> (<color identity>)
Archetype: <primary archetype> / <secondary if applicable>
Win conditions: <how the deck wins>
Key mechanics: <comma-separated keywords matching TAG_STRATEGY_SYNONYMS vocabulary>
Lines:
- <line of play 1>
- <line of play 2>
What makes a card good here: <positive selection criteria>
What doesn't fit: <negative selection criteria>
```

## Key Mechanics Vocabulary

The `Key mechanics` line uses keywords that map to the card tagger's `TAG_STRATEGY_SYNONYMS` dictionary. Common keywords:

- **Spellslinger axis:** spellslinger, instant, sorcery, noncreature, prowess, magecraft, copy, storm, lesson
- **Blink axis:** blink, etb, flicker, stax, protection, evasion, value
- **Aristocrats axis:** aristocrats, sacrifice, graveyard, drain, death trigger, tokens
- **Counters axis:** counters, +1/+1, -1/-1, voltron, proliferate, wither
- **Burn axis:** burn, firebending, big mana, direct damage, x-cost, impulse draw, treasure
- **Combat axis:** voltron, equipment, aura, combat, double strike, trample
- **Value axis:** ramp, mana, lands-matter, landfall, card advantage, recursion

## Reference Examples

### Sokka (Jeskai Spellslinger)

```
Commander: Sokka (URW)
Archetype: Spellslinger / Noncreature matters
Win conditions: Prowess/Magecraft triggers overwhelm; copied spells for burst damage; token army from spell-triggered generators
Key mechanics: spellslinger, instant, sorcery, noncreature, prowess, magecraft, copy, storm, tokens, lesson
Lines:
- Chain cheap instants/sorceries to trigger Magecraft and Prowess across multiple creatures
- Copy high-impact spells (extra combats, burn, draw) for exponential value
- Generate Treasure tokens to fuel big X-cost finishers or multi-spell turns
What makes a card good here: Rewards casting noncreature spells (Magecraft, Prowess, cast triggers). Cheap instants/sorceries that replace themselves. Treasure generation for mana-positive spell chains. Copy effects. X-cost instants/sorceries as finishers.
What doesn't fit: Creature-heavy strategies, combat tricks, auras/equipment (wrong axis), cards that need board presence to function.
```

### Ozai (Rakdos Burn / Big Mana)

```
Commander: Ozai (BR)
Archetype: Burn / Big Mana / Firebending
Win conditions: X-cost burn spells fueled by ramp and cost reduction; direct damage accumulation; punisher effects
Key mechanics: burn, firebending, big mana, direct damage, removal, ramp, mana, x-cost, impulse draw, treasure
Lines:
- Ramp aggressively (Treasure, rituals, cost reduction) into devastating X-cost burn
- Impulse draw to maintain fuel while pressuring life totals
- Punisher/group slug effects for passive damage while setting up big turns
What makes a card good here: Direct damage (especially X-cost or scaling). Treasure/mana generation. Cost reduction. Impulse draw. Cards that turn mana advantage into damage.
What doesn't fit: Defensive creatures, lifegain, incremental value engines, anything that wins slowly.
```

### Shelob Deathtouch Engine (Golgari Fight/Theft)

```
Commander: Shelob, Child of Ungoliant (BG)
Archetype: Deathtouch / Fight / Theft
Win conditions: Deathtouch + fight effects as repeatable removal; Shelob converts dying enemy creatures into Food then into stolen copies; value engine grinds opponents out
Key mechanics: deathtouch, fight, removal, theft, sacrifice, food, tokens, aristocrats, graveyard
Lines:
- Use deathtouch creatures + fight spells to destroy any creature for 1-2 mana
- Shelob turns enemy creatures into Food tokens, then those Food tokens into copies
- Sacrifice outlets and death triggers generate value from stolen creatures
What makes a card good here: Has deathtouch. Fight or bite effects. Theft (gain control). Sacrifice outlets. Death triggers. Food synergy. Low-cost removal.
What doesn't fit: Big dumb beaters without keywords, +1/+1 counter strategies, lifegain-focused cards, equipment/voltron.
```

## How the Tagger Uses Strategy

The card tagger's `TAG_STRATEGY_SYNONYMS` maps mechanic tags (like "Magecraft") to strategy keywords (like "spellslinger"). When scoring a card for a deck:

1. The card's mechanic tags are looked up in `TAG_STRATEGY_SYNONYMS`
2. Each tag's synonym list is compared against the deck's `Key mechanics`
3. Overlap generates a score — more overlapping keywords = higher score
4. Strategy-specific deep patterns provide additional scoring bonuses

The actual per-deck strategies live in Airtable, not here. Read them at runtime via `get_record` on the Decks table.
