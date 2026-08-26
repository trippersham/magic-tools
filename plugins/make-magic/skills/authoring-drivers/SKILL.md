---
name: authoring-drivers
description: >
  Derive an in-search QUAD sim Driver (Φ, P, macro, S) from a deck's deck-primer Strategy, emit
  it as register-by-playerId Java, ECJ-compile it, and hand it to the ship gate. TRIGGER when:
  user says "author a driver for [deck]", "make a sim pilot for this deck", "give this deck a
  driver", "why does the AI misplay my combo", or the gauntlet needs a companion Driver per
  deck. SKIP for distilling/eliciting the Strategy itself (distilling-strategy), for card
  selection (refining-decks), or for running the gauntlet without authoring (the sim CLI).
user-invocable: true
---

# Authoring Drivers (the in-search quad)

Turn a deck's **deck-primer Strategy** into a **quad Driver** `(Φ, P, macro, S)` — pure
functions the patched XMage minimax consults *inside its own search*, keyed by `playerId`.
PlayerA stays a plain `ComputerPlayer7`; the Driver only **registers** its quad, so any
unregistered seat is pure CP7 (the intelligence-preserving property). This is NOT the retired
engine-replacement subclass — there is no `ComputerPlayer7` to extend and no `copy()` to
override.

<primary-constraint>
**The proactive/reactive call routes the whole Driver — make it first.**

A **proactive** deck (aggro/combo/midrange/go-wide/voltron) *enacts a win*: it gets a **macro**
(the deterministic win sequence) gated by **P** (assembly precondition), plus **Φ** and optional
**S**. A **reactive** deck (control/stax/spellslinger-control) *answers*: it is **Φ-only** — no
macro/P/S. Emit a macro for a reactive deck and the gate has nothing real to fire; omit one for
a proactive combo deck and the AI never assembles the kill. Read `PRIMARY STRATEGY` and decide
before deriving a single slot.
</primary-constraint>

## The quad, and where each slot comes from

Read the deck's Strategy through the **deck-primer lens** (see
`building-decks/references/strategy-schema.md`, "The deck-primer lens"). Each primer reading
maps to one quad slot:

| Primer reading | Quad slot | Java shape (emitted by `driver_authoring.render_quad_driver`) |
|---|---|---|
| Gameplan / identity | **Φ** | `int phi(Game, UUID)` — a bounded (±1e6) monotone potential toward the plan, registered into `DriverBonus` |
| Win condition(s) | **macro** | `ComboMacro.apply(Game, UUID)` — the deterministic win, driven with bounded state moves, registered into `MacroRegistry` |
| Assembly / the combo turn | **P** | `ComboMacro.applicable(Game, UUID)` — "is the kill executable NOW?" |
| Key sequencing & choices | **S** | `SelectionSteer.apply(...)` — a category-comprehensive selection steer, registered into `SelectionRegistry` |
| Mulligan / keepable hands | note | documented in the class javadoc (the frozen seam has no mulligan registry yet) |

The emitter (`pipeline/pipeline/sim/driver_authoring.py`) owns the ingestion contract: you write
each slot's **body**; it generates the `public static void register(UUID playerId)` that wires
the (up to) three registries, the method/inner-class signatures, and the non-null `Player me`
guard. The reference implementation is
`pipeline/pipeline/sim/reference_drivers/JelevaThoracleReferenceDriver.java`; the worked specs
`JELEVA_QUAD_SPEC` (proactive) and `SHORIKAI_REACTIVE_QUAD_SPEC` (Φ-only) are your templates.

## The §7.1 authoring intuitions (the hard-won discipline — do not lose these)

These are load-bearing. Every quad must honor them; `check_quad_guardrails` enforces the
mechanical ones and the never-worse gate (P5) is the ultimate arbiter.

- **Own the noun, defer the verb.** The **real-seat slots** — **S** and the **macro's real
  fire** — act on ONE owned decision (what to cast/target/keep) and defer everything else. Never
  own combat, land drops, attacker selection, or generic sequencing. Φ and the in-search
  macro-fold run *inside* the search by design and cannot regress the real seat. This is the
  core never-regress discipline: a Driver that re-pilots what CP7 already does well plays
  *worse* than bare CP7.
