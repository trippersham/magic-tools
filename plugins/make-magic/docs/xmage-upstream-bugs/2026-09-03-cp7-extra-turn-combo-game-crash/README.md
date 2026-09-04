# ComputerPlayer7 silent game death: extra-turn / turn-loop combo decks stop mid-play with no terminal event

**Summary:** In headless `ComputerPlayer7`-vs-`ComputerPlayer7` Commander games where one
seat pilots a deck built around an **extra-turn / turn-loop or flicker-loop combo package**,
the game thread **dies abruptly in the middle of ordinary play**. The last transcript line is
a perfectly normal event — a land drop, a point of combat damage, a life change, or a fresh
`Turn:` marker — and then output simply **stops**. There is no game-end, no winner, no draw,
no timeout, and no exception text anywhere in the transcript. The game never produces a result.

**Severity:** silent mid-game death with **no terminal event**. Unlike a hang, the process
does not spin — it stops. The game is unrecoverable and, from inside the transcript, gives no
cause. Affected matchups are simply lost work.

This is a **distinct failure mode** from the previously-filed
[CP7 optional-mana-ability livelock](../2026-08-29-cp7-optional-mana-ability-livelock/README.md)
(§7): that bug is an *unbounded loop* whose transcript grows forever at ~1900 lines/sec; this
bug is a *hard, fast stop* whose transcript is 2–20 KB and simply ends. Same environment
(stock 1.4.60, plain CP7 both seats), opposite signature.

---

## 1. Environment

- **Engine:** XMage `ComputerPlayer7` (the minimax "MA" AI), module `mage-player-ai-ma`
  (`org.mage:mage-player-ai-ma`).
- **XMage version:** **1.4.60** (upstream tag `xmage_1.4.60V3`; `magefree/mage`). Our build is
  a stock 1.4.60 reactor clone — there is **no** make-magic fork of the engine. We add only an
  out-of-tree headless runner (`org.makemagic.xmage.XMageBatch`) and a `mage.collectors`
  log-shadow; neither touches AI decision logic or the rules engine. Every affected game is on
  the **`_baseline_` arm**, which attaches **none** of our optional AI-driver seam — i.e.
  byte-identical stock CP7 in both seats.
- **Format / setup:** Commander (100-card singleton, 40 life, command zone), 1v1
  `CommanderDuel`, both seats CP7, real 7-card opening hands + mulligans
  (`GameOptions.testMode = false`). Single-threaded: `game.start()` runs the whole game on the
  calling (`main`) thread and returns only when the game ends.
- **Guards armed but not tripped:** a 150 s hard per-game wall-clock deadline
  (`MAX_GAME_WALLCLOCK_SECS`) and a 64 MiB transcript byte-cap (`MAX_TRANSCRIPT_BYTES`). **The
  deadline did not fire** (the thread dies well before 150 s) and **the byte-cap was not hit**
  (files are 2–20 KB). The death is fast and synchronous, not a slow spin.

## 2. The decks at the center

Both subject decks were independently flagged by our own adversarial deck review for
**infinite / extra-turn combo packages**. Enumerated from the actual deck lists in this repo:

### 2a. Jeskai — Narset, Enlightened Master
`plugins/make-magic/pipeline/pipeline/data/gauntlet/commander/casual/jeskai__narset-enlightened-master.dck`

Extra-turn / turn-loop / proliferate-loop machinery it actually contains:

- **Extra-turn spells:** `Time Stretch` (two extra turns), `Karn's Temporal Sundering`.
- **Turn / planeswalker-activation loop core:** `The Chain Veil` + `Teferi, Temporal Archmage`
  (untap-lands ultimate/ability) — the canonical Chain-Veil infinite-activation / infinite-mana
  engine; `Omniscience` (free-cast enabler that chains the noncreature suite, extra-turn spells
  included); `Narset, Enlightened Master` (commander: free-casts noncreature spells off the top).
