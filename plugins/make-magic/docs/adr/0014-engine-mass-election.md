# 0014 Engine = node-weighted event-neighborhood MASS election (the "2C flip")

| Field | Value |
|---|---|
| Status | Accepted. In force on branch `feat/strategy-engine`. Output-changing re-baseline (ADR 0003). |
| Review | pending owner review: agent-authored from the GATE-0 GO-WITH-FIXES design + the re-validated full-170 delta; the orchestrator reconciles, shows the owner the delta, and commits. |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests -q` (453 pass). Parity gate: `uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle` — engine `987f2660…`, resolve `85752bb9…`, full-corpus `7e5190e8…` (as of the 2026-11-14 step-2 landing; see amendment). Invariant: the mass election is STRUCTURE-ONLY (the sort/keep-walk reads only `adens`; naming never reads the election). The 1 `named_strategy` change in the step-2 landing (Ghave) comes from WIRING `mana-produced`, not from the election mechanism — see [0020](0020-event-substantiveness-producer-anchor.md) + [0003](0003-byte-parity-held-files.md). |
| Code paths | [`facts.py`](../../pipeline/pipeline/transforms/strategy/facts.py) (`_isolate_engines` — the mass sort + the `event_admitted` (event-grain) and `earned_archetypes` (archetype-grain, B6) candidate-pool filters + the `elected` set; `compute_facts` computes the earned set from `requirement_for(arch).satisfied` post-ctx and passes it in), [`engine_report.py`](../../pipeline/pipeline/transforms/strategy/engine_report.py) (consumes `engines`/`forward_chains`, order-invariant), [`relay.py`](../../pipeline/pipeline/transforms/strategy/relay.py) (runs the SAME `_isolate_engines` over relay-augmented edges, with `earned_archetypes=None` — archetype grain OFF, structure-only). |

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

## Amendment — 2026-11-14 (step-2 landing): `lead` removed + event-substantiveness hook

Two coupled refinements landed with the corrected mana-wiring:

1. **No distinguished single `lead`.** The output is the ranked `elected` candidate set
   ONLY; the `lead_engine` field and the `lead = engines[0]` selector are removed.
   Consumers that need "the top candidate" read `elected[0]` locally. This is a pure
   field drop — **0 elected-SET changes** (the election walk is untouched). The decision
   body above still narrates the historical `lead`; read every "lead" there as "the
   top-mass unit = `elected[0]`".

2. **Event-substantiveness admission hooks between unit construction and the keep-walk.**
   Before the mass election, the candidate pool is filtered to
   `[e for e in engines if event_admitted(e['event'], cmd_attrs, assets)]` (ADR 0002
   level 3 / [0020](0020-event-substantiveness-producer-anchor.md)). The keep-walk's
   `top_mass` and `total_mass` are then taken over the POOL, so removing a refused
   top-mass unit lowers the 0.6 floor and lets genuine engines re-enter — this is how the
   8 mana-wiring evictions dissolve. The reported `engines` list stays COMPLETE
   (exclusion is candidacy-only); chains, hubs and the loop probe are untouched. The gate
   is GENERIC — keyed on the event's Requirement DATA, never an `if mana` / deck-name
   branch; an event with no data row is admitted (today's behavior).

This supersedes the "Options not taken" bullet that dropped a substantiveness/share floor
as "the wrong layer": that correctly rejected a *mass-share* floor inside the election.
The level-3 gate is a different mechanism — an ABSOLUTE command-zone PRODUCTION anchor,
read before the election from the deck alone, with no share/mass term at all (0020 §
Options not taken records the measured rejection of every mass/density/centrality
alternative).

## Amendment — 2026-11-14 (task 6.5): rank-don't-cut supersedes the 0.6 keep-ratio ([0022], #72)

The **0.6·top-mass relative keep-cut** of Decision step 4 is **removed**. The `elected` walk
now keeps **every** candidate-pool unit ranked by mass desc, with only the member-Jaccard<0.5
near-duplicate dedup — there is no relative-mass threshold. The units reaching the walk are
already substantive by construction (`node_admitted` + the level-3 `producer_anchor`
event-admission above), so a relative-mass cut among them discarded earned secondary engines on a
density-sensitive ratio (issue **#72** — e.g. Teysa's `token-created`, Elenda's `lifegain` falling
below `0.6·top` when the top event's mass inflated). **Centrality RANKS, it does not CUT.** Only
the single structural constant **0.5 Jaccard** remains; the **0.6 keep-ratio is retired** (read
every "0.6 floor" in the Decision/Reasons above as removed). The engine election stays
STRUCTURE-ONLY; the resulting corpus-wide shift (142 improvements, 0 genuine regressions, bounded
set sizes max 10) is re-baselined under [0022] / [0003]. This landed coupled with the
derived-adjacency flip (same re-baseline); the Test-line SHAs are superseded by the 0022 entry
(engine `a7edab20…`, resolve `25f50a23…`, full-corpus `5ce447ef…`).

## Amendment — 2026-11-15 (B6): archetype-grain admission completes the ordering invariant ([0021], [0002])

A **third candidacy filter** now sits between the event-grain `event_admitted` filter
(2026-11-14 amendment) and the rank-don't-cut keep-walk ([0022]). `_isolate_engines` takes a
new `earned_archetypes` argument — the set of the deck's archetypes that clear their [0002]
`Requirement.satisfied` bar — and, when it is supplied, drops any candidate whose payoff
archetypes are **ALL unearned** AND that is **not structurally anchored**. Owner-approved
exemptions KEEP a unit that (a) has ≥1 earned `payoff_archetype`, (b) is structurally anchored
(`commander_coupled`, OR a non-bipartite anchor mode — terminal-producer / exogenous-
environmental), or (c) makes NO archetype claim (empty `payoff_archetypes`). The earned verdict
is the EXISTING per-archetype [0002] data — **no new threshold/knob, no `arch == '<name>'`
branch**. `compute_facts` computes the earned set via `requirement_for(arch).satisfied(st, ctx,
arch)` — the SAME one-named evaluator the naming layer consults — and places `engine_isolation`
into `facts` post-ctx (the evaluator reads ctx). Scope: the P0 election only; `relay.py` passes
`earned_archetypes=None` (grain OFF, structure-only), so `engine_report` is unchanged.

This **reconciles the "Options not taken" bullet** that dropped a substantiveness/share floor
as "the wrong layer": that correctly rejected a *mass-share* floor inside the election. B6 (like
the level-3 event gate before it) is a different mechanism — an ABSOLUTE archetype-earned verdict
read from the deck alone (no mass/share term), identical to the gate that already governs the
archetype LABEL. The election was already not STRUCTURE-ONLY-in-isolation once the level-3 event
gate landed; B6 extends the same established substantiveness-before-centrality pattern to the
archetype grain, realizing [0021]'s ordering invariant (substantiveness → centrality →
synthesis) at all three grains.

**Output-changing re-baseline** ([0003]): engine `a7edab20…` → `afcffc8a…`, resolve `25f50a23…`
→ `3010be2a…`, full-corpus `5ce447ef…` → `4acf0f41…`. Full-170 delta: **28/170 decks change
their elected set, 28 units removed (0 added), 0 genuine regressions, 0 `named_strategy` changes,
1 determinism flip (Xanathar → `loop-or-flat`, elected empties).** Every removed unit is an
incidental `graveyard/reanimator` engine on a deck the engine ALREADY demotes (`gy_demoted`:
`requires_met` True, `satisfied` False) — so the gate makes NO judgment the shipped engine did
not already make (the "genuine engine gated via a `requires_met` false-negative" risk is not
realizable: the gate consults `satisfied`, the identical bar). STRUCTURE-ONLY-vs-naming is
preserved: 0 labels changed. Measurement: `research/b6-election-gate-170-delta.md`.

## Related records
- 0002 — substantiveness gates archetype LABELS (level 2) and, as of 2026-11-14, EVENT
  election candidacy (level 3, `producer_anchor`) whose pool this election consumes; as of
  2026-11-15 (B6) the level-2 archetype verdict (`requirement_for(arch).satisfied`) also gates
  election candidacy (`earned_archetypes`), so the SAME earned verdict drives both the label and
  the election.
- 0020 — the event-substantiveness admission gate hooked into this election.
- 0003 — the byte-parity held files this re-baseline updates (with the proof).
- 0004 — `node_admitted` is the only gate on engine-unit membership.
- 0012 — the Event ontology; `game-end` + `_RULE_TERMINAL_EVENTS` ground anchor mode 2.
- 0016 — the closure-mechanism win-walk (the Engine is distinct from the win-combo:
  the walk detects kill conditions; the Engine identifies the value structure).
- 0017 — edge derivation; the commander-anchored unit is constructed from whatever
  edge exists and then competes on mass here, never auto-promoted.
- [0022](0022-concept-anchored-derived-adjacency.md) — rank-don't-cut (keep every substantive
  unit, drop the 0.6 keep-ratio, #72) + the derived adjacency this election now reads; the
  2026-11-14 re-baseline that supersedes this record's SHAs.
