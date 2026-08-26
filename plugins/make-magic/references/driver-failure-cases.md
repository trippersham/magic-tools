# Driver-design failure cases — catalog for the hardened regression strategy

Every failure mode we've hit building the per-deck XMage driver system, so that once the workflow is production-grade we can turn this into a regression suite. Each entry: **what fails · where we saw it · status · regression hook** (what a test would assert to catch a recurrence). Status = FIXED (locked, needs a regression test) · MITIGATED (rule/process, could regress) · OPEN (needs design) · INHERENT (harness/statistical limit to design around).

Legend for regression hook: **[unit]** pure/Python or render-string assert · **[compile]** real-jar javac · **[gate]** behavioral gate run · **[eval]** skill/LLM-behavior eval · **[harness]** orchestration/infra check.

---

## A. Authoring / skill failures (the LLM composing the line)

| # | Failure | Where seen | Status | Regression hook |
|---|---|---|---|---|
| A1 | **Closed-menu narrowness** — author confined to the 8 seed patterns; falls back to a lesser-fit pattern (or reports a gap and stops) instead of composing a novel line. | Kenrith → hold-interaction instead of the Thoracle line | FIXED (open-ended skill, commit a4f1521) | **[eval]** given a deck whose OWN line no seed encodes, the skill authors a NOVEL line (new intent tag), does NOT fall back; **[unit]** `render_driver` accepts arbitrary `members` (no whitelist) |
| A2 | **Pattern-from-archetype-label instead of the OWN noun** — scopes the guard to the archetype's stereotype, not the deck's actual line. | Szarel/WR: picked `sac-selection` scoped to a "Plant" token because archetype=aristocrats, but the deck sacs LANDS | FIXED (read OWN noun + category-scope) | **[eval]** authored guard noun matches the KEY LINE's OWN clause, not the archetype label |
| A3 | **Single-card-as-proxy for a category** — matches one card's `getName()` as a stand-in; misses comparable members, breaks on a swap. | WR land-sac outlets; general | FIXED (comprehensive enumerated name-set / semantic match) | **[unit]** authored guard enumerates the WHOLE category from `cards[]` (or a semantic test), never one name; **[gate]** a deck-swap adding a category member → `deck_version` flips → re-author |
| A4 | **Strategy at the wrong altitude** — KEY LINES too abstract ("sacrifice for value") to author a pilot line from. | WR (pre-fix), all decks under old schema | FIXED (piloting-altitude KEY LINES: sequence + OWN + DEFER) | **[eval]** distilled KEY LINES carry a concrete sequence + OWN (owned decision) + DEFER; a bare-axis line is rejected back to distilling |
| A5 | **Reference insufficient for novel authoring** — cast-from-hand / choose-card-name / tutor-search idioms undocumented; author had to infer. | Kenrith Thoracle smoke test | FIXING (P1 reference enrichment) | **[eval]** an author composing a spell-sequence/assembly line finds the idiom in the reference (no inference); **[compile]** the composed line compiles first try |

## B. Driver logic / correctness failures (the authored Java)

| # | Failure | Where seen | Status | Regression hook |
|---|---|---|---|---|
| B1 | **Owns the terminal cast/activation but NOT assembly** — relies on CP7 to draw/tutor/deploy the pieces, which CP7 won't do. | Kenrith (owned casts, not tutoring); combo-loop skeleton (activate-only, assumes pieces in play) | FIXING (P3 greedy-assembly: tutor→steer→deploy→fire) | **[gate]** a greedy-assembly driver ASSEMBLES + fires in SOLO goldfish where a terminal-only driver couldn't |
| B2 | **No lethal/stop condition** — fires a blind fixed cap regardless of already having won; can't end the game on the kill → overshoot. | Ghave (cap-20, no `checkIfGameIsOver`) | FIXING (lethal-stop `iWon()` + go/no-go P2) | **[unit]** a damage/drain combo skeleton stops on `checkIfGameIsOver`; **[gate]** it wins on the minimum activations, terminates fast |
| B3 | **Blind fire (no go/no-go)** — fires even when it's not a winning line / walks into a loss. | design gap surfaced by Ghave + the greedy discussion | FIXING (P2 `probeWins`) | **[gate]** A1 negative control: in a non-lethal state the probe VETOES (no fire) |
| B4 | **Guard too tight** — intent never fires because the assembled-state condition is narrower than the game reaches. | Hashaton att.1-2, Nethroi, Marchesa | MITIGATED (iterate-loop loosens; still recurring) | **[gate]** intent fires within ≤3 authoring attempts on a driver-worthy deck; log persistent `intent-not-fired` |
| B5 | **Owns too much / thick driver** — force-attack, re-pilot combat → worse than CP7. | original spike (thick force-attack driver lost to CP7) | FIXED (do-not-own rules + AC8 + never-worse gate) | **[unit]** no rendered driver contains `copy()` / `selectAttackers` override (AC8 test exists); **[gate]** never-worse |
| B6 | **Seed-skeleton compile bugs** — nonexistent API in a skeleton → every fill fails attempt 1. | `mage.game.MageObject`, `getManaPool().getManaCount()`, `@@MEMBERS@@` double-sub | FIXED (commits a0b5d35, eb5c46b) | **[compile]** EVERY seed skeleton + the template compiles against the real jar; **[unit]** exactly one `@@MEMBERS@@`, no `mage.game.MageObject`, no `getManaPool().getManaCount()` |
| B7 | **Can't prevent CP7 pre-casting a combo piece standalone** — thin driver defers priority; CP7 wastes/misfires a piece before assembly. | flagged in Kenrith smoke test | OPEN (structural; may need owning more, or match-mode) | **[gate]** with a greedy-assembly driver, measure combo-piece-wasted rate; design decision pending |

