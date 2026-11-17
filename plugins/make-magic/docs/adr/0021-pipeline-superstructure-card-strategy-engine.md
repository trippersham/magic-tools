# 0021 Pipeline superstructure: Card → Strategy → Engine — two phases, one ordering

| Field | Value |
|---|---|
| Status | **Accepted** (2026-11-15). Phase 5.6 full-corpus role-B validation passed: **187 decks, 7/7 B-test anchors RIGHT, 0 wrong, 0 empty, 0 genuine F4 violations** (the 20 initial F4 flags were all R1 false positives — winning combos aligned to their own archetype, e.g. Korvold aristocrats-combo; the heuristic was tightened). Confidence: 82 HIGH / 84 MED / 21 LOW. The derived-adjacency flip (`0d89930`) + rank-don't-cut (#72, `3ab4034`) + combo-edge B1 machinery (`442b826`) + seed-discovery (event-candidate evaluation) loop (`efd1b1a`) all landed and re-baselined. **B6 realized (2026-11-15):** archetype-grain substantiveness is now UPSTREAM of the election — `elect_engine_units` gates a candidate whose SOLE basis is an UNEARNED archetype claim, via the same `requirement_for(arch).satisfied` verdict the naming layer uses (0001/0002). The ordering invariant (substantiveness → centrality → synthesis) is now fully implemented at all three grains (attribute 0004 / event 0020 / archetype 0002), not partial. B6 is OUTPUT-CHANGING on the frozen P0 path (re-baselined P0 SHAs: engine `afcffc8a`, resolve `3010be2a`, full-corpus `4acf0f41`; prior `a7edab20` / `25f50a23` / `5ce447ef`). Full-170 delta: **28/170 decks change their elected set, 28 units removed (0 added), 0 genuine regressions, 0 `named_strategy` changes, 1 determinism flip (Xanathar → `loop-or-flat`, elected empties)**; every removed unit is an incidental `graveyard/reanimator` engine on a deck the engine ALREADY demotes (`gy_demoted`), so the gate introduces no new judgment. Measurement: `research/b6-election-gate-170-delta.md`. Suite: 547 passed, 4 skipped. **Resolved items:** **(B.3)** role-B production wiring: WIRED (`8aad1c2`, task 7.4). `engine_synthesis.resolve_engine` is the Tier-2 production reader — a cache-aware reader that wraps `synthesize_engine` (cache-hit returns the stored Engine; miss calls `synthesize_engine`); `engine_cache.py` is the materialized store; `ClaudeCliBackend` (`claude -p`, ambient auth, no API key) is the synthesis backend. Cached per-deck Engine via the #65 `ship-materialized` pattern. See [output contract](../strategy-engine-output-contract.md). **(B1)** store population landed in production (`369f209`, combo-edges ON). **(B6)** archetype-grain substantiveness gates the election (`a398705`). **All open items from Accepted status are now resolved.** Remaining deferred work is tracked in GitHub issues: #74 (vocab/energy), #75 (direction), #76 (landfall-density/absolute-floor re-scoped), #77 (harness). |
| Review | Accepted 2026-11-15 after full-corpus validation. |
| Scope | The whole strategy pipeline. This ADR owns the **seams between** the piece-ADRs (0001–0020), which each own one primitive or rule. It adds no new primitive. |
| Code paths | Spans the package: `machine_vocab.py` (card-intrinsic store), `fid_adapter.py` + `edge_roles.py` + `relay.py` (derivation), `substantiveness.py` (all grains), `strategy_graph.py` (`elect_engine_units`, election), role-B synthesis (to build). The composition map below points each stage at its owning ADR. |

## Context

The engine's knowledge is captured in 15 piece-ADRs (0001–0020). Each defines one
primitive or rule well — CardAttribute storage (0008), event ontology (0012), the
substantiveness evaluator (0002) and its grains (0004 attribute, 0020 event), mass-election
(0014), win-walk (0016), native-then-haiku derivation (0017), and so on. **No ADR records
how they compose** — the order in which the stages run, which stage is cached vs computed,
and where the Card-intrinsic / deck-specific seam falls.

Unwritten superstructure is where bolt-ons breed, and it already has:

- **Split derivation (B1).** Within-card decomposition runs as *two parallel,
  non-communicating* paths: `fid_adapter` maps fids → Events for the **win-walk** (0016),
  while within-card relay **edges** come from `CURATED_EDGE_ROLES` + the machine store
  (0017). A card's combo can be *seen* by win-detection yet author **zero relay edges** for
  the mass-election graph — the Urza thin-capture (the fid adapter recognizes Urza's combo
  for the walk but contributes nothing to the election graph).
