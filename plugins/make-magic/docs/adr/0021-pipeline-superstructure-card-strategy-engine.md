# 0021 Pipeline superstructure: Card → Strategy → Engine — two phases, one ordering

| Field | Value |
|---|---|
| Status | Proposed. The superstructure of record; the code is being brought into line by implementation-plan **Phase 5** (tasks B2/B1/B3+B4/B6 + role-B). **Flips to Accepted when Phase 5.6 — the full-corpus 170-deck acceptance gate — passes** (i.e. 5.1–5.5 landed AND the spine's intended results hold with 0 regressions). Describes + prescribes ordering; the realization tasks are output-changing (re-baselined under ADR 0003), this ADR is not. |
| Review | pending owner review: authored in the `/planning` fold (2026-11-14) as the capstone the piece-ADRs compose under. The orchestrator reconciles and commits after owner sign-off. |
| Scope | The whole strategy pipeline. This ADR owns the **seams between** the piece-ADRs (0001–0020), which each own one primitive or rule. It adds no new primitive. |
| Code paths | Spans the package: `machine_vocab.py` (card-intrinsic store), `fid_adapter.py` + `edge_roles.py` + `relay.py` (derivation), `substantiveness.py` (all grains), `facts.py` (`_isolate_engines`, election), role-B synthesis (to build). The composition map below points each stage at its owning ADR. |

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
- **Election ∥ substantiveness (B6).** `_isolate_engines` (election, 0014) and the archetype
  `requires_met` check (substantiveness, 0002) run as *independent* reads of the deck;
  election never consults archetype-substantiveness. They are parallel, not ordered — which
  inverts the ontology: **Strategy is defined as the substantive sub-graph**, so
  substantiveness is *prior* to election, not beside it.

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
| Synthesis → Engine | B.3 | role-B (ADR TBD at build) |
| Knowledge placement (orthogonal axis) | — | 0001 |

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
  - **B6 — make Phase-B ordering real:** substantiveness → election → synthesis, replacing the
    current parallel election ∥ archetype-substantiveness. `producer_anchor` living inside
    `_isolate_engines` is an implementation convenience that must *read* as substantiveness
    qualifying the graph before centrality ranks it.
- **Role-A** builds the Phase-A universal process-once driver (inert→no-op, native-else-haiku,
  versioned store) to this spec — not a scoped "frontier." The design-doc "Incompleteness
  Frontier (3 buckets)" framing is retired in favor of universal process-once; its
  "engine-relevant otag" test becomes the inert/no-op predicate and its "uncovered" test
  becomes native-vs-haiku routing.
- **Role-B** builds Phase-B.3.
- This ADR **supersedes no piece-ADR**; it is the capstone they hang on. Later piece-ADRs
  (role-B) cite their stage here.