## C. Gate / measurement / harness failures (evaluating the driver)

| # | Failure | Where seen | Status | Regression hook |
|---|---|---|---|---|
| C1 | **Solo goldfish can't assemble a tutored combo** (base AI won't tutor+precast) → intent-not-fired or forced-assembly tempo cost. | Kenrith, Nethroi, Marchesa | FIXING (greedy-assembly B1 — owning assembly makes solo viable, no opponent to disrupt) | **[gate]** greedy-assembly driver fires in solo where terminal-only failed (== B1) |
| C2 | **Reactive/value drivers invisible to the solo own-turn metric** — hold-interaction / sac-value read flat or worse on the kill clock. | Tayam (+1.0), WR, Derevi/Shorikai/Jeleva (flat solo, match lift) | MITIGATED (`gate_mode=match` for reactive) | **[gate]** reactive patterns gate on MATCH; a match-mode driver's win-rate lift is the signal, not own-turn |
| C3 | **Combo durdles past the turn cap** (cEDH combo-control) → exit-143. | Kess | INHERENT (needs greedy-assemble to close faster, or match with a cap) | **[harness]** flag turn-cap hits distinctly from real fails |
| C4 | **Combo with no natural kill step** — `checkIfGameIsOver` can never fire (token loop) → lethal-stop mechanically useless; need guard-requires-payoff / go/no-go. | Ghave (Saprolings never reduce life) | FOUND empirically; FIXING (go/no-go probe evaluates terminal state, not per-step) | **[gate]** C2 smoke: a value+drain loop authored via go/no-go terminates cleanly; a value loop with NO win path is correctly not-driver-worthy |
| C5 | **Board-state explosion makes per-step sim expensive** (O(n²) cascades). | Ghave (suspected) | UNCERTAIN (empirical test couldn't reproduce; loop never fired) | **[gate]** if reproduced: cap owned activations + require-payoff guard bounds it |
| C6 | **Gate-volume vs real-hang confusion** — a 12-game author gate under contention exceeds the 10-min ceiling and LOOKS like a combo hang but isn't. | Ghave 143 (was gate volume, not a hang) | FOUND (measurement artifact) | **[harness]** record per-game wall-clock + game count; distinguish "N slow games" from "one hung game" before labeling a hang |
| C7 | **Seedless jitter** — driven vs baseline own-turn medians jitter ±1 at small N; the noise floor ≈ the effect size, so small effects vanish and neutral drivers pass/fail on coin-flips. | Tayam (gate 8v8 vs compare 9v8); Phase-2 tolerance | INHERENT (statistical) | **[gate]** raise `games` for tighter gates (not lower tolerance); report CIs; a "no-op negative-control driver" must read null |
| C8 | **Reanimator self-mill setup horizon exceeds the solo window** — can't fire in a bounded goldfish. | Nethroi, Marchesa | OPEN (needs match-mode or a yard-primed goldfish) | **[gate]** yard-primed goldfish (pre-seed the graveyard) or match-mode for reanimators |
| C9 | **Bash foreground timeout hard-caps at 600000 ms** — match gates exit-143 at 10:00; 900000 silently capped. | Kenrith, Ghave, Shorikai | MITIGATED (—games sizing; solo preferred; no >1 match gate concurrent) | **[harness]** timeouts ≤ 600000; match gates `--games ≤ 2`; run long gates via nohup+poll not one blocking call |
| C10 | **go/no-go probe is CONSERVATIVE on a regenerating (undying) loop** — the fixed pre-built `probeWins` steps carry stale `sourceId`s once a piece self-destructs and returns with a NEW identity (undying refuel), so the probe only confirms lethality within the CURRENT resource pool. SOUND (never a false GO), but over-vetoes a board the live refuel could win from. | A1 Mikaeus go/no-go (30 veto / 1 fire; fires only once opponent is already in reach of on-board counters) | FOUND empirically; BY-DESIGN boundary | **[gate]** for activated/regenerating loops use probeWins as a "lethal-blow-available-now" VETO + `iWon` STOP, NOT a full-combo simulator; for spell-sequence combos (stable step identity, e.g. Thoracle) the probe drives the full kill. A regenerating-loop deck must not depend on the probe to DRIVE assembly-to-lethal. |

## D. Orchestration / infra failures (running the study)

| # | Failure | Where seen | Status | Regression hook |
|---|---|---|---|---|
| D1 | **Async-waiter parking** — agents dispatch the gate async and yield, parking mid-gate with the result uncollected. | Derevi, Jeleva, Kess, Tayam, Shorikai, WR pilot | MITIGATED (foreground + explicit timeout rule in PIPELINE.md) | **[harness]** a pipeline agent runs gates in one foreground call; never re-arms an async waiter |
| D2 | **Broad `pkill java` kills sibling gates** — one agent's kill SIGTERMs others' JVMs. | Kess agent (a 2nd cause of batch-1 exit-143s) | FIXED (no-broad-kill rule; kill own PID subtree only) | **[harness]** no agent issues `pkill -x java`/`killall java`; kills target exact launched PIDs |
| D3 | **Concurrency oversubscription** — 5-wide → load 12.8, JVMs SIGTERM-killed. | batch 1 | FIXED (3-wide) | **[harness]** width ≤ derived pool; monitor load/RAM; back off on alarm |
| D4 | **Wrong-worktree drift** — agent ran in the wrong checkout. | Phase-1 agent (crispi-score not the driver worktree) | FIXED (hard-pin + verify branch first) | **[harness]** every agent verifies `git branch --show-current` before any edit |
| D5 | **API 529 kills an agent mid-run, losing the write-up** — result computed but report lost. | WR does-it-help (recovered from transcript) | MITIGATED (ledger durability + recover-from-transcript) | **[harness]** every stage persists to the ledger the instant it completes; results reconstructable from disk, not agent memory |
| D6 | **Staging-dir orphans from killed runs** — transient 39 MB H2 copies not `rmtree`'d, slow disk drift. | overnight run (21→18 GiB) | MITIGATED (between-batch sweep when java==0) | **[harness]** monitor sweeps `sim/staging/xmage-*` when no live java; alarm on disk<5 GiB |
| D7 | **Un-onboarded backend / ephemeral-only visibility** — collection backend not onboarded → only ephemeral drafts visible. | perdeck-authoring worktree (only 4 drafts) | FIXED (.env propagated + `collection onboard`) | **[harness]** pipeline verifies backend resolves the target deck before Stage 1 |

## E. Trust / verification failures (our own reporting — the meta class)

| # | Failure | Where seen | Status | Regression hook |
|---|---|---|---|---|
| E1 | **Overclaiming from single / weak / unverified data** — asserting a conclusion the data doesn't license. | "Tayam genuinely useful" (no evidence); "confirmed across corpus" (n=1 weak, overlapping CIs); "non-terminating to simulate" (unverified); "definitive read: board explosion" (loop never fired) | RECURRING — corrected each on scrutiny | **[process]** every reported finding names its evidence + a falsifiable claim; **NEGATIVE CONTROLS in the suite** (a no-op driver that MUST read null; a non-lethal deck the go/no-go MUST veto) so "it fired" ≠ "it helped" and "it passed" ≠ "it's correct" |

---

## Toward the regression suite (when we're production-grade)
Group the hooks into runnable tiers:
- **[unit] / [compile] — cheap, every commit:** A1/A3/B5/B6 (render + real-jar compile of every skeleton + template; no whitelist; do-not-own; token census; category-scope).
- **[gate] — nightly / pre-merge, on a quiet box:** B1/B2/B3/C1/C4 (the smoke gate itself becomes the regression: Mikaeus go/no-go, greedy-Thoracle package, Ghave-class value+drain, the negative-veto control) + C7 (a no-op negative-control driver reads null).
- **[eval] — skill-behavior, periodic:** A1/A2/A4/A5/B4 (novel-line authoring, OWN-noun scoping, altitude, iterate-loop convergence).
- **[harness] — infra invariants, asserted in the pipeline:** C6/C9/D1-D7 (worktree-pin, foreground-timeout, no-broad-kill, ledger-durability, staging-sweep, backend-resolves).
- **[process] — E1:** every claim carries its evidence; the suite embeds negative controls so a passing driver can't be a false positive.

The smoke gate (`smoke-gate.md`) is the seed of the [gate] tier — passing it is both the corpus greenlight AND the first regression baseline.
