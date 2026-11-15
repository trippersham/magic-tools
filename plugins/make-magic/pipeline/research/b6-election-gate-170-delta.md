# B6 — archetype-grain substantiveness upstream of the engine election (full-170 delta)

**Task 7.2 — wire ARCHETYPE-grain substantiveness into `_isolate_engines` (ADR 0021 B6).**
HEAD `369f209`. Lake: `~/.local/share/make-magic/{normalized,raw}/*.parquet`.
OUTPUT-CHANGING on the frozen P0 path (`compute_facts` → `_isolate_engines`) — re-baselined.
Measurement harness + raw records: scratchpad `off.json` / `on.json` (position-keyed diff).

## TL;DR

- **Gate built.** `_isolate_engines` now takes `earned_archetypes` and, after the EVENT grain
  (`event_admitted` / `producer_anchor`), drops any candidate whose payoff archetypes are ALL
  **unearned** AND that is not structurally anchored. The earned verdict is the EXISTING per-
  archetype data — `requirement_for(arch).satisfied(st, ctx, arch)`, the SAME one-named [0002]
  evaluator the naming layer consults. No new threshold/knob, no deck-specific branch.
- **Gate: 0 GENUINE regressions.** 28/170 decks change their elected set; **28 units removed,
  0 added; 0 `named_strategy` changes; 1 determinism flip (Xanathar → `loop-or-flat`, elected
  empties).** EVERY removed unit is an incidental `graveyard/reanimator` engine on a deck the
  engine ALREADY demotes (`gy_demoted`: `requires_met` True, `satisfied` False). The gate makes
  no judgment the shipped engine did not already make.
- **#76 NOT retired.** B6 does NOT demote the Alesha `land-enters-battlefield` / `landfall`
  unit — `landfall` clears the default `member_count(3)` bar (3 generic ramp/removal cards
  tagged `landfall`), so it is exempt under policy (a). Retiring #76 needs a landfall-specific
  density Requirement (owner-graded [0002] DATA), outside B6's generic reuse of the existing bar.
- **Re-baselined** (output-changing, [0003]): engine `a7edab20…`→`afcffc8a…`, resolve
  `25f50a23…`→`3010be2a…`, full-corpus `5ce447ef…`→`4acf0f41…`. New full-corpus SHA reproduced
  from the live lake via `engine_parity_oracle.full_corpus_sha()`. Suite 547 pass / 4 skip;
  ruff/pyright clean (0 new findings).

## The gate as built

`_isolate_engines(nodes, event_edges, adens, assets, cards, earned_archetypes=None)`. The new
filter sits between the event-grain `event_admitted` filter ([0020]) and the rank-don't-cut
keep-walk ([0022]):

```
pool = [e for e in engines if event_admitted(e['event'], cmd_attrs, assets)]   # EVENT grain
if earned_archetypes is not None:                                              # ARCHETYPE grain (B6)
    pool = [e for e in pool if _archetype_admitted(e)]
```

`_archetype_admitted(e)` — KEEP the unit iff any exemption holds (owner-approved policy):

- **(a)** `any(a in earned_archetypes for a in e['payoff_archetypes'])` — ≥1 EARNED payoff
  archetype.
- **(b)** `_members(e) & cmd_attrs` (commander-coupled) **or** `_anchor(e) != 'bipartite'`
  (a non-bipartite anchor mode — terminal-producer / exogenous-environmental). Structural
  anchoring.
- **(c)** `not e['payoff_archetypes']` — the unit makes NO archetype claim (a pure structural
  event-loop).

GATE only a unit whose payoff archetypes are ALL unearned AND is not structurally anchored.
Note on **"combo-anchored"** (listed in the owner policy as a structural mode): combo edges
feed `engine_report`, not the `compute_facts` election path (B1), so no combo-anchored unit
is representable inside `_isolate_engines` today — it is vacuously absent on this path, not a
missing exemption.

`compute_facts` computes `earned_archetypes = {arch for arch, st in strategies.items() if
requirement_for(arch, cfg).satisfied(st, ctx, arch)}` POST-ctx (the evaluator reads ctx) and
places `facts['engines']['engine_isolation']` into the shared `facts` dict afterward. The
`relay.py` path (feeding `engine_report`) passes `earned_archetypes=None` — grain OFF, structure-
only — so B6 is scoped to the P0 election only, exactly as [0021] specifies.

