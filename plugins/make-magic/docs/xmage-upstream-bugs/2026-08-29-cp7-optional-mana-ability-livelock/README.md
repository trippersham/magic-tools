# ComputerPlayer7 livelock: optional no-benefit mana ability re-activated forever (game never terminates)

**Summary:** In a headless `ComputerPlayer7`-vs-`ComputerPlayer7` Commander game, the AI
enters an unbreakable intra-turn loop by compulsively re-activating an **optional**,
no-benefit activated **mana** ability whose cost includes a life payment. The engine
never terminates the game — one turn runs forever, emitting the same two log lines
(~1900 lines/sec) indefinitely. This is **not** a rules-mandated loop (under the
Comprehensive Rules it would neither be a draw nor cause anyone to lose — the game simply
continues), and it is **not** an artifact of our harness: it reproduces with a plain,
unmodified CP7 in both seats.

**Severity:** game-non-terminating (livelock / effective hang). A single affected
matchup wedges a worker process until an external wall-clock kill.

---

## 1. Environment

- **Engine:** XMage `ComputerPlayer7` (the minimax "MA" AI), module `mage-player-ai-ma`
  (`org.mage:mage-player-ai-ma`).
- **XMage version:** **1.4.60** (upstream tag `xmage_1.4.60V3`; `magefree/mage`). Our build
  is a stock 1.4.60 reactor clone — there is **no** make-magic fork of the engine. We add
  only an out-of-tree headless runner (`org.makemagic.xmage.XMageBatch`) and a
  `mage.collectors` log-shadow; neither touches AI decision logic. The bug reproduces on the
  **`_baseline_` arm**, which attaches none of our optional AI-driver seam (see §3), i.e.
  byte-identical stock CP7 behavior.
  - Pinned in `plugins/make-magic/pipeline/pipeline/sim/java/xmage-dist/pom.xml`
    (`<xmage.version>1.4.60</xmage.version>`, jar `Implementation-Version: 1.4.60`).
- **Format / setup:** Commander (100-card singleton, 40 life, command zone), 1v1
  `CommanderDuel`, both seats CP7, real 7-card opening hands + mulligans
  (`GameOptions.testMode = false`). Single-threaded: `game.start()` runs the whole game on
  the calling (`main`) thread and returns only when the game ends.

## 2. The card at the center: Blood Celebrant

