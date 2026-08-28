---
name: authoring-drivers
description: >
  Classify a deck by the COMBO LITMUS, then either seed an in-search QUAD sim Driver
  (Φ, P, macro, S) deterministically from the detected win-combo (DRIVE) or emit a neutral
  bare-CP7 driver (THIN), ECJ-compile it, and hand it to the ship gate. TRIGGER when: user says
  "author a driver for [deck]", "make a sim pilot for this deck", "give this deck a driver", "why
  does the AI misplay my combo", or the gauntlet needs a companion Driver per deck. SKIP for
  distilling/eliciting the Strategy itself (distilling-strategy), for card selection
  (refining-decks), or for running the gauntlet without authoring (the sim CLI).
user-invocable: true
---

# Authoring Drivers (the in-search quad — combo litmus)

Classify a deck by a **combo litmus**, then produce its Driver. A **quad Driver** `(Φ, P, macro,
S)` is a set of pure functions the patched XMage minimax consults *inside its own search*, keyed
by `playerId`. PlayerA stays a plain `ComputerPlayer7`; the Driver only **registers** its quad,
so any unregistered seat is pure CP7 (the intelligence-preserving property). This is NOT the
retired engine-replacement subclass — there is no `ComputerPlayer7` to extend and no `copy()` to
override.

<primary-constraint>
**The combo litmus is the top-level classifier — run it FIRST, before deriving anything.**

Run `win_combos_in_deck(deck_identity, combos)` (from `pipeline.transforms.combo_detect`).

- **DRIVE** — it returns ≥1 concrete win-combo (all pieces in the 99, `result` a GAME-WIN per
  `is_game_win_result`). Seed the quad **deterministically from the chosen Combo** with
  `seed_quad_from_combo(combo, archetype=...)`. You do NOT invent the line — the emitter fills a
  fixed template (P + S generated from `card_names`, a bounded macro scaffold from `result`).
- **THIN** — it returns `[]` (no concrete in-deck win-combo). Emit `QuadSpec.thin(name)` — a
  NEUTRAL driver (Φ=0, no macro/P/S, optional real mulligan) that is behaviorally **bare CP7**.
  Record "bare CP7" and stop.

Proactive/reactive is NOT the router anymore. It survives only as one *input* to the rule-4
Φ-mode choice (dedicated vs capable) below — never as the top-level route.
</primary-constraint>

## The 4 load-bearing rules (design §5)