- **Category/mechanic altitude, not individual cards.** Write **S** to match a **comprehensive
  predicate over a category** ("any top-of-library tutor," "any sacrifice outlet"), never one
  card as a proxy for the category — that silently breaks on a swap and misses comparable
  members. `getName()` matched against the **FULL member set** of a category is acceptable *when
  that set is knowable at design time* (enumerated name-sets are drift-safe: a swap flips
  `driver_valid`, re-authoring picks up the new member). A discrete infinite/deterministic combo
  **is** its named pieces — name them (that is the macro's own precondition).
- **The do-not-own guardrails.** Never own: attacker selection / force-attack; generic
  combat/blocks (narrow exception: a condition-gated respond-only chump for a walker/commander);
  land drops / curve; politics / goad / multi-opponent threat assessment (no ground truth in a
  goldfish/1v1). And — specific to the quad — the macro's `apply` must **never call
  `priority()` or `copy()`** on the handed game: drive the known outcome with explicit
  `moveCards` / `applyEffects` / a capped `getStack().resolve` (bounded by
  `ComboMacro.PROBE_MAX_STEPS`).
- **The three-beat guard→act→defer shape** is the shape of every real-seat slot: **guard** on
  "is this my owned decision now?" (S: `pid.equals(source.getControllerId())`; macro:
  `applicable`), **act** once, **defer** (`return false`) everywhere else so CP7 keeps control.

## The seed patterns are examples, NOT a closed menu (open-ended authoring)

The pattern catalog (`references/pattern-catalog.md`) and the combo package
(`references/combo-package.md`) are a **starting vocabulary**, re-expressed as quad shapes:

| Seed pattern | Quad shape |
|---|---|
| combo-loop / spell-combo | **macro + Φ** — the win sequence as `apply`, gated by `applicable` (P); Φ develops toward it. S if a tutor assembles it. |
| reanimate-target / sac-selection / discard-selection | **S** — a category-comprehensive selection steer (the graveyard bomb, the expendable fodder, the off-plan pitch). |
| hold-interaction / protect-commander | **Φ-only** (reactive) — Φ rewards holding interaction + mana open; no macro. |
| gowide-payoff | **macro (light) + Φ** — Φ rewards a wide board; macro casts the payoff (never `selectAttackers`). |
| mull-for-plan | the **mulligan note** (documented; no seam registry yet). |

When none fits, **compose a novel quad from the primitives** — the gate is what makes a novel
line safe, not membership in a menu. A line that declares its win (macro fires) and does not
regress the solo clock (never-worse) is a legal Driver regardless of which primitives it
stitched together. Do NOT manufacture an ownable line for a CP7-fine deck (linear aggro /
goodstuff): record an honest Φ-only or a thin Driver and stop.

<reference file="references/xmage-api-primitives.md">
The registration contract (the current seam) + the `mage.*` primitives the slot bodies call
(read/query helpers, the tutor-search overloads, gotchas). The top section is authoritative for
the quad; the lower §A/§B/§C idioms describe the retired subclass path — read them for the
`mage.*` API surface, but express the logic as quad slots.
</reference>
<reference file="references/pattern-catalog.md">
The ranked seed patterns + archetype→pattern selection + the DO-NOT-OWN list. Re-expressed as
quad shapes per the table above.
</reference>
<reference file="references/combo-package.md">
The greedy-combo package (tutor-assemble + fire) — now expressed as **S** (tutor steer) +
**macro** (the fire, in-search go/no-go replaces the retired `probeWins`).
</reference>

## The authoring flow

1. **Read the Strategy + classify.** `collection get-deck "<deck>" --field strategy`. Make the
   **proactive/reactive** call from `PRIMARY STRATEGY`. Proactive ⇒ a macro; reactive ⇒ Φ-only.
2. **Derive the slots** from the primer readings (table above). For a proactive deck: Φ from the
   Gameplan, `apply` (macro) from the Win Condition, `applicable` (P) from the Assembly turn, S
   from the `KEY LINES` `OWN:` at category altitude. For a reactive deck: Φ only.
3. **Build the `QuadSpec`** (`driver_authoring.QuadSpec` / `MacroSpec` / `SteerSpec`) and render
   with `render_quad_driver(deck, spec)`. `macro=None, steer=None` ⇒ a Φ-only reactive class.
4. **Guardrail-check.** `check_quad_guardrails(rendered)` — raises `GuardrailViolation` on a
   `priority()`/`copy()`-in-macro, owned combat, or owned land drops. Fix and re-render.
5. **ECJ-compile.** `driver_compile.compile_driver(source)` (or `compile_for_injection`) against
   the effective dist jar; surface ECJ diagnostics into a repair loop. Cached on
   (dist SHA, source hash).
6. **Gate (P5).** Hand the compiled quad to the ship gate in its mode: proactive ⇒ macro fires +
   never-slower solo; reactive ⇒ never-worse-solo floor + opt-in defended lens.

## Verify

- **Emit a compiling quad for one deck.** Author a `QuadSpec` from a real Strategy → render →
  ECJ-compile succeeds against the local dist jar. `JELEVA_QUAD_SPEC` (proactive) and
  `SHORIKAI_REACTIVE_QUAD_SPEC` (Φ-only) are the reference specs, proven by
  `tests/test_driver_authoring.py::test_*_quad_ecj_compiles_against_real_dist`.
- **A guardrail violation is rejected** by `tests/test_driver_guardrails.py`
  (`check_quad_guardrails`): a macro `apply` that calls `priority()`/`copy()` on the handed sim,
  a quad that owns combat/attacker selection, or one that owns land drops. This is **AC8** —
  keep it enforced by the test.

## When NOT to use

- **Eliciting the Strategy** — that is `distilling-strategy`; this skill CONSUMES a written
  Strategy.
- **A CP7-fine deck** (linear aggro / goodstuff / politics) — do not invent a line. Ship a Φ-only
  or thin Driver, or defer. Politics/group-hug decks: flag and stop (no goldfish ground truth).
- **Running the gauntlet** without new authoring — use the sim CLI with the pre-compiled Drivers.
