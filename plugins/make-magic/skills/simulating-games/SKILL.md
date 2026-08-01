---
name: simulating-games
description: >
  Empirically test MTG decks by running real headless Forge games via the `simulate` CLI.
  TRIGGER when: user asks to "test how a deck plays", "does deck X actually work",
  "confirm this swap", "validate this change", "simulate deck X", "run a gauntlet",
  "A/B these two lists", "what's the win-rate", "play deck X against deck Y", or wants
  empirical (a-posteriori) confirmation of a deck's behavior. Also trigger to close the
  loop on a building-decks proposal — "prove that swap is actually better". SKIP for
  a-priori strategy reasoning, card fit, or Quadrant diagnosis — those are building-decks
  tasks; this skill runs the games that CONFIRM them.
user-invocable: true
---

# Simulating Games

Empirically test decks with real headless MTG Forge games via the `simulate` CLI: a
candidate deck plays a **gauntlet** of opponents, and you read the resulting
**win-rate ± CI** plus a **telemetry profile** (kill-turn, win-margin, wincon mix,
ramp curve) to confirm how the deck actually plays.

<primary-constraint>
**A Forge win-rate is directional evidence, not ground truth. NEVER report a bare
win-rate — ALWAYS report it with its 95% CI, and frame the verdict correctly.**

Why: Forge's opponents are driven by a **rule-based heuristic AI**, not a skilled
pilot. It plays fair beatdown and midrange competently but is **weak at control,
combo, stax, and intricate lines** — so an absolute win-rate is a measurement of *this
AI on this gauntlet*, never a ladder rating. The asymmetry is the load-bearing part:

- A deck that **CANNOT** beat the gauntlet is **genuinely flawed** — if it can't even
  clear a rule-based bot, it will not clear real opponents. Failure is trustworthy.
