# Seed pattern catalog + selection + the Mikaeus worked example

> **Quad re-expression (Phase 4).** These seeds were written for the retired
> `ComputerPlayer7`-subclass path (`priority()`/`chooseTarget()` overrides). They carry forward
> as **quad shapes** — read them for the *decision each pattern owns* and the *do-not-own list*
> (both unchanged), then express the logic as quad slots: **combo-loop / spell-combo → macro +
> Φ** (the loop/sequence as `ComboMacro.apply`, gated by `applicable`=P; Φ develops toward it);
> **reanimate-target / sac-selection / discard-selection → S** (a category-comprehensive
> `SelectionSteer`); **hold-interaction / protect-commander → Φ-only** (reactive; Φ rewards
> holding interaction + mana open, no macro); **gowide-payoff → macro (light) + Φ** (cast the
> payoff, NEVER `selectAttackers`); **mull-for-plan → the mulligan note** (documented; no seam
> registry). The DO-NOT-OWN list below is the quad's guardrail surface verbatim (plus: a macro's
> `apply` must never call `priority()`/`copy()` on the handed sim). The `SEED_PATTERNS`/
> `PatternSkeleton` code the body references belonged to the retired template and is gone; the
> live emitter is `render_quad_driver(deck, QuadSpec)`.

The ranked top-8 skeletons below were once `SEED_PATTERNS`/`PatternSkeleton` code in
`driver_authoring.py` for the retired `ComputerPlayer7`-subclass template; that code (and the
`LineSpec`/`PatternSkeleton.build` machinery) is **gone**. They survive here as a *design
catalog* — read each for the decision it owns, then express it as quad slots per the
Quad re-expression note above. The live emitter is `render_quad_driver(deck, QuadSpec)`, and
`JELEVA_QUAD_SPEC` in `driver_authoring.py` is the **proven** end-to-end positive control (a
proactive spell-combo quad: Φ + macro + P + S). The other rows are coherent design shapes —
grow one into a `QuadSpec` when a specific deck's goldfish shows the miss.

**Routing note (2026-08-27 combo-litmus pivot).** The `gate_mode` column below is the *old*
solo/match label, and the old "proactive → macro-gate / reactive → Φ-only floor" split is
**retired as a router**. The top-level classifier is now a **combo litmus**: a deck DRIVES iff
`combos_in_deck` finds a concrete in-deck win-combo (all pieces in the 99, `result` is a
game-win) — else it is **THIN** (bare CP7). There is **one** gate and it is **pure measurement**
(`MACRO_FIRE_REAL` capability + never-slower vs the SAME deck on bare CP7 ±2 + brick-cap
validity + `_MIN_GATE_GAMES=20`, commander-`fmt`) with **NO** bracket/archetype absolute turn
bar. Proactive/reactive survives only as an **input to the rule-4 Φ-mode choice**
(dedicated vs combo-capable), never as the route or the gate mode. Consequently the reactive/
Φ-only rows below (hold-interaction, protect-commander) are **thin/flag** shapes — they did not
measure as a win (see `research/gutcheck-defended-lens.md`, `prior-research-reconciliation.md`)
— not a DRIVE route.

---

## The ranked catalog

Ranked by (measured base-AI gap) × (archetype breadth) × (clean measurability).

