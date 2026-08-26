# Implementation Plan — Productionize the in-search quad Driver

**Design:** `design.md` (this dir) · **Supersedes:** `implementation-external-driver-SUPERSEDED.md`
**Target branch:** `feat/insearch-quad-driver` off `feat/shared-perdeck-driver`
**Date:** 2026-08-25 · **Planner:** `/planning`

---

## Preflight results

| Assumption (design) | Reality (verified) | Resolution |
|---|---|---|
| Seam lives on a make-magic `magefree/mage` **fork** | **No fork.** CI `xmage-dist-release.yml:79` clones pristine `magefree/mage @ xmage_1.4.60V3`; `CommanderDuel` is an **upstream** module (`:81`) | **Corrected → committed patch series** over the pinned upstream tag (design §3). No fork to maintain. |
| POC seam source available to turn into patches | `~/mtg-sim-lab/xmage-lab/.../ai/score/` has `DriverBonus/ComboMacro/MacroRegistry/SelectionRegistry/SelectionSteer` + patched `ComputerPlayer6.java` | **OK.** Patches derived by diffing POC source vs the pristine `xmage_1.4.60V3` tag. |
| Base-branch seam files present | `drivers.py`, `driver_gate.py`, `driver_authoring.py`, `xmage_runtime.py` present on `feat/shared-perdeck-driver`; **`driver_compare.py` ABSENT** (added only in the abandoned tier) | **Cherry-pick `driver_compare.py`** (+ preserved data/knowledge) forward in Phase 0. |
| Harness overlay pattern for new classes | `pipeline/sim/java/xmage/src/{mage,org}` (MakeMagicHooks + XMageBatch), shaded against the dist | New classes *can* overlay; **existing-class edits cannot** → they must go in the reactor patch. |

**Preflight status: PASS with corrections** (fork→patch-series correction folded into `design.md`; no blockers).

**Standing risk (for the implementer):** the two hook edits touch upstream method bodies — a `git apply --check` gate must fail loudly on an XMage bump. The existing license-audit + `--warm` smoke CI gates must stay green after the patch step.

---

## Execution guardrails (apply to EVERY task)

