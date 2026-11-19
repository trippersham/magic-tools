---
paths:
  - "**/pipeline/transforms/strategy/strategy_graph.py"
  - "**/pipeline/transforms/strategy/engine_report.py"
---

# Node predicates

Two DISTINCT deck-level rules, each with exactly one named implementation. Do not merge them.

<node-predicates>

- The substantiveness presence gate is `determine_node_eligible(support, is_cmd)` = `support >= 3 or (is_cmd and support >= 2)`. `requires_met`, `lands_signal_present`, and `otag_typal_registered` call it. Reason: one named impl per rule (ADR 0001 clause 4); inline copies drift. See `plugins/make-magic/docs/adr/0004-node-predicates.md`.
- Graph node admission is `determine_node_admitted(support, is_cmd)` = `support >= 3 or is_cmd` (a commander-carried attr is admitted at ANY support count). The `nodes` set and `engine_report`'s machine-attr admission call it. Reason: DISTINCT from `determine_node_eligible` — NEVER merge them; merging forces commander admission to require >= 2 and silently drops single-commander-attr nodes (a behavior change). Check: the strategy suite + the parity oracle.
- If you change either predicate, update ADR 0004 in the same change. Reason: the decision's one home is the record.

</node-predicates>
