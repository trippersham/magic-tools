# Design — Productionize the in-search quad Driver

**Date:** 2026-08-25 · **Pivoted:** 2026-08-27 (combo-litmus + pure-measurement gate)
**Org/repo:** trippersham/magic-tools · plugin `make-magic`
**Status:** approved in brainstorming; combo-litmus pivot spec approved (G1, 2026-08-27). Next: `pivot-and-corpus-validation-plan.md`.
**Supersedes:** `design-external-driver-SUPERSEDED.md` (the external `-Dmakemagic.driverA` path — bankrupt for combo assembly).
**Companion research:** `FINDINGS.md`, `in-search-architecture.md`, `research/in-search-unification-analysis.md`, `research/compile-toolchain-research.md`, `research/solo-goldfish-quad-spike.md`, `references/driver-failure-cases.md`, **and the 6-thread combo-only evidence base (see "Evidence base" below).**

---

## 0. Goal

Ship the **in-search quad** Driver architecture as a real, usable make-magic feature: a project-owned XMage dist that ingests per-deck Drivers, an authoring workflow that derives a Driver from a deck's cards + Strategy, a **pure-measurement** solo-goldfish ship gate, and a gauntlet corpus that ships **with** a compiled Driver per combo deck (thin decks ship bare CP7). Scope = **B (full pipeline)** → dogfood author-batch → adversarial static review → whole-corpus statistical validation.

The proven architecture (see `in-search-architecture.md`): a Driver is a **quad `(Φ, P, macro, S)`** of pure functions injected INTO CP7's minimax via `playerId`-keyed registries + one-time jar hooks. This design productionizes that seam; it does not re-open the architecture.

**The pivot in one line.** The in-search Driver's PROVEN value is **COMBO-ONLY** and is a **capability change** (0 → fires the deterministic win), NOT a general win-rate lift. Non-combo decks (value/control/midrange) ship a **THIN** driver (neutral Φ=0 + optional real mulligan) = essentially bare CP7, because a content-bearing Φ that shapes toward a non-racing plan REGRESSES the solo clock. The classifier is therefore a **combo litmus**, and the gate is **pure measurement**.

---

## 1. Key decisions (locked in brainstorming + the 2026-08-27 pivot)

1. **Target = B, then dogfood, then corpus.** Full pipeline (seam + authoring), then classify+author across the whole corpus, adversarial static review, then the whole-corpus statistical run bucketed by archetype×wincon.
2. **Ownership boundary.** Project owns the **dist** (seam hooks + registries + quad-ingestion interface, new pinned `XMAGE_DIST_SHA256`). Users own **Drivers** (runtime code). The corpus ships **with a companion Driver per combo deck**.
3. **Seam delivery = committed patch series over pinned upstream** *(corrected by preflight — there is no make-magic fork; CI clones pristine `magefree/mage @ xmage_1.4.60V3` and `CommanderDuel` is an upstream module).* The seam (the `score/` package + registries + the `GameStateEvaluator2`/`ComputerPlayer6` edits) lives as a **reviewable patch series in-repo** (e.g. `pipeline/sim/java/xmage-dist/patches/*.patch`) that CI `git apply`s onto the cloned upstream tag *before* `mvn install`, then rebuilds the shaded dist. All seam classes + edits land in the AI modules so they compile together. The `XMAGE_DIST_SHA256` re-pin is **in scope on purpose**.
4. **Driver artifact = reviewable `.java` + project-precompiled `.class`.** Source is the artifact of record (diffable in PRs, user-editable). The project pre-compiles the shipped corpus so *using* the gauntlet needs only the JRE.
5. **Compile toolchain = ECJ.** Eclipse Compiler for Java (`ecj`, ~3.21 MB) compiles user-authored Drivers on the **JRE we already provision** (Temurin 21) — no JDK, no +145 MB. Fetched + SHA-pinned like the dist jar. (`research/compile-toolchain-research.md`.)
6. **Ship gate = PURE MEASUREMENT (capability + relative + brick-cap validity).** No bracket/archetype-presuming absolute turn bar. The gate measures: **capability** (`MACRO_FIRE_REAL` — the macro actually executes via `act()`, not merely `DRIVER_MACRO_FIRED` reachability) + **relative never-slower** vs the SAME deck on bare CP7 (±2 turn tolerance, solo own-turn clock) + **brick-cap validity** (opponent-deckout-at-cap = brick, not a pass — the Jeleva-freeze pathology) + `_MIN_GATE_GAMES=20` floor. Commander decks route `fmt='commander'`. There is **NO absolute/bracket/archetype turn threshold anywhere** — the CRISPI scorer derives the bracket FROM the measured speed, so hardcoding a bracket bar in the gate is a **circular dependency**. (See §6.)
7. **Classifier = COMBO LITMUS, not a proactive/reactive spine.** The top-level routing question is a pure combo litmus (DRIVE iff a concrete in-deck win-combo, else THIN = bare CP7). Proactive/reactive is **demoted** from "the spine" to an **internal input of rule-4 Φ-mode selection** (dedicated vs combo-capable) — it no longer routes at the top level. The deck-primer / `strategy-schema` reading is preserved as a **lens** (a reading, not a format change) that informs Φ authoring; the tagger/strategy-schema contract is unchanged. (See §7.)
8. **Base branch.** Abandon `feat/perdeck-driver-authoring` (the bankrupt external-driver tier). Branch fresh from `feat/shared-perdeck-driver`; cherry-pick preserved data/knowledge forward. Corpus-validation runs on `feat/insearch-quad-driver`.