- **TDD** (`superpowers:test-driven-development`) — failing test first for every Python/harness task. Java seam patches are verified by the dist smoke + the behavioral gate (Phase 6/8), not pytest.
- **Verification command** — each task states an executable check + expected output. Never "it works."
- **Adaptation protocol** — STOP and surface if: a patch stops applying, the dist SHA won't stabilize, ECJ can't compile against the dist, or the solo-valid gate assumption fails to reproduce. Do not paper over.
- **Phase retrospective** — at each phase boundary append to `workpad.md`: decisions, surprises, follow-ups.
- **Code-review gate** — `/reviewing` at each phase boundary (scope = that phase's diff). Local lenses only (no billed cloud review — user preference).
- **No broad `pkill java`** — kill only own PIDs. **JVM runs** foreground-with-timeout or detached nohup+poll; never dispatch-async-and-park.

---

## Phase 0 — Branch & migration  ·  posture: **implementing**

- **0.1** Worktree `.worktrees/insearch-quad` + branch `feat/insearch-quad-driver` off `feat/shared-perdeck-driver`.
  *Verify:* `git log --oneline -1` → `7a91e7e`; `git branch --show-current` → `feat/insearch-quad-driver`.
- **0.2** Carry forward **preserved** artifacts from `feat/perdeck-driver-authoring` (NOT external-driver code): the 156-deck gauntlet + manifest, `references/{xmage-api-primitives,pattern-catalog,combo-package}.md`, the `strategy-schema`/`distilling-strategy` KEY-LINES work, `references/driver-failure-cases.md`, and **`sim/driver_compare.py`** + tests.
  *Verify:* gauntlet `.dck` count = 156; `driver_compare.py` present; `uv run pytest tests/test_driver_compare.py -q` collects.
- **0.3** Land `design.md` + this plan into `references/`; capture the **retire-list** (external `driverA` path, `probeWins`, greedy skeleton) as a Phase-3/4 deletion checklist in `workpad.md`.
  *Verify:* files present; retire-list recorded.

**Gate:** `/reviewing` Phase-0 diff → retrospective.

---

## Phase 1 — The seam: patch series + dist rebuild + re-pin  ·  posture: **collaborating** (upstream-edit judgment)

- **1.1** Derive the patch series: diff the POC `score/` package + `ComputerPlayer6`/`GameStateEvaluator2` edits vs the pristine `xmage_1.4.60V3` tag → `pipeline/sim/java/xmage-dist/patches/NNNN-*.patch` (`git format-patch` style). Seam classes + hook edits all in the AI modules (one reactor pass).
  *Verify:* on a fresh tag clone, `git apply --check patches/*.patch` succeeds; `mvn -pl …AI.MA,…AI.MAD -am install -DskipTests` builds.
- **1.2** Wire the patch step into `build.sh` (local) + `xmage-dist-release.yml` (CI) between clone and `mvn install`, with a `git apply --check` fail-loud guard.
  *Verify:* `build.sh` produces the dist jar; license-audit + `--warm` card-db smoke pass.
- **1.3** Define the **quad-ingestion interface** (registration by `playerId`) as the stable public seam API; document in `references/xmage-api-primitives.md`.
  *Verify:* a throwaway Driver registers; the seam no-ops for an unregistered `playerId` (1-deck smoke).
- **1.4** Rebuild dist, cut the re-pin release per the runbook, set `XMAGE_DIST_SHA256`.
  *Verify:* `verify-published-pin` + `verify-committed-harness-jar` green; `test_xmage_dist_sha_is_pinned` passes.

**Gate:** `/reviewing` (extra scrutiny on the two upstream edits) → retrospective.

---

## Phase 2 — ECJ compile-and-cache  ·  posture: **implementing**

- **2.1** ECJ fetch + SHA-pin in `xmage_runtime.py`, mirroring the dist-jar pattern (fetch, verify, re-hash on cache hit; `None` fails closed).
  *Verify:* `test_ecj_fetch_and_pin` passes.
- **2.2** Compile helper: `java -jar ecj.jar --release <lvl> -cp <dist> <Driver.java>` → cache keyed by **(dist SHA, source hash)**; structured ECJ stderr diagnostics.
  *Verify:* good `.java` → `.class`; broken `.java` → structured diagnostics (fixture test).
- **2.3** Cache invalidation on dist-SHA change; `--release` matched to dist bytecode level.
  *Verify:* `test_driver_cache_invalidates_on_dist_bump` passes.

**Gate:** `/reviewing` → retrospective.

---

## Phase 3 — XMageBatch: register-by-playerId + Driver injection  ·  posture: **implementing**

- **3.1** Rewrite the `XMageBatch.java` Driver path: classpath-inject the ECJ-compiled Driver, register by the real player's `playerId`; opponent unregistered (pure CP7).
  *Verify:* 1-deck run logs the Driver registering + seam firing for PlayerA only.
- **3.2** Retire the external `-Dmakemagic.driverA` path + `probeWins`/go-no-go template + greedy skeleton.
  *Verify:* no live references (grep); harness build + `verify-committed-harness-jar` green.
- **3.3** Python harness wires author→ECJ compile→inject→run.
  *Verify:* end-to-end driven run from a hand-authored Jeleva quad `.java` → game result with macro-fire log lines.

**Gate:** `/reviewing` → retrospective.

---

## Phase 4 — `authoring-drivers` rewrite + Strategy primer reframe  ·  posture: **collaborating**

- **4.1** Reframe the Strategy schema to the deck-primer structure (Gameplan/Win-Condition/Assembly/Sequencing/Mulligan), proactive/reactive-anchored; KEY LINES → Sequencing subsection. Update `distilling-strategy` + `strategy-schema`.
  *Verify:* schema doc renders primer sections + the proactive/reactive routing rule; one deck re-expressed.
- **4.2** Rewrite `driver_authoring.py` to emit a **quad Java class** from primer sections (Φ←Gameplan, macro←Win-Condition, P←Assembly, S←Sequencing, +mulligan; absent-macro for Φ-only).
  *Verify:* `test_driver_authoring` — quad renders for proactive + Φ-only fixtures; ECJ-compiles.
- **4.3** Rewrite the `authoring-drivers` SKILL to derive the quad from primer Strategy; **re-home the §7.1 intuitions** (own-noun-defer-verb → S; category-altitude S incl. `getName()`-when-comprehensive; do-not-own guardrails; three-beat shape). Carry pattern-catalog/api-primitives/combo-package refs re-expressed as quad shapes.
  *Verify:* skill-eval on 1 deck emits a compiling quad; a `copy()`-override violation is rejected by `test_driver_guardrails`.

**Gate:** `/reviewing` → retrospective.

---

## Phase 5 — Gate metric swap (dual-mode solo)  ·  posture: **implementing**

- **5.1** Swap `driver_gate.py` to the **solo own-turn clock** + **slot-exercise** assertion; dual-mode: proactive = macro-fires + never-slower-than-CP7; reactive = never-worse-solo floor (+ opt-in defended lens).
  *Verify:* `test_driver_gate` — proactive-fires-and-not-slower PASSES; never-fires FAILS; reactive floor passes.
- **5.2** `driver_compare.py` aligned to the solo clock; opt-in "power run" (~2× games) flagged, default off.
  *Verify:* `test_driver_compare` passes; power-run default off.

**Gate:** `/reviewing` → retrospective.

---

## Phase 6 — Verification ladder (dogfood the tools)  ·  posture: **collaborating**

Author each shape *via the Phase-4 skill*, gate in its mode (design §10). **If a shape fails, STOP** — design signal before the corpus.

- **6.1** CLEAN combo — Jeleva. *Verify:* skill-authored quad passes the proactive solo gate (reproduces the spike through the productionized path).
- **6.2** damage-loop — Mikaeus. *Verify:* proactive gate passes.
- **6.3** Φ-only reactive — Shorikai. *Verify:* never-worse-solo floor passes; reactive value on the opt-in defended lens.
- **6.4** STRAINED selection — a reanimator/sac deck. *Verify:* gate passes; **S fires beyond tutoring** (log evidence).
- **6.5** value-drain — Ghave. *Verify:* passes or honestly vetoed (no false fire).

**Gate (ladder):** all 5 pass → `/reviewing` → retrospective.

---

## Phase 7 — Full corpus authoring + ship Drivers  ·  posture: **implementing** (parallelizable)

Only after Phase 6 passes.

- **7.1** Author a quad per gauntlet deck (parallelized; monitor agent for mem/CPU/disk ~every 10 min; per-deck ledger for restartability).
  *Verify:* ledger shows per-deck status; failures honestly recorded.
- **7.2** Gate each; **project pre-compiles** passing Drivers → ship `.java` + `.class` alongside each `.dck`.
  *Verify:* a JRE-only using-the-gauntlet smoke runs a shipped Driver; coverage numbers logged (authored/gated/shipped, drops named — no silent truncation).

**Gate:** `/reviewing` → retrospective.

---

## Phase 8 — Behavioral verification (mandatory final)  ·  posture: **collaborating**

Prove the feature works as a user experiences it.

- **8.1** **Author-a-new-deck (JDK/ECJ):** fresh deck Strategy → skill → ECJ compile → gate → driven sim → printed Tier-2 Speed. **AC3.**
- **8.2** **Use-the-gauntlet (JRE-only):** on a no-JDK path, run a shipped corpus deck + its pre-compiled Driver; no compile invoked. **AC6/AC7.**
- **8.3** **Seam integrity:** driven PlayerA registers; CP7 opponent stays pure (log both ways). **AC2.**
- **8.4** **Reproduce the headline:** Jeleva driven vs vanilla CP7, solo — macro fires (driven) vs 0 (vanilla), driven own-turn clock ≤ vanilla. **AC5.**
- **8.5** Update the gist pack + post the follow-up to gist `9b81` (solo goldfish is valid **for the quad** — reverses the earlier extrapolation).

**Final gate:** `/reviewing` full-branch → retrospective → merge-decision note (merging pulls the `shared-perdeck-driver` stack; land that first or together).

---

## Posture summary
- **Implementing:** 0, 2, 3, 5, 7 (clear criteria, pattern-following).
- **Collaborating:** 1 (upstream method-body edits), 4 (skill/authoring design), 6 & 8 (empirical, judgment on pass/veto).

## Acceptance criteria → phase map
AC1 seam+repin→P1 · AC2 register/no-op→P3,P8.3 · AC3 author-proactive-passes→P4,P6.1,P8.1 · AC4 reactive floor+defended→P5,P6.3 · AC5 gate rejects/slot-exercise→P5,P8.4 · AC6 ladder+corpus ship→P6,P7,P8.2 · AC7 ECJ-on-JRE+cache-key→P2,P8.2 · AC8 KEY-LINES intuitions + copy()/combat test→P4.

## Follow-ups (not in this plan)
- JVM sandboxing (#44) — Drivers are compiled+run user Java; trust posture unchanged.
- Declarative quad spec (option C) — future migration once the successful-driver landscape is understood.
- Statistical power runs — opt-in per headline deck, not a ship gate.

---

## Confidence: **medium-high**
The architecture is proven; preflight found + resolved the one real divergence (fork→patch-series). Residual uncertainty is execution-mechanical: patch-maintenance across XMage bumps, and whether the *skill* reliably derives passing quads across all 5 shapes (Phase 6 is the deliberate gate for that — corpus is blocked on it).