- **Election ∥ substantiveness (B6).** *(Historical — resolved 2026-11-15.)* Originally
  `elect_engine_units` (election, 0014) and the archetype substantiveness check (0002) ran as
  *independent* reads of the deck; election never consulted archetype-substantiveness. They
  were parallel, not ordered — which inverted the ontology: **Strategy is defined as the
  substantive sub-graph**, so substantiveness is *prior* to election, not beside it. B6 fixes
  this: `elect_engine_units` now consults `requirement_for(arch).satisfied` (the one-named 0002
  evaluator) to gate a candidate whose SOLE basis is an unearned archetype claim, with the
  owner-approved structural exemptions (commander-coupled / non-bipartite anchor / no
  archetype claim). Substantiveness is now upstream of centrality at the archetype grain too.

0001 (knowledge-placement / four-homes) is the closest existing doctrine, but it is an
**orthogonal axis**: it says *where* knowledge lives by *what it quantifies over*, never
*in what order the stages compose*. This ADR supplies the missing axis.

## Decision

The pipeline is **two phases**, and within them a **single ordering**.

### Phase A — Card-intrinsic (process-once, deck-agnostic, cached)

Build-time, keyed by **`oracle_id`** (never name — different arts / flavor-names / printings
collapse to one `oracle_id`; a name may map to several oracle_ids, e.g. a card and its token,
so the universe is taken from the #69-fixed tag-preferring resolve). Each distinct card is
processed **exactly once per logic-version**:

1. **Decompose** (CardAttributes from otags + structured features / fids) — **native first,
   haiku only for the residue** (0017). Inert cards (no engine-relevant otag: basics, vanilla
   bodies, one-shot removal) are a **deterministic no-op**, recorded, not skipped — so there
   is no frontier-detector to miss on. Haiku appears here, **once-ever**, as the gated fallback.
2. **Derive** (features → the card's Events + within-card EventEffects) — **deterministic**.

Two version layers (the staleness key): **extraction-version** (text → features; bumps rarely,
the only thing that re-runs haiku, and then only for candidate cards whose text could carry a
newly-added concept) and **derivation-version** (features → events/edges; a pure deterministic
re-walk on catalog/adapter change — **no haiku**). Adjacency changes (`produced_by`×`triggers`,
e.g. a wiring fix) are Phase-B / engine-time and bump **neither**. Result cached in the
`ProvisionalAttributeStore` (0008).

### Phase B — Deck-specific (per-deck, in-memory, from the Phase-A cache)

In strict order:

1. **Substantiveness → the Strategy sub-graph.** All grains — attribute (`node_admitted`,
   0004), event (`producer_anchor`, 0020), archetype (`requires_met` Requirements, 0002) —
   qualify what is *earned* in this deck. **Strategy is the substantive sub-graph.** This runs
   **first**.
2. **Centrality → ranking within the strategy graph.** Mass-election (0014) ranks the
   substantive strategies → the ranked `elected` candidate set. Election is
   *ranking-over-the-substantive-graph*, **not** a prior "engine layer," and emits a **set,
   not a single lead**.
3. **Synthesis → the Engine.** Role-B resolves the ranked strategy graph + combos + commander
   + archetype into the one core Engine, via the never-∅ waterfall. **The Engine exists only
   at synthesis.** Haiku appears here, **per-deck**.

### Invariants

- **Haiku at exactly two points:** Phase-A extraction (once-ever) and Phase-B synthesis
  (per-deck). Everything between is deterministic.
- **The Card-intrinsic / deck-specific seam is Phase A → B.** CardAttributes, Events,
  EventEffects are card-intrinsic (computed once, reused by every deck). Relevance,
  substantiveness, and centrality are deck-specific (never cached per-card).
- **Ordering:** substantiveness → centrality → synthesis. Never centrality before
  substantiveness; never an Engine before the Strategy graph it is synthesized from.
- **Byte-parity (0003):** Phase B's frozen deterministic path (`compute_facts`/`resolve`) is
  SHA-pinned; the Phase-A provisional store feeds only the `engine_report`/`load_store`
  surface. Populating the store does **not** move the P0 SHAs.

## Composition map (each stage → its owning piece-ADR)

| Stage | Phase | Owning ADR(s) |
|---|---|---|
| Card-intrinsic store (`oracle_id`-keyed) | A | 0008 |
| Decompose: native-first, haiku residue | A | 0017 (+ 0007 fid adapter) |
| Derive: features → Events + EventEffects | A | 0012 (events), 0011 (EventEffect guards), 0016 (win-walk view) |
| Substantiveness — evaluator | B.1 | 0002 |
| Substantiveness — attribute grain | B.1 | 0004 |
| Substantiveness — event grain | B.1 | 0020 |
| Centrality / election (ranking) | B.2 | 0014 |
| Synthesis → Engine | B.3 | role-B wired (`8aad1c2`): `engine_synthesis.py` (reader + backends), `engine_cache.py` (materialized store), `roleb_prompt.md` (vendored rules). See [output contract](../strategy-engine-output-contract.md). |
| Knowledge placement (orthogonal axis) | — | 0001 |

## Underlying graph model

The Card → Strategy → Engine superstructure operates over a single bipartite
graph (attribute-nodes × event-nodes) with two projections: the **event
multigraph** (Events as nodes, EventEffects as edges — the authoring layer) and
the **election graph** (attributes as nodes, events as edge-labels — where
`elect_engine_units` runs). Strategy is a quotient of the election graph's
attribute-nodes by archetype grouping. The formal model, including the projection
table placing every primitive, lives in [ADR 0012](0012-event-ontology.md) §"The
bipartite graph and its projections." This ADR's Phase-B ordering
(substantiveness → centrality → synthesis) operates over Projection 2 and its
strategy quotient.

## Consequences

- **Open coherence tasks** (bring the code to this spine; tracked for role-A / the fold):
  - **B1 — unify Phase-A derivation:** one card-decomposition feeds *both* its win-events and
    its within-card relay edges; the fid-adapter view and the EventEffect view become two
    reads of one decomposition, not two pipelines. Closes the Urza thin-capture.
    - **Machinery built (2026-11-14, task 5.2):** The combo-intrinsic authoring
      (`combo_edges.py`, Phase A, cached by `variant_id`, provenance
      `source='spellbook'`) + present-combo assembly (`engine_report._assemble_present_combo_edges`,
      Phase B) MACHINERY is built and unit-tested but **INERT until the 5.3 driver
      populates the store** — no production caller invokes `author_combo_edges`
      in the committed state, so the store is empty and assembly short-circuits.
      The blast-radius (Urza closes `draw-a-card` recursion not `mana-produced`,
      13/17 no-op, Krrik/Ozai contained, 0 win-flag changes) is scratch-harness
      measured; end-to-end verification lands at task 5.3, with the 5.6
      full-corpus gate as the acceptance criterion.  **Urza finding (scratch-harness):**
      `mana-produced` closure for Urza is an emergent **deck-level cross-combo** fact that the
      combo-intrinsic layer structurally cannot author (infinite mana is piece-interaction,
      not piece-tagged; the reciprocal lives in a different combo).  The election surfaces
      Urza's genuine `draw-a-card` recursion instead.  The `mana-produced` resolution is
      deferred to role-B synthesis (task 5.5), with a generic `mana<->draw` value-loop edge
      as the principled fallback (NOT a deck-level hook) if role-B needs it.
  - **B6 — make Phase-B ordering real (REALIZED 2026-11-15):** substantiveness → election →
    synthesis, replacing the former parallel election ∥ archetype-substantiveness.
    `elect_engine_units` now takes `earned_archetypes` (the deck's archetypes clearing their 0002
    `Requirement.satisfied` bar) and, after the event grain (`producer_anchor`, 0020), gates a
    candidate whose payoff archetypes are ALL unearned AND that is not structurally anchored.
    The earned verdict is the EXISTING per-archetype data (0002) — no new threshold/knob, no
    deck-specific branch. Owner-approved exemptions keep a unit that (a) has ≥1 earned payoff
    archetype, (b) is structurally anchored (commander-coupled OR a non-bipartite anchor mode),
    or (c) makes no archetype claim. Scope: the P0 election in `compute_facts` only; the
    structure-only relay-augmentation path (`relay.py`, feeding `engine_report`) passes
    `earned_archetypes=None` and is unchanged. Because the gate consults the SAME `satisfied`
    verdict that already governs naming demotion (`gy_demoted`), it introduces no judgment the
    shipped engine did not already make — the full-170 re-baseline removed only incidental
    graveyard units from already-`gy_demoted` decks (0 genuine regressions). See the Status
    line and `research/b6-election-gate-170-delta.md`.
- **Role-A** builds the Phase-A universal process-once driver (inert→no-op, native-else-haiku,
  versioned store) to this spec — not a scoped "frontier." The design-doc "Incompleteness
  Frontier (3 buckets)" framing is retired in favor of universal process-once; its
  "engine-relevant otag" test becomes the inert/no-op predicate and its "uncovered" test
  becomes native-vs-haiku routing.
- **Role-B** builds Phase-B.3.
- This ADR **supersedes no piece-ADR**; it is the capstone they hang on. Later piece-ADRs
  (role-B) cite their stage here.
