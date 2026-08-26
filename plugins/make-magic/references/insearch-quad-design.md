# Design — Productionize the in-search quad Driver

**Date:** 2026-08-25
**Org/repo:** trippersham/magic-tools · plugin `make-magic`
**Status:** approved in brainstorming; next step `rae-flow:planning` → implementation.md
**Supersedes:** `design-external-driver-SUPERSEDED.md` (the external `-Dmakemagic.driverA` path — bankrupt for combo assembly).
**Companion research:** `FINDINGS.md`, `in-search-architecture.md`, `research/in-search-unification-analysis.md`, `research/compile-toolchain-research.md`, `research/solo-goldfish-quad-spike.md`, `references/driver-failure-cases.md`.

---

## 0. Goal

Ship the **in-search quad** Driver architecture as a real, usable make-magic feature: a project-owned XMage dist that ingests per-deck Drivers, an authoring workflow that derives a Driver from a deck's Strategy, a cheap solo-goldfish ship gate, and a 156-deck gauntlet that ships **with** a compiled Driver per deck. Scope = **B (full pipeline)** → verification ladder (dogfooding the authoring tools) → full corpus.

The proven architecture (see `in-search-architecture.md`): a Driver is a **quad `(Φ, P, macro, S)`** of pure functions injected INTO CP7's minimax via `playerId`-keyed registries + one-time jar hooks. This design productionizes that seam; it does not re-open the architecture.

---

## 1. Key decisions (locked in brainstorming)

1. **Target = B, then verify, then corpus.** Full pipeline (seam + authoring), then a smoke ladder across deck shapes that *uses* the authoring tools as real verification, then the full 156-deck corpus if the ladder passes.
2. **Ownership boundary.** Project owns the **dist** (seam hooks + registries + quad-ingestion interface, new pinned `XMAGE_DIST_SHA256`). Users own **Drivers** (runtime code). The corpus ships **with a companion Driver per deck**.
3. **Seam delivery = committed patch series over pinned upstream** *(corrected by preflight — there is no make-magic fork; CI clones pristine `magefree/mage @ xmage_1.4.60V3` and `CommanderDuel` is an upstream module).* The seam (the `score/` package + registries + the `GameStateEvaluator2`/`ComputerPlayer6` edits) lives as a **reviewable patch series in-repo** (e.g. `pipeline/sim/java/xmage-dist/patches/*.patch`) that CI `git apply`s onto the cloned upstream tag *before* `mvn install`, then rebuilds the shaded dist. All seam classes + edits land in the AI modules so they compile together. The `XMAGE_DIST_SHA256` re-pin is **in scope on purpose**.
4. **Driver artifact = reviewable `.java` + project-precompiled `.class`.** Source is the artifact of record (diffable in PRs, user-editable). The project pre-compiles the shipped corpus so *using* the gauntlet needs only the JRE.
5. **Compile toolchain = ECJ.** Eclipse Compiler for Java (`ecj`, ~3.21 MB) compiles user-authored Drivers on the **JRE we already provision** (Temurin 21) — no JDK, no +145 MB. Fetched + SHA-pinned like the dist jar. (`research/compile-toolchain-research.md`.)
6. **Ship gate = cheap solo own-turn clock, dual-mode.** The solo-goldfish spike proved the quad **fires the combo in a solo goldfish and kills faster than vanilla CP7** (21 macro fires vs 0; median own-turn 12 vs 15), so the cheap solo clock is a valid Speed signal for proactive decks. Reactive (Φ-only) decks keep a never-worse-solo floor + optional defended lens. (`research/solo-goldfish-quad-spike.md`.)
7. **Strategy section = deck-primer structure, proactive/reactive-anchored.** Reframe the human-facing Strategy from "KEY LINES" to the recognized deck-primer vocabulary (Gameplan / Win Condition / Assembly / Sequencing / Mulligan), led by the proactive↔reactive archetype call. KEY LINES becomes the "Sequencing" subsection — its *intuitions are preserved, re-homed* (§7.1). A vocabulary/structure change that projects onto the quad — **not** a change to the quad format.
8. **Base branch.** Abandon `feat/perdeck-driver-authoring` (the bankrupt external-driver tier). Branch fresh from `feat/shared-perdeck-driver`; cherry-pick preserved data/knowledge forward.

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

## 5. Authoring pipeline (the B rewrite)

