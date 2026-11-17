# 0001 Knowledge placement: four homes by what the knowledge quantifies over, and one named implementation per deck rule

| Field | Value |
|---|---|
| Status | Accepted. In force across the strategy package. |
| Review | ratified by the owner on 2026-11-11. (Agent-authored; owner-ratified.) |
| Test | Enforced today by review + ADR 0002's substantiveness tests (the code/data split has no archetype-name branches) + the query-pasteability guard in `query.py`. **Honest gap:** the fourth-clause duplication/governed-path check is not yet built (planned as the combo-design conformance check); until then, clause 4 is review-enforced. |
| Code paths | Exemplars (the doctrine governs the whole package; precise governance lives in the specific records): [`distill_strategies.py`](../../pipeline/pipeline/transforms/strategy/distill_strategies.py) (queries as identity), [`determine_strategy_substantiveness.py`](../../pipeline/pipeline/transforms/strategy/determine_strategy_substantiveness.py) (the code/data split), [`query.py`](../../pipeline/pipeline/transforms/strategy/query.py) (the query evaluator). |

## Context
Three habits corrupt a classification engine over time: (1) mechanic knowledge
hard-coded as Python (regex/if-chains over card text) — unverifiable, rots as new sets
ship; (2) the same deck-level rule copy-pasted — the copies drift silently; (3)
over-abstraction — deck logic dressed up as "configurable data" that is really a program
in disguise.

## Decision
A piece of knowledge is placed by **what it quantifies over**:
- over **cards** ("which cards do X?") → a **Scryfall query** (pasteable on scryfall.com,
  authorable by haiku);
- a fixed set of **named mechanics** → a **data row** (a noun, no hidden logic);
- over the **deck** (counts/thresholds/ranking/graph walks) → **generic code**;
- an outside **authority**'s facts (otag taxonomy, creature-type catalog, ban list,
  Spellbook feature-IDs) → **reference data**, synced not hand-maintained.

Plus the **fourth clause**: every deck-level rule has **exactly one named
implementation**; an inline re-type of an existing rule is a defect of the same severity
as a mechanic-name branch. Sub-rules: the **verb/noun** test (noun → query/row; verb over
the deck → code) and **rule-of-two over a data concept's values**, not its call sites.
`determine_strategy_substantiveness.py` (ADR 0002) is the reference implementation of the whole doctrine.

## Reasons
Verifiability follows the placement — a query is checkable on scryfall.com, a data row is
diffable, generic code has one source of truth, reference data is owned upstream. The
fourth clause is the one the knowledge graph / a conformance check can enforce
mechanically (a predicate with two defining sites is detectable).

## Options not taken
- **The "grammar of operations" (everything-as-configurable-data).** Rejected: a lambda
  or branch stuffed into a JSON row is code masquerading as data — it fails the verb/noun
  test and is neither verifiable nor safely haiku-editable.

## Consequences
- The consolidation moves mechanic logic to queries/data (enchantment surfacing, lands
  signal, tribes) and collapses duplicated deck rules (the presence gate ×4 → one
  `determine_node_eligible`, ADR 0004), per the fourth clause.
- Honest gap (restated): the clause-4 check is review-enforced until the conformance check
  ships. Planning/design artifacts live in the owner's transient plans tree and are **not**
  linked from this record.

## Related records
- 0002 — the reference implementation (substantiveness).
- 0003 — the held files that exemplify "generic code / data, byte-frozen."
- 0005 / 0006 / 0007 (planned) — instances: tribe catalog (authority), config
  universal-or-gone, combo win as feature-ID.
