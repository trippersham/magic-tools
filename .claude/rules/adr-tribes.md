---
paths:
  - "**/pipeline/transforms/strategy/tribes.py"
  - "**/pipeline/transforms/strategy/data/creature_types_catalog.json"
---

# Tribe resolution

Creature subtypes resolve against the Scryfall creature-type catalog allowlist (reference data), not a hand-maintained blocklist.

<tribe-resolution>

- Resolve subtypes by keeping only spans that ARE real creature types per `data/creature_types_catalog.json` (the Scryfall `/catalog/creature-types` authority), matched longest-first (so `Time Lord` resolves as one span). Reason: a hand blocklist drifts (it leaked basic land types as tribes) and can't express multi-word types; per CR 205.3 subtype→type attribution is non-positional, so the allowlist IS the resolution. Never reintroduce a `NON_CREATURE_SUBTYPES`-style blocklist or word-by-word filtering. See `plugins/make-magic/docs/adr/0005-tribe-resolution-creature-type-catalog.md`. Check: `pytest tests/test_c3_tribes.py` (incl. the Forest-Dryad + Time-Lord probes).
- The catalog is sourced reference data (fetched from the authority, committed), not a hand-list — refresh via the sync, don't hand-edit entries.
- If you change tribe resolution, update ADR 0005 in the same change and re-verify its Test.

</tribe-resolution>