`authoring-drivers` skill is rewritten to **derive the quad from a deck's Strategy** (primer-structured — see §7). `driver_authoring.py` emits quad Java (a class implementing the quad-ingestion interface) instead of an external `ComputerPlayer7` subclass. Authoring flow: read primer Strategy → classify proactive/reactive → derive Φ (potential toward Gameplan), P (Assembly/readiness), macro (Win Condition sequence; absent for Φ-only reactive), S (Sequencing choice steers), mulligan → emit `.java` → ECJ compile → gate.

**Authoring knowledge base (preserved from the external design, re-homed):** the api-primitives / pattern-catalog / combo-package references carry over. Critically, the *minimax-purity constraints* still apply to the parts of the quad that run at the real seat — the S steer and the macro's real fire key on `getState().getPriorityPlayerId()`, and no Driver code overrides `copy()`. (In the external design these framed the whole driver; here they scope only the real-seat slots, because Φ and the in-search macro-fold run inside the search by design.)

## 6. The ship gate (dual-mode, cheap by default)

Reuses `driver_gate.py` (never-worse) + `driver_compare.py` (vs-CP7), metric swapped from external-driver solo-speed to the **solo own-turn clock** + a **slot-exercise** assertion.

- **Proactive decks (has macro):** compiles + registers + **macro demonstrably fires** (≥ some fires across the batch) + **never-slower than vanilla CP7 on the solo own-turn clock** (n≈12–15). No match harness.
- **Reactive decks (Φ-only):** compiles + registers + **never-worse-solo floor**; reactive *value* measured only via an **opt-in defended lens** (not a ship blocker — a passive goldfish gives a reactive deck nothing to react to).
- Statistical *superiority* is an opt-in "power run" (~2× games) for headline decks, never a ship requirement.

## 7. Strategy section reframe (deck-primer, proactive/reactive-anchored)

The recognized deck-primer structure replaces the KEY-LINES-as-schema framing; it maps ~1:1 onto the quad, so it reduces bespoke invention rather than adding a parallel artifact.

| Primer section (recognized) | Quad slot (machine) |
|---|---|
| **Proactive or reactive? / archetype** (aggro/combo/midrange/control) | routes: macro-deck vs Φ-only → **which gate mode** |
| **Gameplan / identity** | **Φ** |
| **Win condition(s)** | **macro** |
| **Assembly / the combo turn** | **P** |
| **Key sequencing & choices** (← KEY LINES, at category/mechanic altitude) | **S** + macro ordering |
| **Mulligan / keepable hands** | mulligan hook |

Touches: §5 (skill derives the quad from primer sections) and the preserved `strategy-schema` / `distilling-strategy` (primer reframing, KEY LINES → Sequencing subsection). Scoped explicitly as a vocabulary/structure change — the quad format is unchanged.

### 7.1 Preserving the KEY LINES authoring intuitions (do not lose in the refactor)

The KEY LINES work earned hard-won intuitions. The refactor *re-homes* each rather than dropping it:

- **"Own the noun, defer the verb"** (own what to cast/target/keep; never own combat/sequencing) → becomes the authoring guidance for the **S slot** and the macro's real-fire step. This is the core discipline that keeps a Driver from regressing CP7's competent decisions.
- **Category/mechanic altitude, not individual cards** (a line addresses *all* cards in a category/mechanic, not one named card) → the **Sequencing** subsection's authoring rule, and it maps to how **S** should be written: match by comprehensive predicate over a category (e.g. "any top-of-library tutor," "any sacrifice outlet") rather than a single card name. `getName()` is acceptable *when comprehensive for a whole category* (knowable at design time) — that refinement is preserved verbatim as S-authoring guidance.
- **The pattern catalog** (reanimate-bomb, sac-fodder, hold-interaction, protect-commander, go-wide-anthem, discard-keep-plan, mull-for-plan, combo-loop) → preserved as the **pattern-catalog reference**, re-expressed as quad shapes: selection patterns → **S**; reactive patterns → **Φ-only**; combo/loop → **macro + Φ**; mulligan → the mulligan hook.
- **The do-not-own guardrails** (attacker-selection, force-attack, generic combat/blocks, land drops, politics, `copy()`-override) → preserved as authoring guardrails; still enforced by a template/skill check + a test.
- **The three-beat guard→act→defer shape** → still the shape of the real-seat slots (S, macro real-fire): guard on `isMyPriority`, act once, defer to `super` everywhere else.

## 8. Preserve / rewrite / retire