| # | key | hook(s) | gate_mode | Owns | What CP7 misplays | Over-engineering risk |
|---|---|---|---|---|---|---|
| 1 | `combo-loop` | priority + chooseTarget | solo | Fire the deck's scripted win loop once all pieces are in play, empty stack, my main. | CP7 evaluates one activation at a time under minimax and wanders off a multi-step loop — it cannot sequence one. **Total gap. PROVEN.** | Gate HARD on all-pieces-present; never partial-assemble by force — let CP7 develop and cast the pieces. |
| 2 | `hold-interaction` | priority + playMana | match | The *mana-open discipline*: on my turn, holding a named counter, keep ≥ reserve mana up instead of tapping out. | Under-values reactive cards — 2.8% counter rate vs 31% for the minimax build. **Biggest measured gap.** | Own only mana-open, NOT what to counter (that's CP7 / chooseUse). Skip for tap-out combo/aggro. Solo goldfish can't see it → `match`. |
| 3 | `mull-for-plan` | chooseMulligan | solo | Keep/ship by the deck's plan: a land window + one key-piece check. | Mulligans on generic land-count, ignoring the deck's key enabler. | Keep it LIGHT — a land window + one "has a key piece?" check; never a full evaluator, never mull below 5. |
| 4 | `reanimate-target` | chooseTarget | solo | Steer the reanimation spell's graveyard target to the deck's bomb. | Generic target choice returns the first legal body, not the bomb the deck is built to cheat. | Own only the pick, scoped to the reanimation spell source. Don't own whether to reanimate or how the yard filled. |
| 5 | `sac-selection` | choose (Outcome.Sacrifice) | solo | Feed a token / expendable body to the outlet before a real threat. | Sacrifices sub-optimally for synergy decks (a threat over fodder). | Own only the ORDERING; CP7 decides whether/when to go off. |
| 6 | `gowide-payoff` | priority (NOT selectAttackers) | solo | Cast the anthem/overrun in my precombat main when the board is wide. | May fire the overrun off-sequence or swing without it. | **HARD RULE: priority(), never selectAttackers.** Own the payoff CAST only; defer ALL combat. Force-attack regresses. |
| 7 | `discard-selection` | choose (Outcome.Discard) | solo | Pitch an off-plan card; protect the named key piece. (Reanimator INVERTS: bin the bomb.) | Generic discard can pitch a payoff. | Own a small keep/dump preference, not a whole-hand ranking. The reanimator inversion is a deck-specific fill. |
| 8 | `protect-commander` | priority + chooseUse | match | Keep protection + mana open, fire it in RESPONSE to removal targeting my commander/voltron body. | Doesn't reserve reactive protection; wastes it proactively. | Gate strictly on "my key permanent is the target of a stack object." Never pre-cast, never force the swing. Higher regression risk. |

**The long tail** (build only when a specific deck's goldfish shows the miss, not as a
seed): wrath timing, answer-priority whitelist, ramp-first sequencing, stax asymmetry,
lifegain/superfriends payoffs, mana-color sequencing. Each is real but either low-gap
(CP7 already competent) or hard-to-gate (regression risk).

---

## Archetype → pattern selection

> **Subordinate to the combo litmus.** This map is a *within-DRIVE* guide for which S/macro/Φ
> shapes a combo deck wants — it does NOT decide drive-vs-thin. The combo litmus decides that
> first: no concrete in-deck win-combo ⇒ THIN (bare CP7), regardless of archetype. The
> control/reanimator/voltron rows below whose value is reactive Φ-only are **thin/flag** unless a
> concrete win-combo is also present.

Match the deck's Strategy `PRIMARY STRATEGY:` line + its `Focus Otags`/`otag_buckets` to the
pattern(s). Usually 1–2 patterns; `mull-for-plan` rides along on almost everything.

- **combo** → `combo-loop` (+ `mull-for-plan` keyed on a combo piece). Many combo decks
  also want `hold-interaction` to protect the line — co-author when the deck runs counters.
- **control / spellslinger-control / stax** → `hold-interaction`.
- **reanimator** → `reanimate-target` (+ `discard-selection` inverted to bin the bomb).
- **aristocrats / tokens** → `sac-selection` (+ `gowide-payoff` for a token-anthem deck).
- **go-wide** → `gowide-payoff` — but note most go-wide decks score **thin** (CP7 pilots
  token beatdown fine; the driver owns only the pre-combat payoff cast).
- **voltron** → `protect-commander` (higher regression risk; author only the responsive
  protection, never combat).
- **any** → `mull-for-plan` as the baseline thin driver.

### Driver-worthy vs CP7-fine (the triage that governs selection)

From the corpus study, three verdicts:
- **Y (driver-worthy, ~50%)** — a non-trivial pattern genuinely applies (combo /
  hold-interaction / reanimate / sac / protect-commander) or the base AI demonstrably
  misplays the line. The real authoring backlog. Compose the substantive pattern.
- **thin (~34%)** — only `mull-for-plan` + a long-tail nudge (gowide / ramp-first /
  lifegain). A driver adds a little; ship a cheap thin driver or defer.
- **N (CP7-fine / un-authorable, ~16%)** — aggro / typal beatdown / goodstuff with no
  ownable line; politics / group-hug (no ground truth in a goldfish/1v1). Ship at most a
  `mull-for-plan` stub, or none — do NOT invent a line. Politics decks: flag and stop.

The selection rule: **a CP7-fine deck gets only a thin `mull-for-plan` (or defer); never
manufacture an ownable line to justify a driver.** Inventing one fails the three tests and
risks the never-worse gate.

---

## The DO-NOT-OWN list (owning these regresses win-rate)

- **Attacker selection / force-attack** — the spike's load-bearing finding: a thick
  driver that force-attacked did *worse* than CP7. Own the payoff cast; never who swings.
- **Generic combat & most blocking** — CP7 does basic aggro/combat fine. (Narrow
  exception: a defensive chump to protect a walker/commander, condition-gated, respond-only.)
- **Land drops & curve sequencing** — CP7 curves out.
- **Politics / group-hug / goad / threat assessment across opponents** — no ground truth
  in goldfish/1v1; a thin gate can't encode multi-agent social reasoning. Out of scope.
- **Overriding `copy()`** — corrupts CP7's own minimax search (see xmage-api-primitives.md).
- **"Own-my-main ⇒ never defer" branches** — every override must fall through to `super`.

---

## Worked example — the live quad (`JELEVA_QUAD_SPEC`) and the retired Mikaeus fill

The live, gate-passing positive control is **`JELEVA_QUAD_SPEC`** in `driver_authoring.py` — a
proactive Thassa's-Oracle spell-combo quad. Read it there in full (and its rendered form via
`render_quad_driver`); the shape to learn:

