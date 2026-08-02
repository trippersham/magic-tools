# Interpreting Forge Simulation Results

Depth reference for the `simulating-games` skill. Read this when you need the exact
meaning of a result field, the sample-size math behind a CI claim, the archetypes Forge's
AI mis-rates, or the caching/seed mechanics that shape a run.

---

## 1. Result field reference

The `simulate deck` / `ab` output is a rendering of `SimResult` (per candidate) plus a
`TelemetryProfile` (pooled over all its games). Every field below comes straight from
`pipeline/sim/core.py` and `pipeline/sim/telemetry.py` — nothing here is invented.

### SimResult (the win-rate layer)

| Field | Meaning |
|-------|---------|
| `candidate` / `gauntlet_source` / `fmt` | Which deck, which field (`curated`/`mine`/`both`), which format. |
| `win_rate` | Overall proportion of candidate wins over ALL games (draws are not wins). |
| `win_rate_ci` | **Wilson** 95% CI on `win_rate`. This is the interval you quote — never the bare rate. |
| `wins` / `losses` / `draws` / `total_games` | The raw tally the rate is computed from. |
| `per_opponent` | One `OpponentResult` per gauntlet deck (see below) — the matchup breakdown. |
| `cached_matchups` / `fresh_matchups` | How many opponents were served from cache vs run fresh. `fresh == 0` means a fully cached re-run (identical to last time). |
| `profile` | The pooled `TelemetryProfile` (see §2). |

**OpponentResult** (each `per_opponent` row): `opponent`, `wins/losses/draws/games`,
`win_rate`, its own `win_rate_ci`, and `cached` (True if that matchup came from cache).
A deck can post a healthy overall rate while getting **crushed by one opponent** — always
scan the per-opponent CIs for a matchup the candidate simply loses; that lopsided cell is
often the real finding, hidden by the average.

### Comparison (the `ab` layer)

`ab` diffs two `SimResult`s over the **same** gauntlet:
- `win_rate_delta` = `A.win_rate − B.win_rate` (points).
- `stronger` = the higher-win-rate variant, or `None` on a tie.
- `metric_deltas` = per-telemetry-metric `A − B` (`None` when either side lacks the signal).

**Confidence in a delta comes from each side's Wilson CI (i.e. sample size) — there is no
seed-paired / common-random-numbers claim.** Two variants run on the same seed are NOT a
paired test; treat the delta as real only when the CIs are well separated. Overlapping
CIs ⇒ "not distinguishable at this sample size," full stop.

---

## 2. TelemetryProfile field reference

Pooled over every game the candidate played. A field is `None`/`-`/empty when no game
supplied the underlying signal (all draws, unclassifiable wincon, etc.).

| Field | Source signal | Read it as |
|-------|---------------|-----------|
| `games` | count of pooled games | the telemetry sample size (may be < `total_games` if some games yielded no features) |
| `avg_kill_turn` / `median_kill_turn` | `GameFeatures.kill_turn` = turn a player first hit ≤ 0 life | the deck's **clock**. Median is the robust one — prefer it over the mean when a few grindy games skew the average. |
| `avg_win_margin_life` / `median_win_margin_life` | `GameFeatures.win_margin_life` = winner's remaining life at game end | **resilience** / how close wins were. High = dominant; near-0 = racing/topdeck wins. |
| `wincon_mix` | `GameFeatures.wincon` ∈ `combat` / `burn` / `mill` / `other` | **did the intended win fire?** Classified from the killing `Damage:` line (combat vs burn), `mill` when the loss was deck-out, else `other`. |
| `mean_ramp_curve` | mean lands-in-play per turn across games | the **Development-quadrant** acceleration — flat curve = ramp plan didn't land. |

### Wincon classification caveat

`wincon` is inferred from the final killing blow, so it is **coarse**: a combo kill that
routes its lethal through combat damage reads as `combat`; anything the classifier can't
attribute is `other`. A high `other` share is a flag to **read the games**, not proof the
deck has no plan. Do not report the wincon mix as ground truth for combo/engine decks —
name it as a heuristic.

---

## 3. Sample-size math (why the CI is the finding)