- **Proliferate / loyalty snowball feeding the loop:** `Deepglow Skate` (doubles counters),
  `Inexorable Tide`, `Flux Channeler`, `Thrummingbird`, `All Will Be One`, `Ichormoon Gauntlet`,
  `Karn's Bastion`, `Gatewatch Beacon` — plus a dense planeswalker shell (`Teferi, Temporal
  Archmage`, `Teferi, Master of Time`, `Chandra, Torch of Defiance`, `Jace, the Mind Sculptor`,
  and ~15 more) that the Chain Veil / proliferate engine is built to loop.

> Note: our review's shorthand named "Enter the Infinite / lock"; the actual list does **not**
> run *Enter the Infinite* — the extra-turn payload is `Time Stretch` + `Karn's Temporal
> Sundering` on top of the Chain Veil / Teferi-Temporal-Archmage activation loop. Reported
> honestly so a maintainer reproduces the real list.

### 2b. Esper — Aminatou, the Fateshifter
`plugins/make-magic/pipeline/pipeline/data/gauntlet/commander/mid/esper__aminatou-the-fateshifter.dck`

Extra-turn / flicker-loop machinery it actually contains:

- **Extra-turn spells:** `Temporal Mastery`.
- **Flicker / blink loop core:** `Aminatou, the Fateshifter` (commander: `-1` flickers a
  permanent), `Displacer Kitten` (flicker permanents on each noncreature cast), `Felidar
  Guardian` (ETB flicker), `Brago, King Eternal` (combat-damage mass flicker), `Venser, the
  Sojourner`, `Peregrine Drake` (untap 5 lands → infinite mana with any flicker), `Archaeomancer`
  (ETB return an instant/sorcery — recurs `Temporal Mastery` → **infinite turns** with a flicker
  engine).
- **Same Chain-Veil turn/activation core as 2a:** `The Chain Veil` + `Teferi, Temporal Archmage`.
- **Proliferate feeders:** `Deepglow Skate`, `Ichormoon Gauntlet`, `Flux Channeler`,
  `Karn's Bastion`.

The two decks share the `The Chain Veil` + `Teferi, Temporal Archmage` loop core and the
`Deepglow Skate` / proliferate shell; they differ in the extra-turn payload (spell-based vs.
`Archaeomancer`-flicker-recursion).

## 3. Minimal repro

- **Subject seat:** either deck in §2 (plain CP7).
- **Opponent seat:** plain CP7 on any varied commander deck — the corpus reproduces the crash
  against **five different opponents** (`the-ur-dragon`, `ramos-dragon-engine`,
  `tovolar-the-midnight-scourge`, `prosper`, `xyris-the-writhing-storm`), so it is **not**
  opponent-specific.
- **Both seats:** plain `ComputerPlayer7`, **no** AI driver (`_baseline_` arm).
- **Format:** Commander 1v1 (`CommanderDuel`), real hands, `testMode = false`.

**Probabilistic, board-state triggered.** It is not deterministic from turn 1 and not tied to a
single card. In the captured corpus the death lands anywhere from **turn 9 to turn 34** — i.e.
once the game is deep enough that the AI has deployed and begun operating the turn/flicker
machinery. Re-running the same matchup does not always crash. Expect to run a batch.

## 4. Expected vs actual

**Expected:** every game reaches a terminal state — a win, a loss, or (rarely) a legitimate
draw — and the engine reports it. Even a genuinely unbreakable combo loop should surface *as*
a loop (the sibling livelock, §7), not as a silent stop.

**Actual:** `game.start()` stops advancing at an ordinary mid-play moment and never returns a
result. The transcript's final line is a normal `Land:` / `Damage:` / `Life:` / `Turn:` event.
No `XMAGEBATCH RESULT`, no `XMAGEBATCH TIMEOUT`, no `Game Result:`, no exception text.

## 5. Evidence

### 5a. Corpus signature (17 preserved transcripts, all `_baseline_`)