- A deck that **BEATS** the gauntlet is only confirmed **functional** (it does the
  thing it's built to do), **NOT confirmed good** — the bot didn't punish it. Success
  is weak evidence.
- Combo/control candidates especially: a low Forge win-rate may be the AI failing to
  pilot them, not the deck being bad. Say so; don't condemn the deck on the number.

So: report `win-rate [95% CI lo–hi]`, and rank on the metric that matches the QUESTION
(fastest clock → kill-turn; most resilient → win-margin/spread; "did the plan fire?" →
wincon mix), not on win-rate alone. A tight verdict needs a real sample — see
[Sample-size honesty](#sample-size-honesty).
</primary-constraint>

<red-flags>
If you catch yourself about to:
- **Report "62% win-rate" with no CI** — STOP. `62% [95% CI 44–77%]` from 30 games is
  a shrug, not a verdict. The interval IS the finding.
- **Declare a deck "good" because it won the gauntlet** — STOP. It's confirmed
  *functional*, not good. The bot is beatable; real pods aren't.
- **Condemn a control/combo deck for a low win-rate** — STOP. Suspect the AI's piloting
  before the deck's construction. Read the telemetry, not just the tally.
- **Draw a conclusion from ~20 games** — STOP. That's ±~20 points; it's directional.
  Say "directional," or run more games.
- **Invent a flag** (`--iterations`, `--vs`, `--verbose`, `--json`) — STOP. Only the
  flags in the [router](#operation-router) exist. Grep `pipeline/sim/run.py` if unsure.
</red-flags>

## The CLI: `simulate`

Every operation goes through one wrapper — a PEP-723 `uv run --script` entry that
forwards to the `pipeline.sim.run` dispatcher:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/simulate <verb> [args...]
```

**Deck arguments** (`<A>` / `<B>` / `<name>`) resolve two ways, no record IDs:
- a **`.dck` file path** (arg ends in `.dck` or is an existing file) — read off disk; OR
- an **Airtable deck NAME** — resolved via the collection store and rendered to `.dck`.

The collection backend auto-resolves (override with `MAKE_MAGIC_BACKEND=local|airtable`);
Forge/Java auto-resolve (override with `MAKE_MAGIC_FORGE_HOME` + `MAKE_MAGIC_JAVA`).

**Check the environment first.** `doctor` and `gauntlet show` run offline (no game JVM);
`match` / `deck` / `ab` spawn real Forge, so confirm Forge is reachable before a long run:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/simulate doctor
```
It prints the runtime-derived **safe pool size** (concurrent JVMs), a free-RAM/disk
snapshot, and whether Forge is available (with an actionable "how to enable" if not). If
`doctor` reports Forge NOT AVAILABLE, stop and surface its message — do not attempt a run.

## Prerequisites

- **uv** — the wrapper runs via `uv run --script` (PEP 723 inline metadata).
- **Java + Forge** — auto-fetched at runtime and reused if already present; verify with
  `doctor`. A game verb without a resolvable Forge exits with a clean `error:`, not a
  traceback.
- **A backend** (only when addressing decks by NAME rather than `.dck` path) — local YAML
  or Airtable, same as the other make-magic skills. `--gauntlet mine|both` also needs the
  store (it pulls your own decks); `--gauntlet curated` never touches it.

## Operation Router

| User intent | Verb | Command |
|-------------|------|---------|
| "How does deck X play?" / "test deck X" / "run the gauntlet" | `deck` | [Evaluate a deck](#1-evaluate-a-deck-vs-the-gauntlet) |
| "Is variant B better than A?" / "confirm this swap" / "validate this change" | `ab` | [A/B a change](#2-ab-a-change) |
| "Play X against Y" / "X vs Y head-to-head" | `match` | [Head-to-head](#3-head-to-head) |
| "Check Forge / how many games can I run" | `doctor` | [above](#the-cli-simulate) |
| "What's in the gauntlet?" / "show the field" | `gauntlet show` | [Inspect the field](#4-inspect-the-gauntlet) |
| "Why did X lose to Y?" / "pull up that game" / "what happened in game N" | `log` | [Replay a past game](#5-replay-a-past-game) |

Every verb takes `--format constructed|commander` (default `constructed`). Commander is
the make-magic house format — pass `--format commander` for Commander decks. **Commander
games run roughly 8× slower** than constructed (bigger board states, longer games), so
size the run accordingly.

---

## 1. Evaluate a deck vs the gauntlet

"How does deck X actually play?" — the standalone empirical read.

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/simulate deck "<Airtable deck name>" \
  --gauntlet both --format commander --games 30
```
- `--gauntlet curated|mine|both|<bundle>` — `curated` is the small default bundled opponent
  set (offline, no store); `mine` is your own decks; `both` merges them (curated first);
  `<bundle>` is a packaged named tier set. Default `curated`. The shipped named bundle is
  **`guilds`** — the 10 two-color guilds × weak/mid/strong, 30 authentic 40-card constructed
  decks. It's a far more discriminating instrument than the 5-deck default (a candidate's
  win-rate *across the tiers* localizes its power level), at the cost of more games per run.
  `simulate gauntlet show --source guilds` lists it.
- `--games N` — games **per opponent** (default 4 — fine for a smoke, far too few for a
  verdict; see [Sample-size honesty](#sample-size-honesty)).
- `--seed N` — RNG seed (default 42). `--force` bypasses the matchup cache and re-runs
  every matchup fresh (otherwise unchanged matchups are served from cache).

Output: overall `win-rate [95% CI]` with the W-L-D tally, a **per-opponent** breakdown
(each with its own CI, and a `[cached]` marker), and the **telemetry profile**. Read the
profile in quadrant-theory language against the deck's building-decks intent — does the
measured behavior match the a-priori plan? See
[Interpreting results](#interpreting-results).

## 2. A/B a change

"Is this swap actually an improvement?" — the a-posteriori half of a building-decks
proposal. Run both variants over the **same** gauntlet and compare the deltas.

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/simulate ab "<A>" "<variant B .dck or name>" \
  --gauntlet curated --games 30
```
`<A>` and `<B>` are each a deck name or a `.dck` path — so "A = the live deck, B = the
same deck with one card swapped" is a natural A/B: export B to a `.dck`, or save it as a
named deck, then diff. Output: each variant's `win-rate [95% CI]`, the **Δ win-rate
(A − B)** in points, the **stronger** variant (or a tie), and **per-metric deltas** across
the telemetry (kill-turn, win-margin, ramp curve).

**Whether the delta is real depends on the CIs.** If the two variants' CIs overlap
heavily, the "stronger" label is noise — say the change is **not distinguishable at this
sample size** and offer to run more games. A +4-point delta on 30 games is inside the
error bars; the same delta on 300 games is a signal.

## 3. Head-to-head

"Play X against Y directly" — a single matchup, win tally only (no gauntlet, no CI
aggregation).

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/simulate match "<A>" "<B>" -n 30
```
- `-n N` — number of games (default 4). `-s / --seed N` — RNG seed (default 42).
- `--format constructed|commander`.

Output: `A: W wins   B: W wins   draws: D`. Use this for a targeted "does X beat Y?"
question; use `deck` when you want a win-rate against a *field*.

## 4. Inspect the gauntlet

See the field a candidate is measured against (offline — no Forge, no store, no network):

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/simulate gauntlet show --format commander
```
Lists the packaged opponents for that format. `--source curated` (default) or a named
bundle (`--source guilds`). (`mine` / `both` fields are user-specific and not enumerated by
this verb — read them via the collection skills.)

## 5. Replay a past game

"Why did X lose to Y?" / "pull up that specific game" — a **forensic** deep-dive into an
already-simulated matchup, with **no re-run**. Every `deck` / `ab` run persists the full
verbose Forge log of each game to DuckDB, so a past game is replayable exactly as it
happened. This matters because **Forge's seed is not reproducible** — re-running the same
matchup yields a *different* game, so the only faithful record is the one captured at run
time.

```bash
# List the stored matchup(s) for a deck pair + each game's index/outcome:
${CLAUDE_PLUGIN_ROOT}/scripts/simulate log "<A>" "<B>"
# Print one game's full turn-by-turn log:
${CLAUDE_PLUGIN_ROOT}/scripts/simulate log "<A>" "<B>" --game 0
```
- `<A>` / `<B>` are the same deck references (name or `.dck`) used in the run — they're
  matched by **content hash**, so an edited deck won't match its old logs (by design).
- **A `.dck` path is fully offline** (read off disk — the exact text that was simulated). **A
  bareword is an Airtable name resolved by a LIVE store lookup** to the deck's *current* text;
  if the deck was edited since the run, its hash no longer matches and the logs won't be
  found. Prefer the `.dck` path that was simulated for reliable forensics.
- `--game N` prints game `N` (0-based); omit it to list the games first. Narrow ambiguous
  matches (multiple seeds / game-counts / Forge versions) with `--seed` / `--games` /
  `--format` / `--forge`; the listing shows an 8-char matchup-key prefix to tell runs apart.
- No Forge needed: reads straight from DuckDB. Use it to explain an upset turn-by-turn,
  confirm a wincon, or check whether a loss was mana screw vs. getting outclassed.

---

## Interpreting results

### Sample-size honesty

Win-rate is a proportion; its CI shrinks with √N. Budget games to the question:

| Games (candidate total) | 95% CI half-width (near 50%) | Use for |
|-------------------------|------------------------------|---------|
| ~20 | ≈ ±20 pts | a smoke test — **directional only** |
| ~100 | ≈ ±10 pts | a rough read; separates "works" from "doesn't" |
| **~300 per finalist** | ≈ ±6 pts | **a tight verdict** — the number to quote for a decision |

Note `deck`/`ab` take `--games` **per opponent**: total games ≈ `--games × opponents`.
Against an 8-deck gauntlet, `--games 30` is ~240 candidate games — near the "tight
verdict" band; `--games 4` (the default) is a smoke. Always translate `--games` into a
*total* before you characterize the certainty, and **quote the CI, not the point
estimate**, whenever you state a verdict.

### The telemetry profile — read it in quadrant language

The profile pools every game and reports how the deck *won or lost*, not just how often.
Read each field against the deck's building-decks **Strategy / Focus Otags** (its a-priori
plan) and ask "does the measured behavior match the intent?":

- **kill-turn** (avg / median) — the deck's real clock. An aggro deck's plan says "kill
  by turn ~6"; if the median kill-turn is 9, the a-priori plan didn't materialize.
- **win-margin** (winner's remaining life) — resilience / how close the wins were. A
  control deck should win at high life; a razor-thin margin means it's racing, not
  controlling.
- **wincon mix** (`{combat|burn|mill|other: count}`) — did the *intended* win condition
  fire? A "burn" deck whose wins are mostly `combat` is winning off-plan.
- **mean ramp curve** (mean lands-in-play per turn) — did the Development-quadrant ramp
  plan actually accelerate mana, or is the curve flat?

A field is `-` / empty when no game supplied that signal (e.g. all draws, or an
unclassifiable wincon). Rank finalists on the metric that answers the QUESTION — fastest
clock, most resilient, or "the plan actually fired" — never win-rate in isolation.

<reference file="interpreting-results.md">
Read references/interpreting-results.md for the full telemetry-field reference (exact
semantics of every SimResult / TelemetryProfile field and its source), the Wilson-CI
sample-size table, the AI-blind-spots catalog (which archetypes Forge under- and
over-rates), and the caching / `--force` / seed mechanics.
</reference>

## Relationship to building-decks

**building-decks is a-priori; simulating-games is a-posteriori.** building-decks reasons
about card fit and deck balance *before* a game is played — it proposes a swap on
Strategy/Focus-Otags/Quadrant reasoning. This skill **closes that loop**: it runs real
Forge games to CONFIRM (or refute) the proposal empirically.

The intended workflow:
1. **building-decks** proposes a change (e.g. "cut X for Y — better Losing-quadrant answer").
2. **simulating-games** confirms it: `simulate ab "<current>" "<with the swap>"` over a
   gauntlet, then read the Δ win-rate ± CI **and** the telemetry deltas.
3. Feed the result back: if the empirical behavior matches the a-priori intent (and the CI
   supports it), the swap is confirmed; if the telemetry diverges from the plan (or the CIs
   overlap), report that the a-priori reasoning did **not** survive contact — the deck's
   measured reality beats the theory.

Standalone "how does deck X play?" starts directly at `simulate deck`. Either way, the
constraint holds: **a win is confirmation the deck is functional; only failure is
trustworthy as a verdict.**
