---
paths:
  - "**/pipeline/transforms/strategy/data/event_catalog_v4.json"
  - "**/pipeline/transforms/strategy/substantiveness.py"
  - "**/pipeline/transforms/strategy/facts.py"
---

# Event ontology

An Event is a node from the game's bounded trigger taxonomy (ADR 0012). Reachability is pure topology — derived from edges, never an authored `role`.

<event-ontology>

- Event nodes carry **only** `{rule, produced_by, triggers}` + optional `abbrev`. No `role`, no `terminal`.
- **`polarity` / `scope` are reserved for `game-end` only.** No other event may carry them.
- **Promiscuity** (a popular event produced/triggered by many attrs) is gated by a higher `Requirement` in `build_substantiveness()` — DATA in the existing ADR 0002 evaluator, not new machinery.
- **Mechanic-class is a CardAttribute, not an Event.** Saga → `counter-placed` (lore counters); dice/dungeon → own event or `None`; the mechanic-class itself is a CardAttribute query operand (ADR 0011/0008). This keeps the event taxonomy bounded and game-rules-derived.
- **Do not add `role`, `terminal`, or any new field** to event nodes without updating ADR 0012 and the invariant test (`test_event_ontology.py`).
- If you change the event catalog schema, update ADR 0012 in the same change and re-verify `test_event_ontology.py` + the parity oracle.

</event-ontology>