- **Φ** (`phi_body`, from **Gameplan**) — a monotone potential rising across the whole assembly
  path (hold a combo half → a tutor can fetch the missing half → both halves → library empty +
  Oracle), so CP7's leaf evaluator develops toward the combo.
- **macro + P** (`MacroSpec`, from **Win Condition** + **Assembly**) — `applicable` (P) gates on
  "the deterministic kill is executable NOW" (my turn, empty stack, Oracle in hand + an
  exile-library piece + mana, or library already empty); `apply` drives the known outcome with
  bounded explicit state moves (exile the library, ETB the Oracle, resolve), **never**
  `priority()`/`copy()` on the handed game. The emitter injects the standard `DRIVER_MACRO_FIRED`
  marker at `apply` entry — the gate's proactive slot-exercise signal.
- **S** (`SteerSpec`, from **Sequencing**) — steer a tutor's library search to the *missing*
  combo half, category-comprehensively; the emitter emits `DRIVER_STEER_FIRED` on a true return.

A new proactive combo copies this: swap the piece names, the Φ milestones, the `applicable`
gate, and the steer's want-list; keep the guard→act→defer shape. A Φ-only reactive deck (e.g.
`SHORIKAI_REACTIVE_QUAD_SPEC`) emits Φ only — no macro/P/S — and is gated on the never-worse
floor.

**The retired Mikaeus fill (historical).** Before the quad, the proven end-to-end example was
`MIKAEUS_LINE_SPEC` — a `ComputerPlayer7`-subclass thin driver (Mikaeus + Triskelion undying
ping-loop) that owned three thin responsibilities via `priority()`/`chooseTarget()`/
`chooseMulligan()` overrides, emitting a `DRIVER_LINE_FIRED` marker. That template and its
`LineSpec` were **deleted in Phase 5**; the loop it expressed maps onto the quad as **macro + Φ**
(the ping-loop as `ComboMacro.apply`, gated by `applicable`; Φ develops toward assembly). It is
retained here only as a design reference for the guard→act→defer discipline, not as live code.
