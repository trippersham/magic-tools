# 0005 Tribe resolution: creature subtypes via the Scryfall creature-type catalog allowlist, not a hand-maintained blocklist

| Field | Value |
|---|---|
| Status | Accepted. Built in the consolidation (output-changing fix). |
| Review | pending owner review: agent-authored; records the fix as built. |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests/test_c3_tribes.py -q` — includes two probe tests that lock the fix: `Land Creature — Forest Dryad` → `['Dryad']` (no `Forest` leak) and `… — Time Lord …` → `['Time Lord']` (one span, not `['Time','Lord']`). Full-corpus proof: exactly 2/170 decks shed a spurious `Forest` tribe, no classification change. |
| Code paths | [`tribes.py`](../../pipeline/pipeline/transforms/strategy/tribes.py), [`data/creature_types_catalog.json`](../../pipeline/pipeline/transforms/strategy/data/creature_types_catalog.json) |

## Context
`creature_subtypes()` word-split the type-line's post-dash span and kept words NOT in a
hand-maintained `NON_CREATURE_SUBTYPES` blocklist. Two live bugs: the blocklist was
**missing the basic land types** (so `Land Creature — Forest Dryad` leaked `Forest` as a
tribe), and word-splitting **can't represent two-word types** (`Time Lord` → `['Time','Lord']`).
Per CR 205.3, subtype→type attribution is **non-positional**, so no amount of type-line
filtering ("resolve the primary type first") can assign `Forest` to Land vs Creature.

## Decision
Resolve creature subtypes against the **Scryfall creature-type catalog as an allowlist** —
keep only spans that ARE real creature types, matched **longest-first** (so the only
multi-word type, `Time Lord`, resolves as one span). The catalog is **reference data**
(ADR 0001 category ④): `data/creature_types_catalog.json`, 377 entries fetched once from
`https://api.scryfall.com/catalog/creature-types` with provenance (`object`/`uri`/
`total_values` + sorted `data`), loaded at import. The hand `NON_CREATURE_SUBTYPES`
blocklist is deleted.

## Reasons
The authority's catalog **is** the resolution — a hand blocklist drifts (it already missed
every basic land type) and cannot express multi-word types. Keeping it as a sourced
reference file (not a hand-list, not a runtime live-fetch) follows the ④ discipline.

## Options not taken
- **Extend the blocklist** (add the basic lands, etc.). Rejected: unbounded, and CR 205.3
  makes positional/type-line disambiguation impossible regardless.
- **Resolve the primary card type first, then attribute subtypes.** Rejected: subtype→type
  attribution is non-positional (CR 205.3) — unimplementable from the type line.

## Consequences
- Output-changing fix: exactly **2/170** decks (`gruul__rydia`, `mono-green__selvala`) drop a
  spurious lower-ranked `Forest` tribe; **no** `named_strategy`/identity/confidence/winning-tribe
  changed (Forest never won the vote). Re-baselined with A7 (ADR 0003).
- Invisible to the 17-deck hermetic oracle (no Forest/Island land-creatures or Time Lord in
  the fixtures); the delta lands entirely on the full 170-deck corpus.
- New reference-data file; its refresh should join the unified `ReferenceSource` sync (future
  work, anchored by ingest issue #68).

## Related records
- 0001 — reference data (category ④), the doctrine this applies.
- 0003 — the parity re-baseline (resolve + full-corpus) covering this + A7.
