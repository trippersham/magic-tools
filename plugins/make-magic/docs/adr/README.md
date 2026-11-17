# Architecture Decision Records — make-magic strategy engine

These records capture durable design decisions of the strategy engine *as built*,
co-located with the code they govern. **Read the record for a file before you change
that file; if your change alters the decision, update the record in the same change**
(see each record's *Code paths*). Decisions live here once — companions and rules
*link*, never copy.

Path-triggered surfacing is wired via repo-root `.claude/rules/adr-*.md` (native
Claude Code `paths:` injection): touching a governed file loads the matching rule,
which points back here.

| # | Title | Status | Review | Code paths |
|---|---|---|---|---|
| [0001](0001-knowledge-placement-doctrine.md) | Knowledge placement: four homes by what knowledge quantifies over + one named implementation per deck rule | Accepted | ratified 2026-11-11 | doctrine (exemplars: vocab/substantiveness/query) |
| [0002](0002-substantiveness-generic-evaluator.md) | Substantiveness: one generic determiner over a closed kind-set + per-archetype data, no archetype-name branches | Accepted | ratified 2026-11-11 | determine_strategy_substantiveness.py, strategy_graph.py |
| [0003](0003-byte-parity-held-files.md) | Byte-parity held files: the curated layer is byte-frozen, changed only by a proof-carrying re-baseline | Accepted | ratified 2026-11-11 | 6 held curated files |
| [0004](0004-node-predicates.md) | Node predicates: distinct `determine_node_eligible` (presence gate) and `determine_node_admitted` (graph admission), one impl each | Accepted | pending owner review | strategy_graph.py, engine_report.py |
| [0005](0005-tribe-resolution-creature-type-catalog.md) | Tribe resolution: Scryfall creature-type catalog allowlist, not a hand blocklist | Accepted | pending owner review | tribes.py, creature_types_catalog.json |
| [0006](0006-config-universal-or-gone.md) | Configuration: universal or gone — frozen boolean knobs removed, not toggled | Accepted | pending owner review | config.py |
| [0007](0007-fid-event-adapter.md) | Spellbook fid-to-Event adapter: 1,348-row production data layer mapping features to the bounded trigger taxonomy; consumed by the win-walk (ADR 0016) | Accepted | pending owner review | fid_adapter.py, data/fid_event_adapter.json |
| [0008](0008-canonical-normalization.md) | Canonical normalization: `CardAttribute` (one concept, provenance-tagged source in {curated, haiku, spellbook}); `ProvisionalAttributeStore` (lifecycle-named storage); no compat aliases | Accepted | pending owner review | machine_vocab.py |
| [0009](0009-unified-card-attribute-query-model.md) | Unified CardAttribute membership: one `card_attributes` `otag:`-query map + one compiled matcher — buckets and payoffs are not special cases; `bucket_attributes`/`support` are role lists beside the map; group-merge mechanism collapsed (byte-parity) | Accepted | pending owner review | attribute_catalog.json, assets.py, strategy_graph.py, query.py |
| [0011](0011-eventeffect-guards.md) | EventEffect guards: query/reference over a defined operand set at one evaluation chokepoint — characteristic CardAttribute-ref + bounded scope/identity enum + event-reachable ref + unresolved; the guard defines the producer set; per trigger-ability; scaffolded Phase 2A, evaluation 2B | Accepted | pending owner review | edge_roles.py, relay.py, machine_vocab.py |
| [0012](0012-event-ontology.md) | Event = the game's bounded trigger taxonomy; no `role` field — sink-ness is pure topology; `polarity`/`scope` on `game-end` only; mechanic-class is a CardAttribute not an Event; promiscuity-scaled substantiveness via ADR 0002 | Accepted | pending owner review | event_catalog.json, determine_strategy_substantiveness.py, strategy_graph.py |
| [0014](0014-engine-mass-election.md) | Engine = node-weighted event-neighborhood MASS election (the "2C flip"): mass authoritative for lead + the 0..n `elected` set (0.6 keep-ratio, Jaccard-0.5 dedup); recursive/hub demoted to reported attributes; three generic anchor modes; STRUCTURE-ONLY (0 labels change); distinct from `archetype_identity` | Accepted | pending owner review | strategy_graph.py (`elect_engine_units`), engine_report.py, relay.py |
| [0016](0016-closure-mechanism-win-walk.md) | Closure-mechanism win-walk: the authoritative combo win-detection path — graph walk over fid-adapter events + card-edge classes replaces regex + frozen sink list; deck-aware; guard-evaluated | Accepted | pending owner review | combo_win.py, data/card_edges.json, strategy_graph.py, combo.py |
| [0017](0017-edge-derivation-native-then-haiku.md) | Within-card edge derivation: native CardAttributes first, else haiku→machine-store; never code-side prose parsing. Two distinct haiku roles (A edge-author / B engine-namer) | Accepted | pending owner review | edge_roles.py, machine_vocab.py, relay.py |
| [0020](0020-event-substantiveness-producer-anchor.md) | Event-substantiveness admission: the `producer_anchor` Requirement kind (ADR 0002 third level) gates a promiscuous event's election candidacy by COMMAND-ZONE production anchor (absolute, threshold-free), not density; corrected taxonomy-derived `mana-produced` wiring (`ramp` out of mass, anchor-only via `extra_refs`); hooked before the mass election | Accepted | pending owner review | determine_strategy_substantiveness.py, strategy_graph.py, event_catalog.json |
| [0021](0021-pipeline-superstructure-card-strategy-engine.md) | **Superstructure capstone** — Card → Strategy → Engine as two phases (A: card-intrinsic process-once cache, `oracle_id`-keyed, haiku-once-ever; B: per-deck compute) and one ordering (**substantiveness → centrality/election → synthesis**; Strategy = the substantive sub-graph; Engine exists only at synthesis); composition map of every piece-ADR to its stage. B1 ON, B6 realized, B.3 wired (`engine_synthesis.py`, `engine_cache.py`, `claude -p` backend). All open items resolved. See [output contract](../strategy-engine-output-contract.md). | **Accepted** | 2026-11-15; Phase 5.6 full-corpus role-B (187 decks, 7/7 anchors, 0 wrong); B.3 wired `8aad1c2` | spans the package; owns the seams between 0001–0020; Tier-2: `engine_synthesis.py`, `engine_cache.py`, `roleb_prompt.md` |
| [0022](0022-concept-anchored-derived-adjacency.md) | Concept-anchored **derived adjacency** — the Maximal-mechanic-exclusive-anchor rule (enforced exclusivity validator; `ramp` auto-refused both sides) replaces the hand-enumerated otag→Event lists, DERIVED at catalog load; **rank-don't-cut** engine keep supersedes the ADR 0014 0.6 keep-ratio (#72 resolved); shadow flags retired; seed discovery (event-candidate evaluation) is the named direction | Accepted | 2026-11-14 flip; owner sees the delta at commit | derive_card_events.py, evaluate_event_candidates.py, assets.py, engine_hydration.py, strategy_graph.py, event_catalog.json |

*0005–0006 (tribe catalog, config universal-or-gone) were authored in the same change that
implemented them, from observed post-change behavior.*

*0007 (fid-to-Event adapter) and 0016 (closure-mechanism win-walk) were authored as part of
the Phase 2B step 5b-ii flip. 0014 (Engine mass-election) was authored with the 2C flip that
implemented it, from the re-validated full-170 delta. 0010, 0013, 0015 are reserved for the
remaining Design-2/3 fold (Strategy, DeckAssessment) and will be authored in that
change. 0009 (query contract) was authored with the WS1.2 consolidation that unified the
CardAttribute membership model (one `card_attributes` query map + one matcher), from the
byte-parity delta (all three P0 SHAs unchanged). 0011 (EventEffect guards) and 0012 (Event ontology) were authored ahead of the full
fold as part of Phase 2A scaffolding; 0012 was updated with the mechanic-class clause in 5b-ii.
0017 was authored ahead of the fold because a probe prototype violated its rule; it is closely
related to 0008/0011/0014 and may be merged into 0008 at fold time. 0020 (event-substantiveness
`producer_anchor`) was authored with the step-2 landing that implemented it, from the
empirically-validated full-170 delta; it extends 0002 (third application site) and amends 0014.
0022 (concept-anchored derived adjacency + rank-don't-cut) was authored with the 2026-11-14 task-6.5
flip that made it authoritative, from the full-170 gauntlet delta (0 genuine regressions); it
supersedes the uncommitted `0018-DRAFT` and amends 0003/0012/0014. 0018–0019 remain unused.*
