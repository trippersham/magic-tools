---
paths:
  - "**/pipeline/transforms/strategy/machine_vocab.py"
---

# Canonical normalization

The machine-authored attribute type and its provisional store are named by concept and lifecycle, not by authoring mechanism.

<canonical-normalization>

- **`CardAttribute`** (renamed from `MachineAttribute`) is the canonical name for a provenance-tagged card attribute with event roles. It defaults to `source='haiku'`.
- **`ProvisionalAttributeStore`** (renamed from `MachineStore`) is the canonical name for the in-memory provisional attribute store, named by lifecycle (design-3 section 5).
- **`Provenance.source`** admits `'curated'`, `'haiku'`, and `'spellbook'`. Curated vs authored is always distinguishable.
- **No backward-compat aliases.** `MachineAttribute` and `MachineStore` do not exist. One name per concept.
- **Serialization is unchanged**: the JSONL on-disk format is the same flat record schema. `_record_to_attr` still reads both `produced_event` and `effect_event` for backward compat.
- If you change the attribute type, store, or provenance source enum, update ADR 0008 in the same change.

</canonical-normalization>
