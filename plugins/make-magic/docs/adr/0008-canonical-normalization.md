# 0008 Canonical normalization: `CardAttribute` (one concept, provenance-tagged) and `ProvisionalAttributeStore` (lifecycle-named storage)

| Field | Value |
|---|---|
| Status | Accepted. Renames applied in Phase 2A task 3. |
| Review | pending owner review: agent-authored from design-3 sections 3.4 and 5; records the normalization as built. |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests/test_canonical_normalization.py -q` (rename imports, round-trip, spellbook source widen, no-alias assertions). Parity gate: both P0 SHAs unchanged (`a6dfa607...` engine, `7d35fe19...` resolve). |
| Code paths | [`machine_vocab.py`](../../pipeline/pipeline/transforms/strategy/machine_vocab.py) (`CardAttribute`, `ProvisionalAttributeStore`, `Provenance`) |

## Context

The strategy engine's machine-authored attribute type was named `MachineAttribute` and its
in-memory store `MachineStore`. These names reflected the authoring mechanism (haiku, i.e.
"machine") rather than the concept (a card's provenance-tagged attribute) or the lifecycle
stage (provisional, awaiting verification). Design-3 section 3.4 established
`CardAttribute` as the general primitive -- a provenance-tagged card attribute with event
roles -- and section 5 established that storage is chosen by lifecycle, not by author.

The `Provenance.source` field only admitted `'curated'` and `'haiku'`. The upcoming Spellbook
adapter (Phase 2B) introduces combo-database-derived edges that are neither hand-curated nor
LLM-authored, requiring a third source value.

## Decision

### Rename: attribute type

`MachineAttribute` is renamed to `CardAttribute`. It is a provenance-tagged card attribute
with event roles (trigger/produced) and the same query/name structure as curated synth
attributes. The name reflects the concept, not the authoring mechanism. **No backward-compat
alias** -- a clean one-name rename; the old name does not exist after the change.

### Rename: store

`MachineStore` is renamed to `ProvisionalAttributeStore`. The name reflects the lifecycle
stage (design-3 section 5: storage is chosen by lifecycle -- provisional vs verified -- not
by authoring mechanism). **No backward-compat alias.**

### Widen: provenance source

`Provenance.source` now admits `'spellbook'` in addition to `'curated'` and `'haiku'`.
This is additive -- nothing emits `'spellbook'` yet; the Spellbook fidelity adapter (2B)
will use it. The curated-vs-authored distinguishability invariant is preserved:
`'curated'` remains distinct from `'haiku'` and `'spellbook'`.

### Serialization compatibility

The on-disk JSONL format is unchanged. `persist()` writes flat records with the same keys.
`_record_to_attr()` / `load_store()` continue reading existing stored files without
migration. The `produced_event` / `effect_event` backward-compat read (from 2A.1) is
also unaffected.

## Reasons

- Concept-first naming (ADR 0001): a `CardAttribute` is what it IS, not how it was authored;
  the source field already distinguishes the mechanism.
- Lifecycle-based storage (design-3 section 5): a `ProvisionalAttributeStore` holds provisional
  items regardless of whether they came from haiku, spellbook, or a future source.
- One name (ADR 0001, clause 3): no compat alias; the codebase uses exactly one symbol for each
  concept, and the old name is dead.

## Options not taken

- **Keep `MachineAttribute` with a compat alias.** Rejected: two names for one concept violates
  one-named-impl; the module is not imported by any external consumer.
- **Rename `MachineStore` to `AttributeStore`.** Rejected: too generic; `ProvisionalAttributeStore`
  names the lifecycle stage that distinguishes this store from the curated base.

## Consequences

- All importers updated in the same change (relay.py, skill_authoring.py, engine_report.py,
  and all test files).
- ADRs 0011 and 0017 updated in the same change to reflect the new names.
- The `.claude/rules/adr-edge-derivation.md` rule updated to use `CardAttribute` / `ProvisionalAttributeStore`.
- Output-preserving: both P0 hermetic SHAs identical.

## Related records

- 0001 -- knowledge placement; one name per concept, provenance on every authored fact.
- 0003 -- byte-parity held files; the renames do not touch the curated layer.
- 0011 -- EventEffect guards; `CardAttribute.produced_event` references updated.
- 0017 -- edge derivation; `CardAttribute` / `ProvisionalAttributeStore` references updated.
