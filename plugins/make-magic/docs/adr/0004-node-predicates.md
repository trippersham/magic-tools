# 0004 Node predicates: one `node_eligible` (presence gate) and one `node_admitted` (graph admission) — two distinct rules, each with a single implementation

| Field | Value |
|---|---|
| Status | Accepted. Built in the consolidation. |
| Review | pending owner review: agent-authored; records the model as built in this change. |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests -q` (the suite covers both predicates via `resolve()` and `engine_report`); the parity oracle proves `node_eligible` preserves `resolve()` output (resolve SHA unchanged). |
| Code paths | [`strategy_graph.py`](../../pipeline/pipeline/transforms/strategy/strategy_graph.py) (`node_eligible`, `node_admitted`, and their call sites), [`engine_report.py`](../../pipeline/pipeline/transforms/strategy/engine_report.py) (machine-attr admission) |

## Context
Two deck-level predicates were each duplicated. The **presence gate**
`support >= 3 or (is_cmd and support >= 2)` appeared three times in `strategy_graph.py`
(`requires_met`, `lands_signal_present`, `otag_typal_registered`). The
**graph-admission** rule `support >= 3 or is_cmd` (commander admitted at *any*
support count) appeared twice (`strategy_graph.py`'s `nodes` set and `engine_report`'s
machine-attr admission). A prior plan draft conflated these as one rule in four
places — but they differ: a commander-carried attr is *admitted to the graph* at
any support count, while it is only *present/eligible* at support >= 2.

## Decision
Two named module-level predicates in `strategy_graph.py`:
- `node_eligible(support_count, is_cmd) -> support_count >= 3 or (is_cmd and support_count >= 2)`
  — the substantiveness presence gate; `requires_met`, `lands_signal_present`, and
  `otag_typal_registered` all call it.
- `node_admitted(support_count, is_cmd) -> support_count >= 3 or is_cmd` — graph node
  admission, commander unconditional; the `nodes` set and `engine_report`'s
  machine-attr admission both call it.

They are **deliberately distinct**. Each rule has exactly one implementation (ADR 0001
clause 4).

## Reasons
Clause 4: one named implementation per deck rule, so a future change to either bar
happens once. Keeping them *separate* is load-bearing: merging them would force
commander-carried graph admission to require support >= 2, dropping single-commander-attr
nodes and changing engine output.

## Options not taken
- **Merge into one `node_eligible` ×4.** Rejected: the two rules differ on the commander
  support count; merging is a behavior change (drops nodes), not a dedup. The parity
  oracle would catch it, but the distinction is the point.

## Consequences
- Output-preserving: the strategy suite stays green and both P0 parity SHAs are
  identical after the change.
- `engine_report`'s copy was fixed while the engine layer is still uncommitted — the same
  "a new layer re-types a facts-layer predicate" pattern that produced the combo-litmus
  drift, caught before it fossilized.

## Related records
- 0001 — clause 4 (one named implementation).
- 0002 — substantiveness; `requires_met` (the presence gate) feeds `st['requires_met']`.
- 0003 — `strategy_graph.py` is byte-frozen; this change is output-preserving (SHA unchanged).