- **Preserve:** gauntlet corpus (156 `.dck` + manifest), api-primitives / pattern-catalog / combo-package references, `strategy-schema` + `distilling-strategy` KEY-LINES work (reframed + intuitions re-homed per §7/§7.1), `driver_compare.py`, `driver_gate.py` (metric-swapped), the top-level `driver` CLI shell, `drivers.py` registry/persistence, the failure-cases catalog.
- **Rewrite:** `driver_authoring.py` (emit quad), `XMageBatch.java` (register-by-playerId + Driver injection + ECJ compile step), the dist build (absorb `score/` + hook patches).
- **Retire:** the external `-Dmakemagic.driverA` override path, the `probeWins` / go-no-go template (subsumed — go/no-go is emergent in-search), the greedy-assembly external skeleton.

## 9. Base branch & migration

Abandon `feat/perdeck-driver-authoring`. Branch fresh from `feat/shared-perdeck-driver` (keeps the useful Phase 0-4 seam: classpath injection, registry, gate/compare harnesses, speed routing). Cherry-pick the preserved data/knowledge (gauntlet corpus, references, strategy-schema, failure-cases) forward onto it rather than stacking the bankrupt external-driver code.

## 10. Verification ladder (dogfoods the B tools)

Author quads via the *new* skill for one deck per shape → gate each in its mode → then the corpus:

- **CLEAN combo** — Jeleva (Thoracle)
- **damage-loop** — Mikaeus
- **Φ-only reactive** — Shorikai (defended lens)
- **STRAINED selection** — a reanimator/sac deck (proves S beyond tutoring)
- **value-drain** — Ghave (P-requires-drain + counted macro vs opp life)

Ladder gate: all shapes pass their mode's gate → run the full 156-deck corpus, **ship the compiled Driver + `.java` alongside each gauntlet deck**.

## 11. Sequencing (phase headlines)

1. Seam into the fork + dist rebuild + `XMAGE_DIST_SHA256` re-pin (load-bearing plumbing).
2. ECJ fetch/pin + compile-and-cache in the harness.
3. `XMageBatch` register-by-playerId + Driver injection.
4. `authoring-drivers` skill rewrite (derive quad from primer-structured Strategy; §7.1 intuitions re-homed).
5. Gate metric swap (solo clock + slot-exercise + dual-mode).
6. Verification ladder (5 shapes) → gate.
7. Full corpus authoring + ship Drivers with the gauntlet.

## 12. Risks & mitigations

- **Fork-patch maintenance across XMage bumps** — the two hook patches touch upstream classes; keep them minimal and documented; pin the XMage version.
- **Dist re-pin invalidating cached user Drivers** — mitigated by keying the compile cache on dist SHA (auto-recompile on bump).
- **~half of quad kills convert via combat, not combo** — the gate requires *fires-at-least-sometimes*, not fires-always; a headline power-run is opt-in.
- **Reactive-deck value is defended-lens-only** — accepted; not a ship blocker; solo never-worse floor still applies.
- **`--release` bytecode mismatch** — keep ECJ `--release` matched to the dist bytecode level; invalidate cached Drivers on a dist bump.

## 13. Acceptance criteria

- **AC1** The dist jar built from the fork carries the seam (registries + both hook patches), is re-pinned via a new `XMAGE_DIST_SHA256`, and CI rebuilds it reproducibly.
- **AC2** `XMageBatch` classpath-injects a Driver `.java`→ECJ-compiled artifact that registers its quad by `playerId`; an unregistered opponent stays pure CP7.
- **AC3** Authoring a new proactive deck via the rewritten skill derives a quad from its primer Strategy, ECJ-compiles it, and passes the solo gate (macro fires + never-slower) — no hand-editing.
- **AC4** A reactive (Φ-only) deck authors + passes the never-worse-solo floor; its reactive value is measurable via the opt-in defended lens.
- **AC5** The gate rejects a quad that never fires its macro (proactive) and one that's slower-than-CP7 on the solo clock; slot-exercise assertion is enforced.
- **AC6** The 5-shape verification ladder passes end-to-end using the authoring tools (dogfood), then the full corpus authors + ships a compiled Driver + `.java` per gauntlet deck.
- **AC7** ECJ is fetched + SHA-pinned and compiles a Driver on the provisioned JRE (no JDK); compiled artifacts cache-key on (dist SHA, source hash) and invalidate on a dist bump.
- **AC8** The preserved KEY LINES intuitions (§7.1) are present in the rewritten skill/knowledge base: own-noun-defer-verb, category-altitude S, do-not-own guardrails (with the `copy()`/combat test), three-beat shape.
