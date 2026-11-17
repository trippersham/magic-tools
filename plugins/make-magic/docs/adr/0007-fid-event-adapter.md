# 0007 Spellbook fid-to-Event adapter: production data layer for combo win-detection

| Field | Value |
|---|---|
| Status | Accepted. In force on branch `feat/strategy-engine`. |
| Review | pending owner review: agent-authored as part of the Phase 2B step 5b-ii win-walk flip. |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests/test_fid_adapter.py -q` (adapter coverage + schema assertions). The adapter is consumed by `combo_win.py` (ADR 0016) and transitively by the parity oracle. |
| Code paths | [`fid_adapter.py`](../../pipeline/pipeline/transforms/strategy/fid_adapter.py) (loader), [`data/fid_event_adapter.json`](../../pipeline/pipeline/transforms/strategy/data/fid_event_adapter.json) (the 1,348-row mapping). |

## Context

Commander Spellbook publishes combo variants with typed **produced features**,
each identified by a numeric `fid` and a human-readable name (e.g. fid 1 =
"Near-infinite damage", fid 11 = "Infinite colorless mana"). The strategy
engine needs to classify what each combo *does* — whether it reaches game-end
through rules, combat, or a deck card — which requires mapping those features
to the engine's bounded trigger taxonomy (ADR 0012).

The adapter was promoted from the validated research classifier
(probe2/classify2 + val/outcome_map) into production data.

## Decision

A single JSON data file (`data/fid_event_adapter.json`) maps every Spellbook
feature to its production classification:

```
fid  ->  {event, magnitude, scope, object_descriptor}
```

Where:
- **`event`** is a slug from the bounded trigger taxonomy (ADR 0012), or `None`
  for features with no engine role (oneshot effects, card-class enablers, inert
  symmetrics).
- **`magnitude`** is `'infinite'` | `'near-infinite'` | `'finite'` | `'conditional'`.
- **`scope`** is `'table'` | `'target'` | `'self'` | `'unscoped'`.
- **`object_descriptor`** is an optional dict carrying structured attributes of
  the produced object (e.g. `{keywords: ['haste']}` for token-created features).

The loader (`fid_adapter.py`) exposes a cached `load_adapter()` returning
`{fid: FidMapping}` and lookup helpers (`fids_for_event`, `coverage_stats`).

### Owner-ruled dispositions

During promotion, ambiguous features received owner-ruled classifications:

- ETB-LTB bucket split: death→`permanent-dies`, LTB→`permanent-leaves-battlefield`,
  landfall→`land-enters-battlefield`, ETB→`creature-enters-battlefield`.
- Scry/surveil → `None` (distinct selection, not `draw-a-card`).
- Bounce → `permanent-leaves-battlefield`.
- Life-total-set → `life-loss-drain` (pragmatic: threatens same SBA).
- Saga/dice/dungeon: NOT lumped — saga→`counter-placed` (lore counters),
  die-rolled/ventured/dungeon→`None` (separate events, not yet in catalog).
  See ADR 0012 mechanic-class clause.

## Reasons

- **Separation of data and logic.** The 1,348 mappings are data, not code. A JSON
  file is inspectable, diffable, and can be updated without touching the walk logic.
- **Bounded taxonomy.** Each fid maps to an event from the fixed catalog (ADR 0012)
  or to `None`. No ad-hoc event slugs.
- **Cached, immutable at runtime.** `@cache` on `load_adapter()` means the mapping
  is loaded once and never changes during a run.

## Options not taken

- **Resolve events from feature name strings at runtime.** Rejected: brittle
  (Spellbook can change names), slow, and not auditable.
- **Embed the mapping inside `combo_win.py`.** Rejected: 1,348 rows of data belong
  in a data file, not inline code.

## Consequences

- The adapter is consumed by `combo_win.py` (ADR 0016), which is the authoritative
  win-detection path imported by `strategy_graph.py`.
- Adding a new Spellbook feature requires adding its row to
  `fid_event_adapter.json` (a data change, not a code change).
- The adapter schema (`{fid, name, event, magnitude, scope, object_descriptor}`)
  is fixed; changing it requires updating this record.

## Later changes

- **2026-11-17 — fid remap for the monarch/extra-turn split (#74 Tier-A).** The four turn
  fids 42 (`Infinite turns`), 621 (`Near-infinite turns`), 1183 (`Infinite turns after one
  turn cycle`), 1742 (`Extra Turn Spell`) had `event: "monarch-or-extra-turn"`; that catalog
  slug was split (ADR 0012) and all four now carry `event: "extra-turn"`. Magnitudes/scopes/
  routes unchanged (1742 stays `finite` → oneshot; the three unbounded stay → `turn-monopoly`).
  No monarch fids exist (verified). `combo_win.fid_route`'s branch renamed to match (ADR 0016).
  Data-only change to the `event` field; carried under the Tier-A re-baseline (ADR 0003).

## Related records

- 0012 — Event ontology: the bounded trigger taxonomy the adapter maps into.
- 0016 — Closure-mechanism win-walk: the consumer of this adapter.
- 0003 — Byte-parity: the adapter is a held data file; changes re-baseline.
