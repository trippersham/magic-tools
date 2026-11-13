---
paths:
  - "**/pipeline/transforms/strategy/edge_roles.py"
  - "**/pipeline/transforms/strategy/machine_vocab.py"
  - "**/pipeline/transforms/strategy/relay.py"
---

# Within-card edge derivation

A within-card edge is an `EventEffect` (`trigger_event → produced_event` + provenance + guard scaffolding). It has exactly TWO legitimate sources — never a third.

<edge-derivation>

- **Native first**: if native otags/CardAttributes name BOTH ends of the edge, author a curated `EventEffect` with `Provenance(source='curated')`. Otags name the TRIGGER end (e.g. `cast-trigger-you`) but usually NOT the structured PRODUCED end (token vs draw vs damage); when either end isn't natively expressible, native does not apply.
- **Else haiku**: author the edge as a `CardAttribute` in the `ProvisionalAttributeStore` with `Provenance(source='haiku', lifecycle='provisional')`. It feeds the SAME deterministic walk via `build_relay_augmented_event_edges`, so the engine still falls out of graph math.
- **NEVER parse `oracle_text` with bespoke regex/NLP in the transform layer** to synthesize an edge. Card text is read by the Scryfall query evaluator (`query.py`, membership) and by haiku (edge authoring) — not by a regex here. Reason: brittle (attack-vs-cast mis-parse), DFC-blind (#68), provenance-less. This is the same anti-pattern the engine already rejected for card membership. See `plugins/make-magic/docs/adr/0017-edge-derivation-native-then-haiku.md`.
- **Two haiku roles, ranked**: (A) edge-author (bounded structured edge → deterministic walk) is PRIMARY; (B) freeform engine-namer (names the engine from the decklist) is a separate LAST-RESORT fallback and must not substitute for (A) where (A) can author the edge.
- If you change the derivation path, update ADR 0017 in the same change.

</edge-derivation>