1. **Litmus (drive/thin).** DRIVE iff `win_combos_in_deck` returns ≥1 combo whose `result` is a
   game-win (`is_game_win_result` — a lethal / "win the game" / "each opponent loses" / "infinite
   damage" payoff, NOT a bare "infinite mana/draw/tokens" loop with no in-deck finisher) and all
   pieces are in the 99. Else THIN.
2. **Tiebreak (multi-combo).** Among qualifying win-combos, seed from the **fewest-pieces** one,
   preferring one the **commander participates in**. Two equally-central win lines → author the
   primary + **flag** the second (its macro is a follow-up).
3. **Seed the quad (deterministic).** `seed_quad_from_combo(combo, archetype=...)` fills the fixed
   template from the Combo: `card_names` → **P** (`applicable` = all pieces present & castable
   this turn) + **S** (steer a tutor/search to a still-missing piece); `result` → **macro** (a
   bounded win-enactment **scaffold** — no `priority()`/`copy()`). The macro body is a
   clearly-marked `TODO(author)` scaffold that enacts the terminal (each opponent loses) so the
   seam has a concrete deterministic win to execute + measure; the **true card-specific
   enactment is authored per-deck downstream** (moveCards / applyEffects / a capped
   `getStack().resolve`). The P precondition and S fetch ARE concretely generated from
   `card_names` — those are not scaffolds.
4. **Φ = a SMALL opportunistic nudge (empirically calibrated — this is the default for EVERY
   DRIVE deck).** Seed a small, bounded Φ nudge toward assembly with
   `seed_nudge_quad(combo, alpha=2000)` — **not** a heavy staging Φ. A statistically-significant
   sweep (`research/nudge-magnitude-sweep.md`; n=80 solo, n=90 defended over a 6-opponent field)
   found driver win-rate, solo fire-rate, AND goldfish speed **all peak at a small α ≈ 2000**:
   - **small nudge (α ≈ 2000) — the default.** Beats macro-only on defended win-rate (Heliod
     +0.16, p=0.013; Marchesa +0.12, p=0.063 corroborated by its solo fire-rate peaking at the
     same α) and is **never-slower** on the goldfish clock, so it passes the gate (neutral for
     clean combos, ~1 turn *faster* for board-assembly combos).
   - **thin Φ=0 (`alpha=0`, macro/P/S only) — the floor.** The macro carries the core 0→fires
     capability; fall back to this only when a small nudge can't be expressed for the deck.
   - **heavy dedicated-scale Φ (α ≈ 40000) — AVOID.** Never beat the small nudge in the sweep and
     trends *worse* (it over-steers CP7 off its race — the original ladder regression, Karador 8→15).
     "Dedicated heavy-Φ" is **not** a validated superior mode.
   - Low-centrality decks whose combo rarely assembles (Ghave-style) → the nudge is inert →
     effectively thin; that's fine, the macro still captures the combo on the rare turns it comes together.
   - α ≈ 2000 is the sweep's optimum across 3 decks — a strong default, tunable per deck; the Φ
     uses a cheap early-out (return 0 when no piece is live) so the nudge does not tax search.

Optional **mulligan** slot (`MulliganSpec`) composes with either DRIVE mode or with THIN — a real
slot, but OPTIONAL (ablation showed mulligan-in-isolation net-negative; the default is none).

## The emitter API (what you actually call)

`pipeline/pipeline/sim/driver_authoring.py` owns the ingestion contract — you never write the
`register(UUID)` wiring.

| Call | Returns / does |
|---|---|
| `combo_detect.ensure_combo_lake()` | guarantees `normalized/combo.parquet` (no-op if present; else `spellbook.sync()` + `build()`). Run before the litmus. |
| `combo_detect.load_combos()` | the `list[Combo]` to pass to the litmus. |
| `combo_detect.win_combos_in_deck(identity, combos)` | the **rule-1 DRIVE gate** — concrete win-combos in the deck. |
| `combo_detect.is_game_win_result(result)` | the rule-1 win-result predicate (game-win vs bare-infinite). |
| `driver_authoring.seed_nudge_quad(combo, alpha=2000)` | **rule-3 + rule-4 seed (PREFERRED for DRIVE)** → a `QuadSpec` with a small opportunistic Φ nudge (α≈2000 default; α=0 ≡ thin+macro). Empirically the best mode — see rule 4. |
| `driver_authoring.seed_quad_from_combo(combo, archetype=...)` | the older archetype-routed seed (dedicated/capable Φ); **superseded** by `seed_nudge_quad` per the nudge sweep — keep only for the two-mode API. |
| `driver_authoring.QuadSpec.thin(name, mulligan=?)` | the **THIN** neutral quad (Φ=0, bare CP7) — the no-combo path (or `seed_nudge_quad(..., alpha=0)`). |
| `driver_authoring.ARCHETYPES` / `validate_archetype(a)` | the vocabulary `('drive-dedicated','drive-capable','thin')` + its check. |
| `driver_authoring.render_quad_driver(deck, spec)` | the compilable `Driver.java`. |
| `driver_authoring.check_quad_guardrails(src)` | the §7.1 do-not-own static screen. |

The reference specs `JELEVA_QUAD_SPEC` and `SHORIKAI_REACTIVE_QUAD_SPEC` remain as worked
examples of hand-authored quads (they carry documentary legacy `proactive`/`reactive` labels);
the combo-litmus path produces its quad through `seed_quad_from_combo` / `QuadSpec.thin`.

## The deck-primer lens (a reading, not a router)

Read the deck's Strategy through the **deck-primer lens** (see
`building-decks/references/strategy-schema.md`, "The deck-primer lens"). The lens **corroborates
+ refines** the litmus and informs Φ-mode; it does NOT route and it does NOT set a gate mode:

| Primer reading | Role in authoring |
|---|---|
| Gameplan / identity | informs **Φ-mode** (dedicated staging vs neutral) |
| Win condition(s) | corroborates the litmus + the **macro** seed (which combo to enact) |
| Assembly / the combo turn | corroborates **P** (`applicable`) |
| Key sequencing & choices | refines **S** (category-comprehensive) + macro ordering when you author the per-card line |
| Mulligan / keepable hands | the OPTIONAL mulligan hook |
| *Proactive or reactive? / archetype* | **input to the rule-4 Φ-mode choice only** — NOT the top-level route |

## The §7.1 authoring intuitions (do not lose these)

Load-bearing when you refine the seeded macro's per-card line and the S steer.
`check_quad_guardrails` enforces the mechanical ones; the never-worse gate (P6) is the ultimate
arbiter.

- **Own the noun, defer the verb.** The **real-seat slots** — **S**, the **macro's real fire**,
  and the **mulligan** keep/ship — act on ONE owned decision (what to cast/target/keep) and defer
  everything else. Never own combat, land drops, attacker selection, or generic sequencing. Φ and
  the in-search macro-fold run *inside* the search by design and cannot regress the real seat. A
  Driver that re-pilots what CP7 already does well plays *worse* than bare CP7.
