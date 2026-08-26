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

The ranked top-8 skeletons live in code as `SEED_PATTERNS` in
`pipeline/pipeline/sim/driver_authoring.py`. Each `PatternSkeleton` carries the correct
hook, the guard idiom, an `intent_tag`, a `gate_mode`, and `@@PLACEHOLDER@@` tokens for
the deck-specific bits. Only `combo-loop` is a **proven** end-to-end fill (Mikaeus, the
gate's positive control); the other seven are coherent documented skeletons (right shape,
grow by extraction as a specific deck shows the miss).

`PatternSkeleton.build(name, fills)` fills the placeholders and returns a `LineSpec`. You
can also read a skeleton's `.sample_fills` to see a concrete render.

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

## The Mikaeus worked example (the proven fill)

`MIKAEUS_LINE_SPEC` in `driver_authoring.py` is the one gate-passing end-to-end fill — the
positive control. Read it there in full; the shape to learn:

**The line.** With Mikaeus, the Unhallowed + Triskelion both on my battlefield, on my main
with an empty stack, ping the opponent with Triskelion's counters; at 2 counters left,
self-ping so Triskelion re-dies at 0 → undying refuels it to 4 → loop to lethal.

**What it OWNS (three thin responsibilities), each deferring to `super` otherwise:**
1. **The loop** (`priority`) — guarded by the inherited `onMyMainEmptyStack(game)`; finds
   the two pieces with `findMine`; activates Triskelion's "Remove a" ping via
   `activatableByRule`; sets a `pingSelfNext` flag at ≤2 counters; calls
   `declareIntent("combo-loop:mikaeus")` when the ability actually activates. If the pieces
   aren't assembled or the ability is momentarily gone (mid death/undying return), it
   returns `super.priority(game)` and lets CP7 advance triggers/SBAs.
2. **Triskelion's damage target** (`chooseTarget`) — scoped to `Outcome.Damage` +
   `source` name == "Triskelion" + controlled by me; steers to Triskelion itself when
   `pingSelfNext`, else to an opponent, via the guarded `steerTarget`. Everything else →
   `super`.
3. **The mulligan** (`chooseMulligan`) — never below 5; ship no-landers (< 2) and floods
   (> 5); keep the rest. Visible info only.

**What it does NOT own:** no attacker selection, no `chooseUse`, no develop override, no
`copy()`. CP7 develops the board, casts the pieces, and fights all combat.

**The intent marker.** `intent_tags=('combo-loop:mikaeus',)`, `gate_mode='solo'`. The tag
appears as a `declareIntent("combo-loop:mikaeus")` call at the point the loop fires; the
gate asserts that marker emitted and that the driven own-turn clock is never-worse than
bare CP7. Fully-qualified `mage.*` refs keep `imports=()`.

This is the template a new combo fill copies: swap the two piece names, the loop's rule
prefix, and the mulligan key piece; keep the guard→act→declare→defer shape verbatim.
