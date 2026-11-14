# 0014 Engine = node-weighted event-neighborhood MASS election (the "2C flip")

| Field | Value |
|---|---|
| Status | Accepted. In force on branch `feat/strategy-engine`. Output-changing re-baseline (ADR 0003). |
| Review | pending owner review: agent-authored from the GATE-0 GO-WITH-FIXES design + the re-validated full-170 delta; the orchestrator reconciles, shows the owner the delta, and commits. |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests -q` (453 pass). Parity gate: `uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle` — engine `59fe5d33…`, resolve `82a71840…`, full-corpus `d01d8701…`. Invariant: **0 `named_strategy` labels changed** across the 170-deck gauntlet (STRUCTURE-ONLY); **0 previously-correct elections regressed** (mass-monotone; no lead→None, no lower-mass displacement). |
| Code paths | [`facts.py`](../../pipeline/pipeline/transforms/strategy/facts.py) (`_isolate_engines` — the mass sort + `elected` set + `lead`), [`engine_report.py`](../../pipeline/pipeline/transforms/strategy/engine_report.py) (consumes `engines`/`forward_chains`, order-invariant), [`relay.py`](../../pipeline/pipeline/transforms/strategy/relay.py) (runs the SAME `_isolate_engines` over relay-augmented edges). |

## Context

`_isolate_engines` builds, per directed event, one atomic bipartite unit
(producers → event → consumers) and must pick the deck's **lead engine** (and,
under the Design-3 ontology, the ranked **Engine set** of 0..n Strategies the deck
hinges on). The committed implementation ordered units by a recursive-first
heuristic — primary sort key `kind != 'recursive-engine'`, then payoff-archetype
count, then weight — and set the lead to the first `recursive-engine` unit (a
relay hub that both consumes an upstream event and mints this one). That heuristic
encoded a structural prior ("a relay hub is the engine") that mis-fired across the
corpus: it elected the land-recursion cone on reanimator decks (Karador → `lands`),
graveyard noise on landfall decks (Aesi → `card-enters-graveyard`), and left the
central value engine unelected (`lead_engine=None`) on 114/170 gauntlet decks
(Urza, every spellslinger/token deck with no relay hub).

The Design-3 refinement (§4.2) re-cast `Engine` as a **node-weighted
event-neighborhood mass election**, which beat all 12 fancier centralities in the
E3 probe: path/spectral measures re-inject exactly the biases being fixed
(betweenness/articulation *are* the recursive-first rule numerically; HITS/eigen
drift to dense blocks; PageRank to sinks). *"Isolation needs graph structure;
election needs only mass on the isolated units."* GATE-0 validated the election
GO-WITH-FIXES; the #68/#69 resolution fixes then landed (shifting the baseline),
so this change **re-validated** the election against the committed baseline
(`9e66367`) before re-baselining.

## Decision

Node-weighted mass is the **authoritative** engine-lead signal. In
`_isolate_engines`, over the per-event units:

1. **Mass.** `m(U) = Σ adens(a)` over `producers(U) ∪ consumers(U)` — already the
   unit's `weight`.
2. **Order.** Sort units by `m` descending. Recursive status breaks an **exact
   mass tie only** (`-weight, kind != 'recursive-engine', event`); it is never a
   primary key. `event` is the final determinism tiebreak.
3. **Lead.** The top-mass unit (`engines[0]`), not the first `recursive-engine`.
   `None` only when there are no units.
4. **Engine SET (0..n), reported as `elected`.** Walk the mass-descending units;
   keep `U` when `m(U) ≥ 0.6·m(top)` **and** `U` is not a near-duplicate of a kept
   unit (member-Jaccard `< 0.5`). The list is mass-descending, so the first unit
   below the 0.6 floor ends the walk. Two structural constants (**0.6 keep-ratio,
   0.5 Jaccard**), **zero per-strategy knobs**.
5. **Reported attributes, never election inputs.** `recursive` / `hub` /
   `commander_coupled` / `share` / `anchor` / `thin` are recorded on each elected
   unit for inspection; none feeds the sort or the keep-set (recursive breaks the
   exact-mass tie only).
6. **`determinism`** gains `'mass-elected'` (a non-empty election); the
   commander-loop (`'loop-commander-anchored'`) and flat (`'loop-or-flat-no-forward-hub'`)
   fallbacks are unchanged.

**Three anchor modes, all generic** (§4.2), classified by which side of a unit is
in-deck:
1. **bipartite** — in-deck producers AND in-deck consumers (the classic loop).
2. **terminal-producer** — in-deck producers toward a terminal event (one whose
   rule-edges reach `game-end`). The terminal set is the game-rules-derived
   `win_walk._RULE_TERMINAL_EVENTS` (`game-end`, `life-loss-drain`,
   `library-depleted`), **never a hardcoded payoff literal** (no-frozen-lists).
3. **exogenous-environmental** — in-deck consumers from an exogenous always-on
   source; mass-starved (the exogenous producer contributes 0 `adens`), an anti-∅
   floor that can never outweigh a real in-deck engine.

**STRUCTURE-ONLY is preserved.** The election is reported under
`facts['engines']['engine_isolation']` and never consulted by the naming layer —
verified: **0** `named_strategy` labels changed across the gauntlet. `Engine` is
distinct from `archetype_identity` (the kept external-compatibility label).

**Generic only.** A unit leads because it is generically the central
game-end-reaching strategy, not because of its identity — no `if Urza`, no
commander/deck-name branch. The Urza combo is elected (`artifact-enters`, m=47)
because it is the heaviest convergence, not because it is Urza.

## Scope boundary (this step)

At this step the only edges `_isolate_engines` receives are the committed in-deck
→ in-deck `event_edges`, so **every unit is bipartite (anchor mode 1)** and the
anchor distribution is all-bipartite. The terminal-producer (mode 2) and
exogenous-environmental (mode 3) anchors are implemented and classified but only
**populate** once producer-only / consumer-only edges exist — the mana-wiring and
role-A hydration steps that follow this keystone in the execution sequence. The
classification is generic and structure-derived now; the inputs grow later. The 6
otherwise-empty elections (Baral, Grand Arbiter, Aminatou-Veil, Sen, Talrand ×2)
stay `lead_engine=None` at this step (all were `None` under the committed engine
too — neutral, not a regression) and gain their thin exogenous floor when mode-3
inputs land.

## Reasons

- **Mass fixes the recursive-first bias with no new machinery.** The weight was
  already computed; the flip is a sort key + lead selector. The E3 fixes
  (Karador→graveyard, Aesi→lands, Kykar/Prosper→tokens, Urza→artifacts) and the
  108 none→lead recoveries fall out of mass alone.
- **0.6 / 0.5 are the minted design constants** and suffice for every measured
  outcome including the Urza Engine/combo split — no per-strategy centrality
  weight, closed-cycle bonus, or substantiveness-threshold change was needed
  (owner-approval guardrail for strategy-specific tuning: none was required).
- **Reported-not-ranked** keeps recursive/hub/coupling inspectable without letting
  a structural prior drive the election again.

## Options not taken

- **Recursive-first ordering (the committed heuristic).** Rejected: it *is* the
  bias being fixed — mis-elects relay-hub cones over heavier value engines and
  leaves 114/170 leads empty.
- **Fancier centralities (betweenness, articulation, HITS, eigen, PageRank, …).**
  Rejected: they re-inject the same biases numerically (E3 probe: mass beat all 12).
- **Forcing exactly one engine.** Rejected: wrong on ~72% of decks — the Engine is
  a set (∅ on pure-lock, ≥2 on the majority once edges hydrate).
- **A substantiveness/share floor to suppress promiscuous events (Fable probe).**
  Dropped by the owner (2026-11-13) as the wrong layer: `_isolate_engines` gates
  units by `node_admitted` (support count) only, never `requires_met`; mana
  over-forming is a Layer-C (synthesis) phenomenon, not an election knob.

## Consequences

- **Output-changing re-baseline** (ADR 0003): engine `a6dfa607…` → `59fe5d33…`,
  resolve `d409069e…` → `82a71840…`, full-corpus `0901bc62…` → `d01d8701…`. The
  resolve/full-corpus shift is driven entirely by the embedded `engine_isolation`
  sub-dict; **0 labels changed**.
- **`engine_report._characterize_engines` is order-invariant** to the `engines`
  list (it re-sorts named engines by weight and re-sorts hubs by `relay_hubs`
  order), so only the serialized `engine_isolation` SHA shifts, not the named-engine
  output shape. `relay.py` runs the SAME `_isolate_engines`, so the relay-augmented
  path mass-elects too (`test_relay::test_determinism_mass_elected`).
- **Keystone for the execution sequence:** mana-wiring and role-A hydration build
  on this election; the whole-gauntlet owner gut-check is the acceptance gate after
  all outcome-moving pieces land.

## Related records
- 0002 — substantiveness gates archetype LABELS (a different output); it does not
  gate engine units, so promiscuity is not an election knob.
- 0003 — the byte-parity held files this re-baseline updates (with the proof).
- 0004 — `node_admitted` is the only gate on engine-unit membership.
- 0012 — the Event ontology; `game-end` + `_RULE_TERMINAL_EVENTS` ground anchor mode 2.
- 0016 — the closure-mechanism win-walk (the Engine is distinct from the win-combo:
  the walk detects kill conditions; the Engine identifies the value structure).
- 0017 — edge derivation; the commander-anchored unit is constructed from whatever
  edge exists and then competes on mass here, never auto-promoted.