17 games from the baseline arm of one ~7.5 h / ~2,800-game run window
(9 Narset + 8 Aminatou). **Every one** has: `XMAGEBATCH RESULT` = 0, `XMAGEBATCH TIMEOUT` = 0,
exception/stacktrace lines = 0, and a final line that is an ordinary in-play event. File sizes
2–20 KB (byte-cap never approached). Per-file death table:

| # | subject | opponent | last turn | active seat at death | last transcript line | RESULT/TIMEOUT/exc | bytes |
|---|---------|----------|-----------|----------------------|----------------------|--------------------|-------|
| 1 | Narset | ur-dragon | 11 | PlayerA (subject) | `Turn: Turn 11 (Ai(1)-PlayerA)` + turnstart | 0 / 0 / 0 | 5,654 |
| 2 | Narset | tovolar | 9 | PlayerB (opp) | `Turn: Turn 9 (Ai(2)-PlayerB)` | 0 / 0 / 0 | 4,741 |
| 3 | Narset | tovolar | 9 | PlayerA (subject) | `Turn: Turn 9 (Ai(1)-PlayerA)` | 0 / 0 / 0 | 3,881 |
| 4 | Narset | tovolar | 12 | PlayerB (opp) | `Turn: Turn 12 (Ai(2)-PlayerB)` | 0 / 0 / 0 | 5,269 |
| 5 | Narset | ur-dragon | 14 | PlayerA (subject) | `Life: Ai(1)-PlayerA 29 > 27` (Chain Veil dmg) | 0 / 0 / 0 | 7,760 |
| 6 | Narset | ur-dragon | 14 | PlayerA (subject) | turn-14 turnstart HANDLOG | 0 / 0 / 0 | 6,205 |
| 7 | Narset | ramos | 19 | PlayerB (opp) | turn-19 turnstart HANDLOG | 0 / 0 / 0 | 12,118 |
| 8 | Narset | ramos | 22 | PlayerA (subject) | `Land: Ai(1)-PlayerA played a land` | 0 / 0 / 0 | 10,451 |
| 9 | Narset | prosper | 21 | PlayerB (opp) | `Life: Ai(1)-PlayerA 11 > 9` (Prosper combat) | 0 / 0 / 0 | 13,761 |
| 10 | Aminatou | ramos | 11 | PlayerB (opp) | `Life: Ai(2)-PlayerB 31 > 30` (Baleful Strix combat) | 0 / 0 / 0 | 5,126 |
| 11 | Aminatou | ur-dragon | 13 | PlayerB (opp) | `Life: Ai(2)-PlayerB 40 > 38` (Painful Truths) | 0 / 0 / 0 | 6,681 |
| 12 | Aminatou | prosper | 13 | PlayerB (opp) | turn-13 turnstart HANDLOG | 0 / 0 / 0 | 7,324 |
| 13 | Aminatou | tovolar | 15 | PlayerA (subject) | `Life: Ai(1)-PlayerA 20 > 21` | 0 / 0 / 0 | 8,248 |
| 14 | Aminatou | ramos | 22 | PlayerA (subject) | turn-22 turnstart HANDLOG | 0 / 0 / 0 | 14,066 |
| 15 | Aminatou | xyris | 24 | PlayerA (subject) | `Life: Ai(1)-PlayerA 6 > 5` + turn-24 turnstart | 0 / 0 / 0 | 16,044 |
| 16 | Aminatou | ur-dragon | 33/34 | PlayerA (subject) | **`HANDLOG event=gameend` with both players alive, no RESULT** | 0 / 0 / 0 | 18,604 |
| 17 | Aminatou | prosper | 34 | PlayerB (opp) | `Life: Ai(1)-PlayerA 31 > 30` (Mirkwood Bats combat) | 0 / 0 / 0 | 18,039 |

**What the corpus shows honestly:**

