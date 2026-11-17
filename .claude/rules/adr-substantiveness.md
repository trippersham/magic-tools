---
paths:
  - "**/pipeline/transforms/strategy/substantiveness.py"
  - "**/pipeline/transforms/strategy/strategy_graph.py"
---

# Substantiveness gate

Identity archetypes are earned through one generic evaluator over a closed kind-set plus per-archetype data — never archetype-name branches, never an inline re-type of the presence gate.

<substantiveness-model>

- Judge substance only through `Requirement(kind, params)` dispatched by `_EVALUATORS` over the closed set `{member_count, attr_density, axis_density}`, selected by `requirement_for(arch, cfg)` from DATA. Reason: an `arch == '<name>'` branch, or an inline re-implementation of a Requirement, is mechanic-logic-as-code that drifts. Instead: add a kind (shared, once) or a data Requirement. See `plugins/make-magic/docs/adr/0002-substantiveness-generic-evaluator.md`. Check: `uv run --extra dev pytest pipeline/transforms/strategy/tests -k substantiveness`.
- Never re-inline the presence gate `n >= 3 or (has_cmd and n >= 2)`. It has one home (`requires_met`; the planned `node_eligible`, ADR 0004). Reason: it already exists four times and the copies drift. Check: a single defining site (the planned governed-path/duplication check).
- If you change the substantiveness model, update ADR 0002 in the same change and re-verify its Test. Reason: the decision's one home is the record.

</substantiveness-model>
