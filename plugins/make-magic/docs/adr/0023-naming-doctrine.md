# 0023 Naming doctrine: anchor to the real-world vocabulary, name a thing by what it is, one verb per operation — plus a bad-name checklist and when to ask the owner

| Field | Value |
|---|---|
| Status | Accepted. In force across the strategy package. |
| Review | Agent-authored 2026-11-17; **pending owner ratification**. |
| Test | Review-enforced today. Mechanical enforcement is the graph-CI layer — partly built (`validate_seed_map` dead-ref check, 0 violations) and partly planned (the WS3 catalog-integrity guards: dead-ref validator as a blocking gate, role-consistency linter, one-verb-per-operation collision check). **Honest gap:** same as ADR 0001 clause 4 — until those land, this is review-enforced. |
| Code paths | Doctrine over the whole package. Exemplars of a correct application: [`derive_card_events.py`](../../pipeline/pipeline/transforms/strategy/derive_card_events.py), [`distill_strategies.py`](../../pipeline/pipeline/transforms/strategy/distill_strategies.py), [`determine_strategy_substantiveness.py`](../../pipeline/pipeline/transforms/strategy/determine_strategy_substantiveness.py), [`elect_engine_units` / `evaluate_event_candidates.py`](../../pipeline/pipeline/transforms/strategy/evaluate_event_candidates.py), [`synthesize_engine.py`](../../pipeline/pipeline/transforms/strategy/synthesize_engine.py). |

## Context
A classification engine accretes bad names the way it accretes bad logic, and the damage
is the same: the codebase lies to its readers. Over one working session the catalog and
code carried migration-era names (`_v4_base_buckets`, `newbuckets`), a marketing metaphor
(`self-healing` for what is candidate evaluation), a module named for a data structure
instead of its job (`strategy_graph` = the FACTS layer), seven dead seed references
(`sac`, `clones`→`clone`) that silently named nothing, and a role inversion
(`synergy-proliferate`, a payoff, seeded as a producer). Each was invisible until read
closely. ADR 0001 (the verb/noun test + one-named-rule) and ADR 0008 (name by concept +
lifecycle, not authoring mechanism) already held pieces of the cure, scattered. This record
gathers them and states the whole discipline, so the next name — authored by a person or an
agent — is reasoned, checked, and escalated when it cannot be settled alone.

## Decision

### Part A — Principles to reason from

Ordered most-decisive first. Each is a **hard rule** (a bad name; fix it) unless marked a
**smell** (stop and reconsider; may be fine in context).

1. **Anchor to the domain's real-world vocabulary.** Prefer the authoritative term over a
   coinage. The engine spans two domains with a seam; name by the layer the thing lives in:
   - **Game-semantic** (cards, events, strategies) → the **Comprehensive Rules + standard
     MTG mechanic names**: `monarch`, `recursion`, `dies` (CR 700.4), `mill`, ETB. The CR
     audit named every event by its rule.
   - **Graph / algorithm** (the math over those concepts) → **standard graph-theory / CS**:
     `interior_node`, `edge`, `multigraph`, `mass`, `centrality`, `election`. Not a bespoke
     word when the field has one.
   This is the positive form of principle 7.
2. **Name a thing by what it IS now** — its concept, role, or output — not by its **version**
   (`_v4`), a **past migration** (`newbuckets`, `facts.py`), or — *smell* — its **authoring
   mechanism** (`MachineAttribute`→`CardAttribute`, ADR 0008). The mechanism can be a
   legitimate *field* (`provenance.source ∈ {curated, haiku, spellbook}`); it must not be the
   thing's *identity*.
3. **One canonical verb per operation; one verb, one meaning.** The pipeline vocabulary:
   **derive** (card → attributes / events), **distill** (attributes → strategies),
   **determine** (substantiveness), **elect** (centrality / ranking), **synthesize** (engine),
   **resolve** (the whole-deck orchestrator), **evaluate** (event candidates). No synonym for
   one operation (`route`→`distill`; `_isolate_engines`→`elect_engine_units`); no one verb for
   two operations (`resolve` was triple-claimed — freed by moving stage-3 to `distill`).
4. **Verb/noun test** (ADR 0001): a **noun** → data / query / row; a **verb** → a rule /
   operation. A noun-name that hides logic, or a verb-name that hides data, is a defect.
