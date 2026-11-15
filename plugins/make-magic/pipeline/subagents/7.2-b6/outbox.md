# 7.2-b6 — outbox

**Task:** wire ARCHETYPE-grain substantiveness into the engine election (`_isolate_engines`),
realizing ADR 0021's ordering invariant (substantiveness → centrality → synthesis) at the
archetype grain. OUTPUT-CHANGING on the frozen P0 path → measure full-170 delta, confirm 0
genuine regressions, re-baseline. NO commit. HEAD `369f209`. Full report:
`research/b6-election-gate-170-delta.md`.

## The gate as built

`_isolate_engines(..., earned_archetypes=None)`. Between the EVENT-grain `event_admitted` filter
and the rank-don't-cut keep-walk, a new ARCHETYPE-grain filter drops a candidate whose payoff
archetypes are ALL unearned AND that is not structurally anchored. KEEP a unit iff (owner policy):

- **(a)** ≥1 EARNED `payoff_archetype` — `any(a in earned_archetypes for a in e['payoff_archetypes'])`.
- **(b)** structurally anchored — `commander_coupled` (`_members(e) & cmd_attrs`) OR a non-bipartite
  anchor mode (`_anchor(e) != 'bipartite'`: terminal-producer / exogenous-environmental).
- **(c)** no archetype claim — `payoff_archetypes` empty (pure structural event-loop).

`earned_archetypes` is computed in `compute_facts` as
`{arch for arch, st in strategies.items() if requirement_for(arch, cfg).satisfied(st, ctx, arch)}`
— the SAME one-named ADR 0002 evaluator the naming layer consults. No new threshold/knob, no
deck-specific branch; the earned-verdict is the existing per-archetype data. `engine_isolation`
is now placed into `facts` POST-ctx (the evaluator reads ctx). Scope = the P0 election only;
`relay.py` passes `earned_archetypes=None` (grain OFF → `engine_report` unchanged).

**Code:** `pipeline/transforms/strategy/facts.py` only (`_isolate_engines` signature + gate +
docstring; `compute_facts` computes the earned set and sets engine_isolation post-ctx).
`satisfied()` is the right bar and does NOT over-gate: requires_met-alone would be a no-op
(graveyard "earned" at ≥3 members); `satisfied()` gates exactly the already-`gy_demoted` set.

## Decks changed + classification (the gate)

**28/170 changed. 28 units REMOVED, 0 ADDED. 28 IMPROVEMENT, 0 NEUTRAL (removal sense), 0
REGRESSION.** 0 `named_strategy` changes. 1 determinism flip (`dimir__xanathar-guild-kingpin`
`mass-elected` → `loop-or-flat-no-forward-hub`; its sole elected unit was a share-0.542 incidental
graveyard loop → election empties; label `theft` unchanged). Elected-set size OFF mean 3.95 / max
10 → ON mean 3.78 / max 10 (one deck to 0). Removed-unit share: 6 <5%, 22 ≥5%.

**EVERY removed unit is an incidental `graveyard/reanimator` engine** (`card-enters-graveyard` /
`recursion-from-graveyard`) on a deck whose graveyard identity is ALREADY `gy_demoted`
(`requires_met` True, `satisfied` False) and whose label is non-graveyard (combat, enchantress,
spellslinger, lifegain, artifacts, theft, …). Full 28-row table in the research doc.

## 0-genuine-regression confirmation

**Confirmed: 0 genuine regressions (HARD GATE met — re-baseline proceeded).** Structural proof:
the gate consults the IDENTICAL `satisfied()` verdict that already drives the shipped `gy_demoted`
naming demotion, so it introduces NO new judgment and NO new false-negative surface. The guarded
failure ("a genuine engine gated via a `requires_met` false-negative") is not realizable because
the gate reads `satisfied()`, not `requires_met`. Duplicate-named decks are each gated on their
own earned set (the `aminatou`/`sydri` instances where graveyard IS earned keep their unit). 0
labels changed → no deck lost its identity.

## #76 retired? — NO

B6 does **NOT** retire #76. The Alesha `land-enters-battlefield` / `landfall` unit (share ~0.017)
is KEPT: `landfall` clears the default `member_count(3)` bar (3 generic ramp/removal cards tagged
`landfall`: Path to Exile, Solemn Simulacrum, Sun Titan), so it is exempt under policy (a).
Alesha's elected set is byte-identical OFF vs ON. Retiring #76 needs a landfall-specific density
`Requirement` (owner-graded ADR 0002 DATA), outside B6's generic reuse of the existing bars —
NOT done. #76 stays open.

## Old → new SHAs (re-baseline)

| SHA | OFF (`369f209`) | ON (B6) |
|---|---|---|
| engine_isolation | `a7edab20…17e177` | `afcffc8a…f35c047` |
| resolve | `25f50a23…ca464a` | `3010be2a…931769` |
| full-corpus `FULL_CORPUS_SHA` | `5ce447ef…c24f343` | `4acf0f41…b66092` |

Updated SHA constants in 8 `.py` files (`engine_parity_oracle.py` + 7 test files). Regenerated
`fixtures/engine_isolation_baseline.json` + `fixtures/vocab_parity_baseline.json` (7 fixture decks
shifted, all same graveyard-demotion pattern). Regenerated fixture composites match the
independent full-corpus capture.

## P0 reproduces

- OFF reproduced the three committed P0 SHAs exactly (faithful baseline).
- ON full-corpus SHA reproduced from the live lake via `engine_parity_oracle.full_corpus_sha()`
  == new `FULL_CORPUS_SHA` (`4acf0f41…`). MATCH.

## Suite / lint

- `uv run --extra dev pytest pipeline/transforms/strategy/tests -q` → **547 passed, 4 skipped, 0
  failed** (16 SHA-parity failures before re-baseline; 0 behavioral failures throughout).
- ruff: 0 new findings (facts.py keeps only the 2 pre-existing `∪` ambiguous-char findings).
  pyright (standard): facts.py 0 errors.

## ADR + docs

- ADR 0021 — B6 realized (Status + context bullet + consequence); P0 SHAs updated.
- ADR 0014 — 2026-11-15 amendment (archetype-grain admission; reconciles the dropped
  substantiveness-floor "Options not taken" bullet); Code-paths + Related-records updated.
- ADR 0002 — level-2 verdict now also gates election candidacy (second consumer, no model change).
- ADR 0022 — #76 annotated NOT retired by B6.
- Research: `research/b6-election-gate-170-delta.md`.

## NO commit

Per task — orchestrator verifies the delta + commits. Working tree holds: `facts.py` + 8 SHA
`.py` files + 2 fixture JSONs + 4 ADR `.md` + this research doc + this outbox. Nothing committed.

## Confidence

**HIGH.** The gate is a minimal, generic reuse of the existing `satisfied()` verdict; the 0-
regression result is structurally guaranteed (same verdict as the shipped `gy_demoted`), not just
empirically observed. Both P0 SHAs reproduce from the live lake; suite green; lint clean. The one
honest negative is #76: B6 does not retire it (the `landfall` noise clears the generic default
bar) — correctly reported rather than forced.