Win-rate is a proportion `p`; the Wilson interval half-width scales ≈ `z·√(p(1−p)/n)`,
widest at `p = 0.5`. Rough half-widths at 95% near 50%:

| Candidate total games | ≈ 95% CI half-width | Verdict strength |
|-----------------------|---------------------|------------------|
| 20 | ±20 pts | directional only — a smoke test |
| 50 | ±14 pts | rough |
| 100 | ±10 pts | "works vs doesn't" |
| 200 | ±7 pts | solid |
| **300** | **±6 pts** | **tight — quote this for a decision** |
| 1000 | ±3 pts | overkill for deck-tuning |

`--games` on `deck`/`ab` is **per opponent**; multiply by the opponent count for the
candidate total. Against an 8-deck gauntlet: `--games 4` ≈ 32 games (smoke), `--games 30`
≈ 240 (solid), `--games 40` ≈ 320 (tight). For an `ab`, the question is whether the two
CIs *separate*, which needs each side in the ~200–300 band before a single-digit delta
means anything.

---

## 4. Forge AI blind spots (why a win is weak evidence)

The gauntlet opponents run Forge's rule-based heuristic AI. It is a competent goldfish and
a passable beatdown/midrange pilot, but it systematically mis-plays whole archetypes.
Calibrate every verdict against this list:

**Forge UNDER-rates (a low win-rate here may be the AI, not the deck):**
- **Control** — sequences counters/removal poorly, taps out when it shouldn't, doesn't
  hold up interaction. A control candidate can look worse than it is.
- **Combo** — rarely assembles or protects multi-card combos; may never find the line.
  A low combo win-rate is often un-piloted, not un-viable.
- **Stax / prison / resource denial** — doesn't exploit locks; a stax deck's whole
  premise is invisible to it.
- **Intricate value engines** — misjudges long-horizon card-advantage loops.

**Forge OVER-rates (a win here is especially weak evidence):**
- **Linear aggro / go-wide beatdown** — the AI pilots this near-optimally, so the bot
  *and* your candidate both play it well; beating a fair deck proves little about a real pod.
- **Fair midrange curve-out** — the AI's comfort zone.

**Practical rule:** if the candidate is control/combo/stax and posts a low win-rate,
**suspect the AI's piloting before the deck's construction** — read the telemetry
(did the wincon ever fire? what's the kill-turn spread?) and say so. If the candidate is
linear aggro and posts a high win-rate, discount it: it cleared the AI's *strongest*
suit. Failure remains the trustworthy signal; success only confirms *functional*.

---

## 5. Caching, `--force`, seeds, and the governor

**Content-addressed cache.** Each matchup is keyed by its decks + games + seed + format +
the pinned Forge version. A matchup already run is **served from cache with zero JVM
launches** — that's why a re-run is near-instant and per-opponent rows show `[cached]`.
A Forge-version bump self-invalidates the cache automatically.

**`--force`** bypasses the cache and re-runs every matchup fresh. Use it when you suspect
staleness or want new RNG draws; otherwise leave the cache on — it's what makes iterating
on one variant cheap.

**Seeds.** `--seed` (default 42) seeds the run, but Forge's own `-s` is **not reliably
reproducible** — do not promise bit-identical replays. Because same-seed A/B is not a
paired test (§1), the seed is for coarse reproducibility, not for a CRN variance-reduction
claim. To grow the sample rather than replay it, raise `--games` (or `--force` with a new
seed).

**The governor** derives a safe concurrency pool and runs cache-misses in parallel across
it. `pool = max(1, min(hard_cap=6, cores − 2, free_RAM_GiB // 2))` — it reserves 2 cores
and budgets ~2 GiB per JVM (`-Xmx2g` + overhead), with a floor of 1 so even a starved host
runs one game at a time. It **never exhausts the machine**. `simulate doctor` prints the
derived pool plus a free-RAM/disk snapshot; check it before a large gauntlet.

**Runtime expectations.** Forge is fetched at runtime and reused if present, so the first
run may pay a one-time provisioning cost. Large gauntlets take **minutes**, and
**Commander runs ~8× slower** than constructed (longer games, bigger board states) — size
`--games` and `--format` with that in mind, and lean on the cache so you only pay for the
matchups that actually changed.
</content>
