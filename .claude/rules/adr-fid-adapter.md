---
paths:
  - "**/pipeline/transforms/strategy/fid_adapter.py"
  - "**/pipeline/transforms/strategy/data/fid_event_adapter.json"
---

# fid-to-Event adapter

The fid adapter is production data consumed by `combo_win.py` (ADR 0016). Each of the 1,348 Spellbook features maps to `{event, magnitude, scope, object_descriptor}` from the bounded trigger taxonomy (ADR 0012).

<fid-adapter>

- The adapter schema is `{fid, name, event, magnitude, scope, object_descriptor, note}`. Do not add fields or change the schema without updating ADR 0007. Reason: the schema is fixed and the walk depends on it.
- Adding a new Spellbook feature is a DATA change (add a row to `fid_event_adapter.json`), not a code change. Reason: separation of data and logic (ADR 0007).
- The `event` field must be a slug from the bounded event catalog (`event_catalog_v4.json`, ADR 0012) or `None`. No ad-hoc event slugs. Reason: bounded taxonomy.
- If you change the adapter data or loader, update ADR 0007 in the same change. Reason: the decision's one home is the record.

</fid-adapter>