- **No single common card or last action.** The last card to act before the stop is different
  almost every game — `Chaos Warp`, `Sol Ring`, a combat creature, `Deepglow Skate`,
  `Teferi, Master of Time`, or one of the *opponent's* spells. There is **no** "card X always
  precedes the death." The death lands on a completely ordinary line.
- **Not phase-locked.** Roughly 9 of 17 stop during the **combo pilot's own turn** and ~8 stop
  on the **opponent's turn or mid-combat**. It leans slightly toward the subject's turn but is
  not confined to it.
- **The one real invariant:** one of the two seats is always a §2 extra-turn / turn-loop /
  flicker combo deck, on the plain `_baseline_` CP7 arm, and the transcript ends with no
  terminal event. Turn of death spans 9–34 across five distinct opponents.
- **One sub-variant worth flagging (#16).** This game emits a collector `HANDLOG
  event=gameend` at turn 34 **while both players are still alive** (`UR_life=8`, `opp_life=27`)
  — an end event with no lethal state and *still* no `XMAGEBATCH RESULT`. This is consistent
  with the game aborting during/after an abnormal end rather than resolving a real win.

### 5b. Trimmed transcripts — `evidence/`

Three representative tails (whole files are small; each excerpt shows the abrupt stop and the
zero-RESULT/zero-TIMEOUT/zero-exception counts in its header):

- [`evidence/narset-vs-prosper-baseline_8-tail.log`](evidence/narset-vs-prosper-baseline_8-tail.log)
  — dies mid-combat (turn 21): last line is `Life: Ai(1)-PlayerA 11 > 9`.
- [`evidence/aminatou-vs-ur-dragon-baseline_0-tail.log`](evidence/aminatou-vs-ur-dragon-baseline_0-tail.log)
  — the #16 anomaly: `HANDLOG event=gameend` at turn 34 with both players alive, no RESULT.
- [`evidence/narset-vs-ur-dragon-baseline_8-tail.log`](evidence/narset-vs-ur-dragon-baseline_8-tail.log)
  — dies at a turn boundary (turn 11): last lines are `Turn: Turn 11` then its turnstart.

## 6. Root-cause hypothesis (for XMage maintainers)

We are precise here about **known** vs **inferred**.

**Known (from the transcript alone):**

- The transcript is `System.out` **redirected into the per-game log file**. In our runner, the
  terminal lines (`XMAGEBATCH RESULT`, `XMAGEBATCH TIMEOUT`, `Game Result: …`) are printed
  **only after `game.start()` returns**. If `game.start()` **throws**, control jumps straight to
  the `finally` block that flushes + closes the transcript — so the file ends *exactly* at the
  last event flushed before the throw, and **none** of the terminal lines are written. That is
  precisely the observed signature (ordinary last line, zero RESULT/TIMEOUT).
- The 150 s deadline did **not** fire and the 64 MiB cap was **not** hit → the game thread
  terminated **fast and synchronously**, not by spinning. So this is a thrown throwable during
  play, not a hang.

**Inferred (cannot be confirmed from the transcript, which carries no stack trace):** an
**uncaught throwable in the game thread** during the resolution of the extra-turn / flicker
machinery. The most plausible candidates, given the decks:

- A `StackOverflowError` from **unbounded recursion in extra-turn / flicker / re-cast stack
  handling** (e.g. `Archaeomancer` + a flicker engine recurring `Temporal Mastery`, or the
  `Chain Veil` + `Teferi, Temporal Archmage` activation loop, or `Displacer Kitten` /
  `Peregrine Drake` flicker chains) — these lines build genuinely deep, self-referential
  resolution stacks.
- Or an ordinary `RuntimeException` / `NullPointerException` thrown while applying one of those
  replacement/turn effects.

For maintainers, the fruitful place to look is the **extra-turn insertion path** (how
`TurnMod` / additional-turn effects are pushed and consumed) and the **flicker/blink ETB
re-entrancy** under an AI that is willing to assemble the loop but not to stop it — i.e. the
same class of state the sibling livelock exercises, but here it throws instead of spinning.

## 7. Relationship to the sibling report (2026-08-29 livelock)

Same environment (stock XMage 1.4.60, plain CP7 both seats, `mage-player-ai-ma`, `CommanderDuel`,
headless, `_baseline_` arm), and both are triggered by the AI operating a combo the base rules
say it may decline. They are **different failure modes**:

| | 2026-08-29 optional-mana livelock | 2026-09-03 extra-turn crash (this report) |
|---|---|---|
| Symptom | unbounded loop, never terminates | fast mid-game stop |
| Transcript | grows forever (~1900 lines/s, 84 MB) | 2–20 KB, then simply ends |
| Deadline | eventually trips (or would) | never trips (thread already dead) |
| Terminal line | none (still running) | none (thread threw before RESULT) |
| Likely cause | AI activation heuristic + SBA-timing gap | uncaught throwable in extra-turn/flicker resolution |

Both share the property that **the harness receives no legitimate result**, but for opposite
reasons — one because the game never ends, one because it ends violently.

## 8. Why our harness records no error line in the transcript (worker protocol)

For reference; see
`plugins/make-magic/pipeline/pipeline/sim/java/xmage-dist/src/main/java/org/makemagic/xmage/XMageBatch.java`
(`runWorker` / `runWorkerGame`, the `try { game.start(starterId) } finally { deadline.cancel() }`,
and the `GameDeadline` watcher).

- The **worker→governor protocol** (`READY` / `RESULT` / `ERROR`) is written to the process's
  **real stdout**, captured *before* per-game redirection. The **per-game transcript** is a
  *separate* stream: `System.out`/`System.err` are redirected into the log file for the duration
  of the game and restored in `finally`.
- `runWorkerGame` runs `game.start()` synchronously and, on normal return, emits `RESULT` on
  the protocol pipe. `runWorker` wraps that call in `catch (Exception ex)` → on a thrown game it
  emits `ERROR {id, exc}` on the pipe and prints the stack trace to the **real stderr** — **not**
  into the transcript. So a thrown game leaves the transcript ending mid-play with **no** stack
  trace, exactly as observed.
- Two throwable sub-paths, and **the transcript cannot distinguish them** (the signal is on the
  protocol pipe / governor log, not the preserved file):
  1. A `java.lang.Exception` subclass → caught by `runWorker` → `ERROR` on the pipe. The
     governor's retry-counter treats `ERROR` as **retryable** → the task is requeued and
     **attempt-capped**, then quarantined.
  2. A `java.lang.Error` (e.g. `StackOverflowError`, `OutOfMemoryError`) → **not** caught (the
     clause is `Exception`, not `Throwable`) → it escapes `runWorker` → the **worker JVM dies**.
     The governor observes the worker death, the in-flight task is lost/requeued, and it is
     likewise attempt-capped.
- What we **know**: the tasks were **requeued and attempt-capped** (governor side), and the
  transcripts carry **no** `RESULT`/`TIMEOUT`/exception text. What we **infer**: an uncaught
  throwable in the game thread (§6), most likely a `StackOverflowError` from extra-turn/flicker
  recursion, which would take sub-path (2).

## 9. Our mitigation (context only — not an upstream ask)

- These games are recorded as **failures**, never as fabricated results: a task that never
  emits a decisive `RESULT` is excluded from the W/L/D denominator — it is **not** scored as a
  draw and **not** as a loss for either seat.
- The governor **attempt-caps** requeued `ERROR` / lost tasks and then **quarantines** the
  offending matchups, which bounds the blast radius (≈17 games over ~2,800 in the window).
- Follow-up on our side is to **catch `Throwable`** (not just `Exception`) around `game.start()`
  so an `Error` sub-path also produces a clean non-decisive `ERROR` line instead of killing the
  worker — an observability fix on our seam, orthogonal to the engine defect reported here.
</content>