- **Category/mechanic altitude, not individual cards.** Write **S** to match a **comprehensive
  predicate over a category** ("any top-of-library tutor," "any sacrifice outlet"), never one card
  as a proxy. `getName()` matched against the **FULL member set** of a category is acceptable
  *when knowable at design time*. A discrete combo **is** its named pieces — the seeder names them
  (that is the macro's own precondition).
- **The do-not-own guardrails.** Never own: attacker selection / force-attack; generic
  combat/blocks; land drops / curve; politics / goad. And — specific to the quad — the macro's
  `apply` must **never call `priority()` or `copy()`** on the handed game: drive the known outcome
  with explicit `moveCards` / `applyEffects` / a capped `getStack().resolve` (bounded by
  `ComboMacro.PROBE_MAX_STEPS`).
- **The three-beat guard→act→defer shape** is the shape of every real-seat slot: **guard** on "is
  this my owned decision now?", **act** once, **defer** (`return false`) everywhere else so CP7
  keeps control.

<reference file="references/xmage-api-primitives.md">
The registration contract (the current seam) + the `mage.*` primitives the slot bodies call. The
top section is authoritative for the quad; the lower §A/§B/§C idioms describe the retired subclass
path — read them for the `mage.*` API surface, but express the logic as quad slots.
</reference>
<reference file="references/pattern-catalog.md">
The seed patterns as quad SHAPES (selection → S; combo/loop → macro + Φ; mulligan → the optional
mulligan hook). Subordinate to the combo litmus — reactive "hold-interaction / protect-commander"
patterns survive only as THIN/flag cases, never a DRIVE route.
</reference>
<reference file="references/combo-package.md">
The greedy-combo package (tutor-assemble + fire) — now expressed as **S** (tutor steer) +
**macro** (the fire, in-search go/no-go replaces the retired `probeWins`).
</reference>

## The authoring flow

1. **Ensure the combo lake + load combos.** `ensure_combo_lake()`; `combos = load_combos()`.
2. **Run the litmus (rule 1).** Build the deck identity set (card names and/or oracle_ids);
   `won = win_combos_in_deck(identity, combos)`.
   - `won == []` → **THIN**: `spec = QuadSpec.thin("<deck>")`, record "bare CP7", skip to step 5.
   - `won` non-empty → **DRIVE**: continue.
3. **Tiebreak (rule 2).** Pick the fewest-pieces win-combo (prefer commander participation; flag a
   co-primary second line).
4. **Seed the quad with a small opportunistic nudge (rules 3 + 4).**
   `spec = seed_nudge_quad(combo, alpha=2000)` — the empirically-best mode (small Φ nudge + macro +
   P + S; see rule 4). Use `alpha=0` only if a nudge can't be expressed; never scale to the heavy
   α≈40000 dedicated range (it regresses). Optionally attach a `MulliganSpec` when the deck has a
   real keep/ship read. (The macro body is a `TODO(author)` scaffold — refine the true per-card
   bounded win line downstream, honoring the §7.1 discipline; P + S are already concrete.)
5. **Render + guardrail-check.** `src = render_quad_driver(deck, spec)`;
   `check_quad_guardrails(src)` (raises `GuardrailViolation` on `priority()`/`copy()`-in-macro,
   owned combat, or owned land drops). Fix and re-render.
6. **ECJ-compile.** `driver_compile.compile_driver(source)` against the effective dist jar;
   surface ECJ diagnostics into a repair loop. Cached on (dist SHA, source hash).
7. **Gate (P6 — pure measurement).** DRIVE ⇒ `MACRO_FIRE_REAL` + never-slower-than-same-deck-CP7
   (±2) + brick-cap valid. THIN ⇒ recorded as bare CP7 (CP7-both-sides control; not gated for a
   macro it does not have).

## Verify

- **THIN emits a compiling neutral quad.** `QuadSpec.thin("x")` → render → ECJ-compiles; Φ is
  `return 0;`, no Macro/Steer/Mull inner class; passes `check_quad_guardrails`. Proven by
  `tests/test_driver_authoring.py::test_quadspec_thin_is_neutral_bare_cp7` +
  `::test_thin_quad_ecj_compiles_against_real_dist`.
- **A combo seed compiles.** `seed_quad_from_combo(combo, archetype=...)` → render → ECJ-compiles
  for both Φ-modes, with the piece names concretely in P/S. Proven by
  `::test_seed_quad_from_combo_produces_drive_quad` +
  `::test_combo_seeded_quad_ecj_compiles_against_real_dist`.
- **The litmus is correct.** A Thoracle-style win-combo identity returns the combo; an
  infinite-mana-only identity returns `[]`. Proven by
  `tests/test_combo_litmus.py::test_win_combos_in_deck_*`.
- **A guardrail violation is rejected** by `tests/test_driver_guardrails.py`
  (`check_quad_guardrails`). This is **AC8** — keep it enforced.

## When NOT to use

- **Eliciting the Strategy** — that is `distilling-strategy`; this skill CONSUMES a written
  Strategy (as a corroborating lens on the litmus).
- **A THIN deck** (no in-deck win-combo — value / control / midrange / linear aggro): do not
  invent a line. Emit `QuadSpec.thin` and record bare CP7. This is the honest default, not a
  fallback.
- **Running the gauntlet** without new authoring — use the sim CLI with the pre-compiled Drivers.
