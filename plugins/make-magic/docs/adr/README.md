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
| [0002](0002-substantiveness-generic-evaluator.md) | Substantiveness: one generic evaluator over a closed kind-set + per-archetype data, no archetype-name branches | Accepted | ratified 2026-11-11 | substantiveness.py, facts.py |
| [0003](0003-byte-parity-held-files.md) | Byte-parity held files: the curated layer is byte-frozen, changed only by a proof-carrying re-baseline | Accepted | ratified 2026-11-11 | 6 held curated files |
| [0004](0004-node-predicates.md) | Node predicates: distinct `node_eligible` (presence gate) and `node_admitted` (graph admission), one impl each | Accepted | pending owner review | facts.py, engine_report.py |
| [0005](0005-tribe-resolution-creature-type-catalog.md) | Tribe resolution: Scryfall creature-type catalog allowlist, not a hand blocklist | Accepted | pending owner review | tribes.py, creature_types_catalog.json |
| [0006](0006-config-universal-or-gone.md) | Configuration: universal or gone — frozen boolean knobs removed, not toggled | Accepted | pending owner review | config.py |

*0005–0007 (tribe catalog, config universal-or-gone, combo feature-ID) are authored in the
same change that implements them, from observed post-change behavior.*