### satisfied() vs requires_met-alone — which composes

Policy (a) uses `satisfied()` (the full bar). It does NOT over-gate; it is strictly necessary:

- For the ~33 DEFAULT archetypes, `satisfied() == requires_met` (member_count(3), noncmd=0),
  so those claims are earned whenever present.
- The only archetypes with a STRICTER-than-`requires_met` bar are `graveyard/reanimator`
  (`attr_density`), `control/permission` (`axis_density`), and the EDHREC-new labels
  (`member_count(3, noncmd=2)`).
- Empirically on the 170, the ONLY sole-claim-unearned units are `graveyard/reanimator`
  (attr_density False while requires_met True). **If the gate used `requires_met` alone it
  would be a complete no-op** (0 decks changed) — graveyard would be "earned" at ≥3 members.
  So `satisfied()` is what makes the gate meaningful, and it gates EXACTLY the set the engine
  already `gy_demoted`s — i.e. it is identical to the naming-layer judgment. There is no
  over-gating to fall back from.

## Measurement method

Both arms run `recalc_corpus()` (170 gauntlet `.dck`, built via `corpus.build_deck_from_dck`
+ `attach_meta`, no Airtable) against the SAME live lake:

- **OFF** = committed `369f209` working tree (clean) — reproduced the three committed P0 SHAs
  exactly (`5ce447ef…` / `a7edab20…` / `25f50a23…`), confirming a faithful baseline.
- **ON** = this change.

`recalc_corpus` order is deterministic and identical across arms, so the delta is **position-
keyed** (not name-keyed): the gauntlet contains duplicate deck NAMES (e.g. `isshin`, `aminatou`,
`sydri` each appear twice, and the duplicate instances can differ in whether graveyard is
earned), so a name-keyed diff silently deduped and under-counted; the position-keyed diff is
the correct 170-row comparison.

## Classified delta (the gate)

**28/170 decks change their elected set. 28 IMPROVEMENT, 0 NEUTRAL (removal sense), 0
REGRESSION. 28 units removed, 0 added. 0 `named_strategy` changes. 1 determinism flip.**

- **IMPROVEMENT (28 units):** an incidental `graveyard/reanimator` engine unit
  (`card-enters-graveyard` or `recursion-from-graveyard`) is demoted from the election on a
  deck whose graveyard identity is ALREADY `gy_demoted` (not substantively graveyard). The
  election now agrees with the label.
- **0 added units** — the Jaccard-dedup re-exposed no new unit; every change is a pure removal.
- **1 determinism flip:** `dimir__xanathar-guild-kingpin` (label `theft`) — its ONLY elected
  unit was a share-0.542 incidental graveyard loop; removing it empties the election and flips
  `determinism` `mass-elected` → `loop-or-flat-no-forward-hub`. Faithful: Xanathar is a
  theft/control goodstuff deck with no substantive forward-hub engine. Label unchanged.
- **Elected-set size:** OFF max 10, mean 3.95 → ON max 10, mean 3.78 (bounded; one deck to 0).
- **Removed-unit share:** 6 below 5%, 22 at/above 5% — so this is NOT only "low-share junk"
  cleanup; the gate correctly demotes high-share incidental graveyard engines (Xanathar 0.542,
  Light-Paws 0.533, Tuvasa 0.273) whose mass was a mis-ranking, not a real identity.

### All 28 removed units (every one `graveyard/reanimator`, on a `gy_demoted` deck)