5. **Name a code unit for its job or output, and give it one coherent job.**
   `derive_card_events` derives card events; `elect_engine_units` elects units. Cautionary:
   `strategy_graph.py` names a *data structure* while its job is *computing the facts* (its
   own docstring calls it "the FACTS layer") — the honest name is `compute_facts`
   (deferred, GitHub #80). The three stages it holds are defensible *only* as sub-steps of
   that one job; if a unit's name cannot honestly cover everything inside it, the unit, not
   the name, is wrong.
6. **The name must match the thing's measured role.** Producer vs payoff
   (`synergy-proliferate` is a payoff, not a producer — verify against the cards, not the
   name's vibe); a thing that "looks like a duplicate" is usually **mis-modeled**
   (`recursion-from-graveyard`), not redundant.
7. **No metaphor that does not name a real operation.** `self-healing` →
   `evaluate_event_candidates` / seed discovery. (Negative of principle 1.)
8. **When the handed vocabulary is too coarse, name the atomic concept yourself.** Author a
   CardAttribute backed by a Scryfall query (ADR 0009 / 0017), named with the real mechanic:
   `monarch-maker` ← `oracle:"become the monarch"`, not the coarse `monarch-matters` (which
   lumps maker + payoff). The `rad-counter` synth attribute is the precedent.

**Smells** (reconsider, context-dependent): authoring-mechanism naming (principle 2); the
vague-noun family — `…Context`, `…Manager`, `helper`, `util` (`FactsContext` was a mild
case: a 32-field grab-bag whose name admits it carries no single concept).

### Part B — The bad-name checklist (run before committing a name)

1. Does the name describe the **current** thing, or its history / mechanism / a metaphor?
2. Is there a **real domain term** (CR or graph-theory) I should use instead of inventing one?
3. Is this **verb already used** for a different operation? (grep it.)
4. Does a **noun-name hide logic**, or a **verb-name hide data**?
5. Does the name match the thing's **measured** behavior — checked against the data (the
   cards / the corpus), not the name's suggestion?
6. Is there **already a name** for this concept elsewhere in the package?
7. Is the unit **one coherent job**, or a grab-bag the name cannot honestly cover?

### Part C — When the right name needs the owner (escalate, do not guess)

1. The concept **conflates two things** and splitting them changes behavior
   (`monarch-or-extra-turn`).
2. The vocabulary is too coarse **and** the precise concept boundary is a **judgment call**
   (what exactly `monarch-maker`'s query should match).
3. The right name **collides** with an established one and resolving it is a **trade-off**
   (`resolve` → the orchestrator keeps it, stage-3 becomes `distill`).
4. A candidate **passes a numeric check but is semantically wrong** — precision is necessary
   but not sufficient (`group-slug` scored 0.973 against life-loss yet is off-concept). A
   name/concept the math accepts but meaning rejects is the owner's call.
5. A rename **moves a frozen baseline** (ADR 0003) — always owner-gated, proof-carrying.
6. You **cannot tell a conflation from an intentional grouping** — present both readings,
   do not assert (the `game-end` / `monarch` mis-assertions this doctrine was written after).
7. **No real-world term exists** for a genuinely novel concept — you are *coining*. Coinage
   is ratified, never silent.

## Reasons
A name anchored to the Comprehensive Rules or to graph theory is checkable against an
outside authority, exactly as ADR 0001 places knowledge by verifiability. One verb per
operation makes the pipeline legible end-to-end and makes a collision mechanically
detectable. Matching the measured role is what separates a catalog that models the game
from one that pattern-matches names — the role inversions and dead refs were invisible to a
reader and obvious to the corpus. The escalation parameters exist because the session that
prompted this record produced confident wrong assertions (`game-end has no producer`;
`monarch has no producer`) that a "present both readings" reflex would have caught.

## Options not taken
- **Bolt the doctrine onto ADR 0001's fourth clause.** Rejected: the naming discipline is
  now substantial (principles + checklist + escalation) and would be buried inside a
  knowledge-*placement* record. 0001 and 0008 keep their naming clauses and point here.
- **Make it purely aspirational (principles only).** Rejected: the checklist and the
  escalation parameters are where the principles become operable by a person or an agent
  mid-task — the point is to change the next decision, not to be admired.

## Consequences
- The verb vocabulary (principle 3) is the one realized across the package this session
  (ADR 0021's stages; the `derive`/`distill`/`determine`/`elect`/`synthesize` renames).
- Principle 8 is the standing answer to otag-coarseness: author a CardAttribute, do not
  seed a coarse tag. It feeds the WS3 discovery escalation (a coarse best-candidate →
  "author a query," not a lowered standard).
- Mechanical enforcement is the WS3 catalog-integrity guards; until they are blocking CI,
  Part B is a human checklist and Part C a human judgment — the same honest gap as 0001.

## Related records
- 0001 — the verb/noun test + one-named-rule; this record gathers and extends its naming clauses.
- 0008 — name by concept + lifecycle, not authoring mechanism; no aliases (gathered here).
- 0009 / 0017 — authoring a CardAttribute query (principle 8's mechanism).
- 0021 — the pipeline-stage vocabulary (principle 3's source).
- 0022 — `self-healing` → seed discovery / `evaluate_event_candidates` (principle 7 applied).
- 0003 — frozen baselines a rename may move (Part C-5).