Oracle text (verified via Scryfall `cards/named?exact=Blood Celebrant`, set `lgn` #61):

```
Blood Celebrant        {B}
Creature — Human Cleric    1/1
{B}, Pay 1 life: Add one mana of any color.
```

This is a **single optional activated mana ability** whose cost is `{B}` plus **paying 1
life**. It does **not** deal damage and does **not** gain life on its own. The only "sink"
for repeatedly activating it is having something to spend the produced mana on; with no such
sink, re-activation is pure self-inflicted life loss with no benefit.

> Note on log rendering: XMage's game log renders the 1-life payment as
> `Damage: Blood Celebrant [id] deals 1 damage to <player>`. That is the harness's
> rendering of the life-payment loss, not combat/ability damage dealt by the card. The
> decisive fact is that the life-draining half is Blood Celebrant's **optional** activated
> mana ability.

## 3. Minimal repro

- **Deck A (subject):** cEDH mono-black `K'rrik, Son of Yawgmoth` (a life-payment /
  aristocrats list that runs Blood Celebrant).
- **Deck B (opponent):** Rakdos `Prosper, Tome-Bound` (any opponent works; not
  Prosper-specific).
- **Both seats:** plain `ComputerPlayer7`. **No** AI driver attached (`_baseline_` arm).
- **Format:** Commander 1v1 (`CommanderDuel`), real hands.

**Not deterministic from turn 1.** It is a **board-state** trigger: it fires once the AI
controls Blood Celebrant (plus, for the *unbreakable* variant, a second life-gain source —
see §5) and the search decides to activate the mana ability. In the captured corpus it
first manifested mid-game (turn 7 in the runaway; the near-miss sibling below reaches the
Blood Celebrant burst on turn 19).

Affected class of decks (all cEDH mono-black life-payment / aristocrats commanders that run
Blood Celebrant or equivalent optional life-cost mana dorks):

- K'rrik, Son of Yawgmoth
- Ketramose, the New Dawn
- Yawgmoth, Thran Physician

## 4. Expected vs actual

**Expected (per the Comprehensive Rules):**

- **CR 104.4b** — a game is a draw from a loop **only** when it is a loop of *mandatory*
  actions with no way to stop; loops that contain an **optional** action are explicitly
  excluded from the mandatory-loop draw rule.
- **CR 720** (executing loops / loop shortcuts) — when a loop is being sustained by a
  player's **optional** action, that player must simply **stop** taking the action; the loop
  is broken by choice, not by a draw.
- **CR 704.3 / 704.5a** — state-based actions are checked whenever a player *would* receive
  priority (704.3); a player with 0 or less life **loses** the game (704.5a, an SBA).

Therefore the real-rules outcome is: activating Blood Celebrant is optional, so this loop
**never forms** — no draw, no loss, the game continues. If a player ever *did* let their
life reach 0, SBA 704.5a would make them lose the next time priority is about to be received.

**Actual (XMage `ComputerPlayer7`):**

1. CP7 chooses to re-activate Blood Celebrant's optional mana ability with **no mana sink /
   no benefit**, forever. (Primary defect — an AI heuristic problem.)
2. Secondarily, in the two-Celebrant *unbreakable* variant, XMage lets the life total bounce
   `1 → 0 → 1` within ability resolution **without an intervening priority / SBA window**, so
   the 0-life loss (704.5a) never fires and nothing breaks the loop. (Secondary defect — an
   SBA-timing gap.)

The loop repeats forever at ~1900 lines/sec, emitting nothing but the two line types:

```
Damage: Blood Celebrant [ee9] deals 1 damage to Ai(1)-PlayerA.
Life: Ai(1)-PlayerA 1 > 0
Damage: Blood Celebrant [6d1] deals 1 damage to Ai(1)-PlayerA.
Life: Ai(1)-PlayerA 0 > 1
...
```

Two Blood Celebrant permanents (`[ee9]`, `[6d1]`) oscillate one player's life
`1 → 0 → 1 → 0`. (The `+1` half comes from a second aristocrats life-gain effect in the deck;
the `-1` half is Blood Celebrant's optional life payment.)

## 5. Evidence

### 5a. The smoking-gun transcript (statistics; raw 84 MB file was deleted in disk cleanup)

- **File:** `staging/.../s_commander_cedh_mono-black__k-rrik-son-of-yawgmoth.dck.txt_o_rakdos__prosper.txt_baseline_8.log`
- **Size:** 84,882,709 bytes / 1,707,342 lines.
- **`XMAGEBATCH RESULT` lines: 0** — the game never terminated (our runner emits `RESULT`
  only after `game.start()` returns; it never returned).
- **Last `Turn:` marker at line 33** (`Turn: Turn 7`). Lines 34 – 1,707,342 are all one
  un-terminating turn 7.
- A sampled 100,000-line tail window was **100%** the two loop line types:
  57,144 `Damage: Blood Celebrant` + 42,856 `Life:` lines, and nothing else.
- **Arm = `_baseline_`** — plain CP7 with **no** make-magic driver attached. This is the
  proof that the defect is in the base engine, not in our optional AI seam.

### 5b. Surviving near-miss sibling (same matchup, same arm) — `evidence/`

The raw runaway was deleted, but a smaller sibling from the **same K'rrik-vs-Prosper
matchup on the same `_baseline_` arm** survives and shows the identical mechanism. See
[`evidence/sibling-k-rrik-vs-prosper-baseline_10-excerpt.log`](evidence/sibling-k-rrik-vs-prosper-baseline_10-excerpt.log)
(source: `..._o_rakdos__prosper.txt_baseline_10.log`, 392 lines, terminated normally,
`winner=PlayerB endTurn=34`).

This game has **one** Blood Celebrant on the battlefield (permanent `[8ba]`), so the optional
life-payment ability drains **monotonically** instead of oscillating — the AI activates it
over and over with no mana sink, walking its own life down one point at a time:

```
Damage: Blood Celebrant [8ba] deals 1 damage to Ai(1)-PlayerA.
Life: Life: Ai(1)-PlayerA 33 > 32
Damage: Blood Celebrant [8ba] deals 1 damage to Ai(1)-PlayerA.
Life: Life: Ai(1)-PlayerA 32 > 31
Damage: Blood Celebrant [8ba] deals 1 damage to Ai(1)-PlayerA.
Life: Life: Ai(1)-PlayerA 31 > 30
... (continues down)
```

The only difference between this **terminating near-miss** and the **non-terminating
runaway** is the number of life sources: with one Celebrant and no life-gain the drain
reaches a lethal SBA and the game ends; with **two** Celebrants plus a second aristocrats
life-gain source the same optional-activation heuristic produces a closed `1 → 0 → 1` cycle
that never resolves. Same root cause, two board states.

## 6. Root-cause hypothesis (for XMage maintainers)

Two distinct defects compound:

1. **Primary — AI activation heuristic (`mage-player-ai-ma` / `ComputerPlayer7`).**
   Somewhere in CP7's activated-ability enumeration/scoring, the search treats activating
   Blood Celebrant's mana ability as (at worst) neutral and re-selects it repeatedly. An
   optional **mana** ability should not be activated when there is **no mana sink** — i.e.
   when the produced mana cannot be, or is not being, spent to advance the game — especially
   when its cost is a strictly-negative life payment. The heuristic needs to decline a
   no-sink activation (net life-negative, zero benefit).

2. **Secondary — SBA / priority timing gap.** The engine allows the controller's life to
   pass through 0 (`1 → 0 → 1`) inside a chain of mana-ability activations/resolutions
   **without** a state-based-action check firing at a point where a player would receive
   priority (CR 704.3). If SBA 704.5a were evaluated at the correct window, a life total that
   dips to 0 would end the game even in the two-source oscillation — capping the loop even if
   the AI heuristic (defect 1) is not fixed.

## 7. Suggested fix directions

- **AI guard (addresses defect 1):** in CP7's mana-ability / activated-ability selection,
  reject activating a mana ability that (a) has a non-trivial cost (notably a life payment or
  other permanent resource loss) **and** (b) has no consumer for the produced mana in the
  current line of play. More generally, guard against re-selecting an activation whose only
  net effect is the AI's own resource loss.
- **Loop / SBA re-check (addresses defect 2):** ensure SBAs (CR 704) are evaluated at a
  priority-receipt boundary between successive optional mana-ability activations, so a
  life-to-0 transition triggers 704.5a rather than being masked by an immediate rebound. A
  cheap loop-detector on identical repeated no-progress activations within a single turn
  would also break the livelock defensively.

## 8. Our mitigation (context only — not an upstream ask)

On our side (the make-magic sim harness) we already:

- **Quarantine** the affected cEDH mono-black life-payment / aristocrats commanders so they
  do not wedge corpus runs.
- Are adding a **hard per-game wall-clock deadline** that marks such a game
  **non-decisive / excluded** — explicitly **NOT** scored as a draw and **NOT** as a loss —
  because per the CR the position is neither.

Why existing caps don't catch it (for reference; see
`plugins/make-magic/pipeline/pipeline/sim/java/xmage/src/org/makemagic/xmage/XMageBatch.java`):

- `setTurnCap()` → `GameOptions.stopOnTurn` is only checked at the **top of each turn**
  (`stopAtStep = UNTAP`). This loop never ends turn 7, so the turn cap is never reached.
- `setMaxThinkTimeSecs` is a **per-AI-decision** budget; the loop is a chain of mandatory
  damage/SBA-adjacent events between decisions, not one long think, so the per-decision timer
  never trips.
- `game.start()` runs **synchronously** and emits our `RESULT` line only after it returns; a
  non-returning game emits nothing, which is exactly the `0 × XMAGEBATCH RESULT` signature in
  §5a.
