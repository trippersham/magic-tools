# 0002 Substantiveness is one generic evaluator over a closed kind-set plus per-archetype data Requirements, never archetype-name branches

| Field | Value |
|---|---|
| Status | Accepted. Built at commit `0fca2ff`; in force. |
| Review | ratified by the owner on 2026-11-11. (Agent-authored; owner-ratified.) |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests -k substantiveness`. Asserts the kind evaluators, `FAMILY_PREDICATES`, and `DEFAULT_REQUIREMENT`. |
| Code paths | [`substantiveness.py`](../../pipeline/pipeline/transforms/strategy/substantiveness.py), [`facts.py`](../../pipeline/pipeline/transforms/strategy/facts.py) (`requires_met` / the presence gate; shared with 0003 and the planned 0004). |

## Context
Identity archetypes must be *earned* (a deck has to actually do the thing), and the bar
differs by archetype (payoff count, family density, a degree axis). The failure mode to
avoid is a growing chain of `if arch == '<name>'` substantiveness branches — mechanic
knowledge as code, un-extendable without editing the algorithm.

## Decision
Substantiveness is a `Requirement(kind, params)` dispatched by `_EVALUATORS` over a
**closed kind-set** — `{member_count, attr_density, axis_density}` — with **zero
archetype-name branches**. `requirement_for(arch, cfg)` selects an archetype's Requirement
by **data**: the EDHREC-new set → `member_count(3, noncmd=2)`; the `build_substantiveness`
map → the few density/axis bars; everything else → `DEFAULT_REQUIREMENT =
member_count(3, noncmd=0)`. The one family predicate (`is_graveyard_attr` in
`FAMILY_PREDICATES`) is deliberately **left as code** — a single family, so rule-of-two
does not earn a `{slugs}` table.

## Reasons
Mechanism-vs-parameters: one algorithm over a closed kind-set is auditable and extended by
adding a *kind* (shared, once) or a *data Requirement*, never a per-archetype branch. This
is the reference implementation of ADR 0001's doctrine.

## Options not taken
- **Per-archetype inline substantiveness** (an `arch == …` branch, or re-implementing the
  presence gate `n >= 3 or (has_cmd and n >= 2)` at each site). Rejected: that is exactly
  the copy-paste debt the fourth clause forbids — it already recurred (the presence gate
  exists four times; the consolidation collapses it, ADR 0004).

## Consequences
- `member_count(3, noncmd=0)` **short-circuits** (the `3` is inert when `noncmd <= 0`), so
  `satisfied == requires_met` for the default bar — which is why the spellslinger
  last-resort fallback is provably dead (deleted in the consolidation).
- `_gy_substantive` in `facts.py` is a parallel inline copy of `_eval_attr_density`; the
  consolidation deletes it and derives `gy_demoted` from `requirement_for()`.

## Related records
- 0001 — the doctrine this implements.
- 0004 (planned) — the presence gate `node_eligible` dedup routes through this framework.
- 0003 — `facts.py` is shared and byte-frozen.