---

## Evidence base — why COMBO-ONLY (the 6 research threads)

The combo-only conclusion and the pure-measurement gate are not assumptions — they are the reconciled result of six empirical threads (raw logs under `research/`). One-line summaries:

1. **Gut-check defended lens** (`research/gutcheck-defended-lens.md`) — **REFUTED** the hope that content-bearing quads (real Φ+S+mulligan, the ones solo REJECTED) beat bare CP7 in a *defended* duel. Verdict "thin-is-honest": the solo gate's rejection of content Φ for value/control/midrange was the CORRECT call; a content Φ that shapes toward a non-racing plan regresses the clock. → drives the THIN=bare-CP7 rule.
2. **Goldfish turn benchmarks** (`research/goldfish-turn-benchmarks.md`) — external citeable ground truth for a "successful goldfish" own-turn clock per archetype; combo/aristocrats goldfish meaningfully, control "generally never closes solo." Confirms a solo clock is a valid signal ONLY for proactive/combo decks — and that bracket turn-lengths are 4-player interactive numbers, NOT a solo gate bar (→ no bracket bar in the gate).
3. **Goldfish turn-by-turn** (`research/goldfish-turn-by-turn.md`) — per-own-turn trajectories (board/hand/library/life) distinguishing steady-progress vs develop-then-stall vs archetype-correct-slow; shows the Jeleva reference quad's real combo kill vs CP7's textbook durdle/freeze. Grounds `MACRO_FIRE_REAL` (real execution) as the load-bearing capability signal, not reachability.
4. **Aristocrats combo trace** (`research/aristocrats-combo-trace.md`) — the harder board-ASSEMBLED combo (Ghave/Yawgmoth sac-drain loop) **FIRES** in a solo goldfish once the commander is seated (P6.0 fix); `MACRO_FIRE_REAL` in both. Board-assembly reachability limit (Signal B) does NOT hold. → combo decks beyond 2-card-in-hand lines are drivable; motivates `fmt='commander'` routing.
5. **Gist / prior-research reconciliation** (`research/prior-research-reconciliation.md`) — verbatim digest of the prior corpus: combo (Jeleva) fires 0/90 → 52 commits/9 wins ("directional, not statistically separable"); Φ-only reactive (Shorikai) mechanism "real and strong" but **win-rate FLAT** both arms. Confirms capability-not-winrate for combo, and no measurable win result for reactive.
6. **Mulligan ablation** (`research/mulligan-ablation.md`) — a plan-aware mulligan-ONLY driver (neutral Φ, no macro/S) vs bare CP7's land-count heuristic. Net conclusion: mulligan-in-isolation is **net-negative/CLOSED** → thin = bare CP7; the mulligan slot stays **optional** (its defended value is deferred, #46-adjacent).

**Net:** value from the Driver appears only where a concrete deterministic win-combo exists and the base AI cannot self-assemble/fire it. Everything else is honestly thin.

---

## 2. Architecture & ownership boundary

Two project-owned, SHA-pinned jars + user-owned Drivers:

- **Dist jar** (project-built): XMage engine + make-magic harness + **the quad seam**:
  - three registries as new AI-module classes — `DriverBonus` (Φ), `MacroRegistry` (P + macro), `SelectionRegistry` (S);
  - two hook patches — `GameStateEvaluator2.evaluate` (Φ leaf hook, bounded ±1e6, `specialScore==0`) and `ComputerPlayer6.simulatePriority`/`act` (macro folded into every node's alpha-beta + real fire) + `chooseTarget/choose` (S steer at both the real-CP7 and SP2 rollout seats);
  - a stable **quad-ingestion interface**: a Driver registers `(Φ, P, macro, S)` (+ mulligan) by `playerId`. The id is invariant across `createSimulationForAI` copies and the SP2 swap, so registration reaches every leaf/rollout.
- **ECJ jar** (fetched, SHA-pinned): compiles Drivers on the existing Temurin-21 JRE.
- **Driver** (user-owned runtime code): a `.java` quad class, classpath-injected into `XMageBatch`; static-init registers itself by `playerId`. Reviewable source is the artifact of record.

## 3. Seam delivery (corrected by preflight)

**Reality:** `xmage-dist-release.yml` does `git clone --depth 1 --branch xmage_1.4.60V3 magefree/mage` (pristine upstream) → `mvn -pl Mage.Tests,…Mage.Player.AI.MA,…Mage.Game.CommanderDuel -am install` → `build.sh` shades the dist. `CommanderDuel` is upstream, not a fork module. The make-magic additions today are the *harness overlay* (`pipeline/sim/java/xmage/src`: `XMageBatch` + `mage.collectors.MakeMagicHooks`), compiled against the dist and shaded — a pattern for **new** classes, which cannot override existing upstream method bodies.

**Mechanism:** the seam ships as a **committed patch series** (`pipeline/sim/java/xmage-dist/patches/NNNN-*.patch`, `git format-patch` style, reviewable). It adds the `score/` package (registries + Φ/P/macro/S types) *and* the `GameStateEvaluator2.evaluate` + `ComputerPlayer6.simulatePriority`/`act`/`chooseTarget`/`choose` edits — all inside the AI modules so they compile in one reactor pass. CI gains a `git apply "$DIST_DIR/patches"/*.patch` step (with a `git apply --check` guard that fails loudly if a patch stops applying on an XMage bump) between clone and `mvn install`; `build.sh` documents the same for local builds. Then: **new `XMAGE_DIST_SHA256`** pin in `xmage_runtime.py` + the release-tag rebuild. The patch series is the artifact to maintain across XMage version bumps — smaller and more reviewable than a fork.

## 4. Driver artifact & compile path

- In-repo: `drivers/<identity>__<commander>.java` next to each `.dck`, **plus** a project-pre-compiled `.class`/jar for the shipped corpus (using the gauntlet needs only the JRE — no ECJ, no compile).
- Authoring a *new* Driver: the skill emits `.java`; the harness runs ECJ (`java -jar ecj.jar --release 21 -cp <dist>`), caches the compiled artifact keyed by **(dist SHA, source hash)**, and surfaces ECJ stderr diagnostics into the skill's repair loop. Cached Drivers invalidate on a dist bump / SHA change.

## 5. Authoring pipeline (the B rewrite — combo-litmus)

`authoring-drivers` skill is rewritten to run a **combo litmus first**, then deterministically **seed the quad from the detected Combo**. `driver_authoring.py` emits quad Java (a class implementing the quad-ingestion interface) instead of an external `ComputerPlayer7` subclass.

**Authoring flow (the 8 locked rules):**
1. **Litmus (rule 1).** Run `combos_in_deck(deck cards)`. **DRIVE** iff it returns ≥1 *concrete* combo whose `result` is a game-win (a lethal / "win the game" payoff present in the 99 — not a bare "infinite mana/draw" with no in-deck finisher) and all pieces are in the 99. Else **THIN**: emit a `QuadSpec.thin()` (neutral Φ=0, optional mulligan), record "bare CP7", stop.
2. **Tiebreak (rule 2).** Among qualifying win-combos, seed from the **fewest-pieces** one, preferring one the **commander participates in**. ≥2 equally-central win lines → author the primary + **flag** (second-line macro is a follow-up).
3. **Seed the quad (rule 3, deterministic).** From the chosen `Combo`: `card_names` → **P** (`applicable` = all pieces present + castable this turn) + **S** (fetch the missing piece via any tutor category, category-comprehensive per §7.1); `result` → **macro** (`apply` enacts the win with bounded moves, no `priority()`/`copy()`). One fixed template filled from the Combo — the author does NOT invent the line.
4. **Φ-mode (rule 4, auto).** **DEDICATED** (full combo-Φ staging assembly) iff the commander is a combo piece **OR** ≥3 dedicated tutors for the pieces **OR** the Strategy names the combo as the primary win. Else **COMBO-CAPABLE** (thin Φ=0 + macro+P+S — serendipitous capture, no distortion). Borderline → default thin-Φ + flag. *(Proactive/reactive intuitions inform this Φ-mode choice — they are an input here, not the top-level router.)*
5. Optional **mulligan** slot (`MulliganSteer.shipHand`) — a real slot, but OPTIONAL (ablation showed mulligan-in-isolation net-negative).
6. Emit `.java` → ECJ compile → **gate** (§6).

**Authoring knowledge base (preserved from the external design, re-homed):** the api-primitives / pattern-catalog / combo-package references carry over. Critically, the *minimax-purity constraints* still apply to the parts of the quad that run at the real seat — the S steer and the macro's real fire key on `getState().getPriorityPlayerId()`, and no Driver code overrides `copy()`. (In the external design these framed the whole driver; here they scope only the real-seat slots, because Φ and the in-search macro-fold run inside the search by design.)

## 6. The ship gate — PURE MEASUREMENT

Reuses `driver_gate.py` (never-worse) + `driver_compare.py` (vs-CP7). The gate is **pure measurement** — it presumes **no bracket, no archetype, and no absolute turn bar**. It reads only these signals:

- **Capability:** `MACRO_FIRE_REAL` — the macro actually **executes** via a real `act()` on the live game (not merely `DRIVER_MACRO_FIRED` reachability). A DRIVE deck whose macro never really fires FAILS.
- **Relative (never-slower):** the driven deck's solo own-turn clock is **never slower than the SAME deck on bare CP7**, within a **±2 turn tolerance**. This is the only speed signal — always relative to the same deck's own CP7 baseline, never to an absolute turn number.
- **Validity (brick-cap guard):** an opponent-deckout-at-cap is a **brick**, NOT a pass (the **Jeleva-freeze pathology** — the game freezes and the opponent decks out at the turn cap, which must not read as a driven win).
- **Floor:** `_MIN_GATE_GAMES=20`. Below this, baseline CP7 jitter false-fails the never-slower comparison.
- **Commander routing:** commander decks route `fmt='commander'` (seats the commander + `loadCards(sideboard)`; landed in P6.0 / commit 4ca4368). A maindeck-only stub would false-VETO a commander-dependent macro.

**Why NO absolute/bracket/archetype turn bar.** The CRISPI scorer derives a deck's **bracket FROM its measured speed**. If the gate presumed a bracket-specific turn bar (e.g. "Bracket 4 must kill by T4"), it would be asserting the very quantity CRISPI computes downstream — a **circular dependency**. The gate therefore measures capability + relative speed + validity only; classification into brackets happens later and elsewhere.

**THIN decks** (no in-deck win-combo) emit a neutral driver and are recorded as **bare CP7** — they are not gated for macro-fire (they have no macro); they are the CP7-both-sides control cells in the corpus run.

Statistical *superiority* is never a per-deck ship requirement — it is measured only at the **corpus** level (§10/§11, bucketed by archetype×wincon).

## 7. The classifier & the deck-primer lens (combo-litmus)

The **top-level classifier is the combo litmus** (§5 rule 1): DRIVE iff a concrete in-deck win-combo, else THIN. Proactive/reactive is NOT the router — it is demoted to an internal input of the rule-4 Φ-mode selection.

The recognized deck-primer structure (`strategy-schema` / `distilling-strategy`) is preserved as a **lens** — a *reading* of the deck that informs Φ authoring and the combo seed, **not** a schema/format change and **not** a routing spine. The tagger/strategy-schema CONTRACT is unchanged. It maps onto the quad like so:

| Primer section (lens) | Role in authoring |
|---|---|
| **Gameplan / identity** | informs **Φ** (dedicated staging vs neutral) |
| **Win condition(s)** | corroborates the litmus + the **macro** seed |
| **Assembly / the combo turn** | corroborates **P** (`applicable`) |
| **Key sequencing & choices** (← KEY LINES, at category/mechanic altitude) | **S** authoring + macro ordering |
| **Mulligan / keepable hands** | the OPTIONAL mulligan hook |
| *Proactive or reactive? / archetype* | **input to the rule-4 Φ-mode choice only** — NOT the top-level route |

The primer is a vocabulary/structure *reading*; the quad format is unchanged. Routing is by the combo litmus, and the gate is pure measurement (§6) — the primer never sets a gate mode or a turn bar.

### 7.1 Preserving the KEY LINES authoring intuitions (do not lose in the refactor)

The KEY LINES work earned hard-won intuitions. The refactor *re-homes* each rather than dropping it:

- **"Own the noun, defer the verb"** (own what to cast/target/keep; never own combat/sequencing) → becomes the authoring guidance for the **S slot** and the macro's real-fire step. This is the core discipline that keeps a Driver from regressing CP7's competent decisions.
- **Category/mechanic altitude, not individual cards** (a line addresses *all* cards in a category/mechanic, not one named card) → the **Sequencing** authoring rule, and it maps to how **S** should be written: match by comprehensive predicate over a category (e.g. "any top-of-library tutor," "any sacrifice outlet") rather than a single card name. `getName()` is acceptable *when comprehensive for a whole category* (knowable at design time) — that refinement is preserved verbatim as S-authoring guidance.
- **The pattern catalog** → preserved as the **pattern-catalog reference**, re-expressed as quad shapes: selection patterns → **S**; combo/loop → **macro + Φ**; mulligan → the OPTIONAL mulligan hook. (The reactive "hold-interaction / protect-commander → Φ-only" patterns survive only as **thin/flag** cases — they do not measure as a win and are not a DRIVE route; see the pattern-catalog reference.)
- **The do-not-own guardrails** (attacker-selection, force-attack, generic combat/blocks, land drops, politics, `copy()`-override) → preserved as authoring guardrails; still enforced by a template/skill check + a test.
- **The three-beat guard→act→defer shape** → still the shape of the real-seat slots (S, macro real-fire): guard on `isMyPriority`, act once, defer to `super` everywhere else.

## 8. Preserve / rewrite / retire

- **Preserve:** gauntlet corpus + manifest, api-primitives / pattern-catalog / combo-package references, `strategy-schema` + `distilling-strategy` KEY-LINES work (reframed as a lens + intuitions re-homed per §7/§7.1), `driver_compare.py`, `driver_gate.py` (metric = pure measurement), the top-level `driver` CLI shell, `drivers.py` registry/persistence, the failure-cases catalog.
- **Rewrite:** `driver_authoring.py` (emit quad; `QuadSpec.thin()`; combo-seeding helper), `authoring-drivers/SKILL.md` (combo-litmus front gate + rules 1–4), `XMageBatch.java` (register-by-playerId + Driver injection + ECJ compile step), the dist build (absorb `score/` + hook patches), `driver_gate.py` (pure-measurement metric + `fmt='commander'`).
- **Retire:** the external `-Dmakemagic.driverA` override path, the `probeWins` / go-no-go template (subsumed — go/no-go is emergent in-search), the greedy-assembly external skeleton, **the proactive/reactive routing spine** (demoted to a rule-4 input), **any bracket/archetype turn-bar gate language**.

## 9. Base branch & migration

Abandon `feat/perdeck-driver-authoring`. Branch fresh from `feat/shared-perdeck-driver` (keeps the useful Phase 0-4 seam: classpath injection, registry, gate/compare harnesses, speed routing). Cherry-pick the preserved data/knowledge (gauntlet corpus, references, strategy-schema, failure-cases) forward onto it rather than stacking the bankrupt external-driver code. Corpus validation runs on `feat/insearch-quad-driver`.

## 10. Corpus validation scope (Part II)

Verification is the **whole-corpus statistical validation** in `pivot-and-corpus-validation-plan.md` Part II, not a hand-picked ladder:

1. **Dogfood author-batch** — run the combo-litmus classify+author flow on **every inventory + gauntlet deck**; DRIVE decks seed+compile+gate, THIN decks record bare CP7. Restartable per-deck ledger + a corpus classification manifest {drive|thin, dedicated|capable, detected combos, archetype, wincon-style}.
2. **Adversarial static review (no sim)** — per-driver rule-5 rubric (NECESSARY? / LIKELY-PERFORMS? → {keep, downgrade-thin, re-author, flag}); PLUS the rule-6 false-negative safety net (statically read every THIN decklist for a missed combo, promote if found).
3. **Whole-corpus statistical run** — field `resolve_gauntlet('both')`, **games=20/opp**, subjects = inventory AND gauntlet decks; driver-piloted vs CP7-piloted; thin = CP7-both-sides control. Under a resource+JVM monitor + APFS COW staging.

**Ship rule (rule 8).** Group per-deck deltas by **archetype × wincon** bucket. A bucket **ships driven** iff the pooled driver-vs-CP7 lift's **95% Wilson CI excludes 0 (positive)** AND **n ≥ 30 matchups**. Below n or CI-includes-0 → thin/inconclusive (reported honestly with n). Per-deck statsig NOT required.

## 11. Sequencing (phase headlines)

Per `pivot-and-corpus-validation-plan.md`:

0. **Pivot design of record** (this doc) — combo-litmus + pure-measurement gate.
1. Skill retarget + emitter (combo-litmus front gate; `QuadSpec.thin()`; combo→P/macro/S seeder).
2. Gate = pure measurement + `fmt='commander'` fix.
3. Dogfood author-batch across the whole corpus → classification manifest.
4. Adversarial static review (rule-5 rubric + rule-6 safety net).
5. Whole-corpus statistical run + intervening resource/JVM monitor + COW staging → bucket aggregation.
6. Behavioral verification + corpus-thesis verdict (user gate G2) → merge note.

## 12. Risks & mitigations

- **Fork-patch maintenance across XMage bumps** — the two hook patches touch upstream classes; keep them minimal and documented; pin the XMage version.
- **Dist re-pin invalidating cached user Drivers** — mitigated by keying the compile cache on dist SHA (auto-recompile on bump).
- **`combo_detect` false negatives** — the offline Spellbook snapshot misses combos; mitigated by the rule-6 safety net (static re-read of THIN decklists).
- **Corpus thesis is empirical** — whether driving the combo subset reproduces a bucket-level lift is unknown until the run; null-reportable (user gate G2).
- **`--release` bytecode mismatch** — keep ECJ `--release` matched to the dist bytecode level; invalidate cached Drivers on a dist bump.

## 13. Acceptance criteria

- **AC1** The dist jar carries the seam (registries + both hook patches), is re-pinned via a new `XMAGE_DIST_SHA256`, and CI rebuilds it reproducibly.
- **AC2** `XMageBatch` classpath-injects a Driver `.java`→ECJ-compiled artifact that registers its quad by `playerId`; an unregistered opponent stays pure CP7.
- **AC3** Authoring a DRIVE deck (concrete in-deck win-combo) via the rewritten skill seeds a quad deterministically from the detected Combo (rules 1–4), ECJ-compiles it, and passes the **pure-measurement** gate (`MACRO_FIRE_REAL` + never-slower vs same-deck bare CP7 ±2 + brick-cap valid) — no hand-editing.
- **AC4** A THIN deck (no in-deck win-combo) emits a neutral driver (Φ=0 + optional mulligan) recorded as bare CP7 — no macro, no false-fail for lacking one.
- **AC5** The gate rejects a DRIVE quad whose macro never really fires (`MACRO_FIRE_REAL` absent), one that is slower-than-same-deck-CP7 beyond ±2, and an opponent-deckout-at-cap brick; it applies NO bracket/archetype absolute turn bar.
- **AC6** The dogfood author-batch produces a terminal classification for every corpus deck; the adversarial review verdicts + rule-6 promotions are applied; the whole-corpus run aggregates to archetype×wincon buckets with the rule-8 ship call per bucket.
- **AC7** ECJ is fetched + SHA-pinned and compiles a Driver on the provisioned JRE (no JDK); compiled artifacts cache-key on (dist SHA, source hash) and invalidate on a dist bump.
- **AC8** The preserved KEY LINES intuitions (§7.1) are present in the rewritten skill/knowledge base: own-noun-defer-verb, category-altitude S, do-not-own guardrails (with the `copy()`/combat test), three-beat shape.
</content>
</invoke>