| share | deck | removed event | deck label | ON elected size |
|---|---|---|---|---|
| 0.026 | mardu__isshin-two-heavens-as-one | card-enters-graveyard | combat/aggro | 4 |
| 0.035 | boros__nelly-borca-impulsive-accuser | recursion-from-graveyard | combat/aggro | 4 |
| 0.037 | simic__aesi | card-enters-graveyard | landfall | 2 |
| 0.040 | orzhov__elenda-the-dusk-rose | card-enters-graveyard | aristocrats/sacrifice | 5 |
| 0.046 | AvengersAssemble_5c_2026 | recursion-from-graveyard | +1/+1 counters | 4 |
| 0.048 | mono-white__giada-font-of-hope | recursion-from-graveyard | lifegain | 5 |
| 0.053 | FacelessMenace_Sultai_2019 | recursion-from-graveyard | morph | 3 |
| 0.053 | esper__sydri-galvanic-genius | card-enters-graveyard | artifacts | 5 |
| 0.056 | mono-white__sram-senior-edificer | card-enters-graveyard | voltron/equipment | 4 |
| 0.060 | five-color__the-ur-dragon | card-enters-graveyard | typal/tribal | 9 |
| 0.060 | mono-white__heliod-sun-crowned | card-enters-graveyard | lifegain | 5 |
| 0.062 | mono-red__neheb-the-eternal | card-enters-graveyard | burn | 3 |
| 0.066 | mardu__isshin-two-heavens-as-one (dup) | card-enters-graveyard | combat/aggro | 2 |
| 0.067 | EldraziIncursion_2024 | card-enters-graveyard | typal/tribal | 6 |
| 0.067 | dimir__yuriko-the-tiger-s-shadow | card-enters-graveyard | combat/aggro | 2 |
| 0.067 | esper__aminatou-the-fateshifter | recursion-from-graveyard | alt-wincon/combo | 3 |
| 0.072 | bant__derevi-empyrial-tactician | card-enters-graveyard | alt-wincon/combo | 4 |
| 0.074 | WakandaForever_2026 | card-enters-graveyard | combat/aggro | 5 |
| 0.075 | JeskaiStriker_2025 | card-enters-graveyard | spellslinger | 2 |
| 0.075 | jeskai__shiko-and-narset-unified | card-enters-graveyard | spellslinger | 2 |
| 0.086 | bant__aang-at-the-crossroads | card-enters-graveyard | alt-wincon/combo | 5 |
| 0.099 | jeskai__narset-enlightened-master | recursion-from-graveyard | alt-wincon/combo | 4 |
| 0.138 | mono-blue__azami-lady-of-scrolls | card-enters-graveyard | control/permission | 2 |
| 0.187 | jeskai__kykar-wind-s-fury | card-enters-graveyard | spellslinger | 1 |
| 0.205 | izzet__vivi-ornitier | card-enters-graveyard | alt-wincon/combo | 1 |
| 0.273 | bant__tuvasa-the-sunlit | recursion-from-graveyard | enchantress | 1 |
| 0.533 | mono-white__light-paws-emperor-s-voice | recursion-from-graveyard | enchantress | 1 |
| 0.542 | dimir__xanathar-guild-kingpin | card-enters-graveyard | theft | 0 |

## 0-genuine-regression confirmation (the HARD GATE)

**Confirmed: 0 genuine regressions.** The proof is structural, not just enumerative:

1. Every removed unit's sole payoff archetype is `graveyard/reanimator`.
2. For every one of the 28 decks, the graveyard identity is ALREADY `gy_demoted` — i.e.
   `requires_met` True but `requirement_for('graveyard/reanimator').satisfied()` False — and the
   deck's `named_strategy` is NOT `graveyard/reanimator` (verified per deck).
3. **The gate consults the IDENTICAL `satisfied()` verdict that already drives `gy_demoted`
   (the naming demotion).** So there is NO new false-negative surface: if `attr_density` were a
   false negative for a genuine graveyard deck, that false negative ALREADY exists in the shipped
   naming behavior. B6 introduces no judgment the engine did not already make — it only applies
   the existing verdict to one more consumer (the election). The specific failure the HARD GATE
   guards against ("a genuine engine gated because its archetype was a `requires_met` false-
   negative") is **not realizable here**, because the gate reads `satisfied()`, not
   `requires_met`.
4. For the duplicate-named decks, each physical instance is gated on its OWN earned set: the
   `aminatou` / `sydri` instances whose graveyard IS earned (`satisfied` True) KEEP their
   graveyard unit; only the unearned instances lose it. No earned-graveyard deck lost a unit.
5. 0 `named_strategy` changes across all 170 → STRUCTURE-ONLY-vs-naming preserved; no deck lost
   its identity label.
6. Only Xanathar empties its election (in the 170); `precon__political-puppets` also empties in
   the fixture corpus — both are non-engine control decks (`theft` / `pillowfort`) whose sole
   elected unit was an incidental graveyard loop. Neutral-to-improvement, not a regression.

## #76 — retirement finding: NOT retired

The task hypothesized B6 would demote the Alesha (`mid__mardu__alesha` / `casual__mardu__alesha`)
`land-enters-battlefield` unit (share ~0.017, payoff `landfall`, built from fetchlands). It does
NOT. Measured on Alesha: `landfall` is PRESENT with `requires_met` True AND `satisfied` True —
its Requirement is the default `member_count(3)`, and it has 3 members (Path to Exile, Solemn
Simulacrum, Sun Titan — generic ramp/removal mis-tagged `landfall`). So `landfall` is EARNED,
and policy (a) KEEPS the unit. Alesha's elected set is byte-identical OFF vs ON.

**#76 is NOT retired by B6.** Its noise is an archetype that clears the generic default bar, not
an unearned one — so the generic archetype-grain gate cannot touch it without a mechanic-specific
tightening. Retiring #76 would require a landfall-specific density `Requirement` (an archetype-
grain bar stricter than `member_count`, analogous to graveyard's `attr_density`), which is a
separate owner-graded [0002] DATA decision. B6's scope is the generic gate reusing the EXISTING
bars; inventing a landfall bar is out of scope and was not done.

## Re-baseline — old → new SHAs

Output-changing on the P0 path ([0003]); all three SHAs move. New full-corpus SHA reproduced
from the live lake via `engine_parity_oracle.full_corpus_sha()` (MATCH confirmed).

| SHA | OFF (committed `369f209`) | ON (B6) |
|---|---|---|
| engine_isolation composite | `a7edab20703291c8380340bb542c5a11e25a2e53fa3964eb89ffbce20f17e177` | `afcffc8a6cee9d771bcb71b758d1de75664edb469da7da4d4bcfaf2f7f35c047` |
| resolve composite | `25f50a23d19ac98e37131f80b7ebf5d9d4bc1dd9ef78099e6de9a8a482ca464a` | `3010be2a0d1d5def69f6d63974d75d3023837c39fc8c04c056cfa88565931769` |
| full-corpus `FULL_CORPUS_SHA` | `5ce447ef25ac148f319512c288e83ac6c9aac5b4b3ee78c903da73530c24f343` | `4acf0f4166d2adbeeb4da63fcecf4a25b6f5d27374ab85010942733708b66092` |

**Updated SHA constants** (8 `.py` files): `engine_parity_oracle.py` (`FULL_CORPUS_SHA`),
`test_anchor_derivation.py`, `test_combo_edges.py`, `test_engine_report.py`, `test_healing_loop.py`,
`test_machine_vocab.py`, `test_relay.py`, `test_skill_authoring.py`.

**Regenerated fixtures** (per-deck SHA maps): `fixtures/engine_isolation_baseline.json` and
`fixtures/vocab_parity_baseline.json`. 7 fixture decks shifted, all the same pattern (incidental
graveyard unit removed from a `gy_demoted` non-graveyard deck): `facelessmenace_sultai_2019`
(morph), `tuvasa-the-sunlit` (enchantress), `owned__sokka` (spellslinger), `owned__spiders-play-
with-their-food` (aristocrats), `owned__the-fantastic-four` (spellslinger), `precon__blame-game`
(combat), `precon__political-puppets` (pillowfort → elected empties). The regenerated fixture
composites match the independent full-corpus capture exactly.

## Verification

- OFF reproduces the three committed P0 SHAs exactly (faithful baseline).
- ON full-corpus SHA reproduced from the live lake via `full_corpus_sha()` == new `FULL_CORPUS_SHA`.
- Suite: `uv run --extra dev pytest pipeline/transforms/strategy/tests -q` → **547 passed, 4
  skipped** (was 16 SHA-parity failures before re-baseline; 0 behavioral failures throughout).
- ruff: facts.py carries only the 2 PRE-EXISTING `∪` ambiguous-char findings (present at HEAD);
  0 new findings in any changed file. pyright (standard): facts.py 0 errors.

## ADR amendments

- **[0021]** — Status line: B6 realized; B6 OPEN item closed; P0 SHAs updated; delta summary.
  Context bullet "Election ∥ substantiveness (B6)" + the B6 consequence marked REALIZED.
- **[0014]** — new 2026-11-15 amendment (archetype-grain admission completes the ordering
  invariant); Code-paths line updated; reconciles the "Options not taken" substantiveness-floor
  bullet; Related-records 0002 line updated.
- **[0002]** — "Three application sites" callout: level-2 verdict now also gates election
  candidacy (B6) — a second consumer, no model change.
- **[0022]** — #76 annotated NOT retired by B6, with the reason.
- References **[0002]** (the Requirement the gate consults) throughout.
