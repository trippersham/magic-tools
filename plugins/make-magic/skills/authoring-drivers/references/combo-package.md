# The greedy-combo PACKAGE (novel spell-sequence + tutor-assembly combos)

> **Quad re-expression (Phase 4).** This package predates the in-search quad; its five stages
> re-home onto quad slots: **TUTOR & steer → S** (a `SelectionSteer` over the tutor's library
> search, category-comprehensive); **deploy + fire → macro** (`ComboMacro.apply` drives the
> deterministic win with bounded state moves); **mulligan-for-combo → the mulligan note**;
> **Φ** develops toward assembly. The retired **GO/NO-GO `probeWins` / STOP-on-kill `iWon`**
> helpers are **gone** — the go/no-go is **emergent in-search** now: the macro-fold evaluates
> firing inside CP7's own alpha-beta, and `applicable` (P) gates the real fire, so the Driver
> no longer runs its own throwaway probe. The Thoracle worked example below is realized as the
> proven `JELEVA_QUAD_SPEC` / `JelevaThoracleReferenceDriver.java` — read that for the quad
> shape; the `priority()`-override code here is the retired idiom kept for its `mage.*` API.

The `combo-loop` seed skeleton covers a **permanent-activation** loop (Mikaeus-shaped): the
pieces are permanents already on the battlefield and the driver activates one ability to
loop. But many combo decks — especially cEDH — win with a **spell-cast sequence** that is
NOT a permanent loop: cast piece A, then cast piece B in the same priority window, and the
game is over. The base AI will never assemble or fire this (it evaluates spells locally, not
as a plan), so the driver must **OWN the whole package**: assemble it, then fire it.

This reference is the greedy-combo PACKAGE the author composes for such a deck. It is a
*shape*, not a skeleton in `SEED_PATTERNS` — you write the `members` directly from the P1
idioms + P2 helpers. The worked example is **Thassa's Oracle + Demonic Consultation**
("Thoracle").

## The package (five composed stages)

```
mulligan-for-combo  →  TUTOR & steer to the missing piece  →  deploy the pieces from hand
                    →  GO/NO-GO fire (probeWins)            →  STOP-on-kill (iWon)
```

Each stage is one of the just-landed primitives. Compose them; do not re-derive.

| Stage | Primitive | Reference |
|---|---|---|
| mulligan-for-combo | `mull-for-plan` skeleton keyed on a combo piece | `pattern-catalog.md` #3 |
| **TUTOR & steer** | cast tutor from hand + steer its library search to the named missing piece | P1 idiom **§C** (`xmage-api-primitives.md`), also **§B** for a "name a card" dialog |
| **deploy from hand** | cast a piece still in hand | P1 idiom **§A** (`getPlayable(game, true)` + `activateAbility` CASTS it) |
| **GO/NO-GO fire** | activate/cast only when firing is a confirmed win | P2 helper `probeWins(game, steps)` |
| **STOP-on-kill** | short-circuit the instant the game is won | P2 helper `iWon(game)` (`opponentDead(game)` variant) |

The inherited helpers the stages lean on (all in `_DRIVER_TEMPLATE`, don't re-derive):
`onMyMainEmptyStack` / `isMyPriority` (guards), `findMine` (a piece already in play),
`activatableByRule` (an activated ability by source + rule prefix), `steerTarget` (guarded
`canTarget`→`addTarget`), `declareIntent(tag)` (the gate marker), and the go/no-go trio
`probeWins` / `iWon` / `opponentDead` bounded by `PROBE_MAX_STEPS`.

## Worked example — Thoracle (spell-sequence, tutor-assembled)

**The line.** Cast **Demonic Consultation** naming a card the deck does NOT run → the
reveal-until-named exiles the whole library. Then cast **Thassa's Oracle** with an empty
library → its ETB wins on resolution. If a piece is missing, first cast a tutor (e.g.
**Diabolic Tutor**) and steer its search to the missing piece.

**Stage TUTOR — cast the tutor (P1 §A) + steer its fetch (P1 §C).** Cast the tutor in
`priority`, then answer the library-search `chooseTarget(Outcome, Cards, TargetCard, ...)`
overload (NOT the `Target` one) by matching the tutor as the source and adding the named
missing piece:

```java
@Override
public boolean chooseTarget(mage.constants.Outcome outcome, mage.cards.Cards cards,
                            mage.target.TargetCard target, mage.abilities.Ability source,
                            mage.game.Game game) {
    mage.MageObject s = source == null ? null : game.getObject(source.getSourceId());
    if (s != null && "Diabolic Tutor".equals(s.getName())) {
        for (mage.cards.Card c : cards.getCards(game)) {
            if (c != null && "Thassa's Oracle".equals(c.getName())) {
                target.addTarget(c.getId(), source, game);   // fetch the missing piece
                declareIntent("spell-combo:thoracle-tutor");
                return true;
            }
        }
    }
    return super.chooseTarget(outcome, cards, target, source, game);   // CP6:891 → CP:893
}
```

**Stage steer-the-consult (P1 §B).** Demonic Consultation's "Name a card" is a free-form
`choose(Outcome, Choice, Game)` dialog — set a card the deck does not run so it exiles the
library:

```java
@Override
public boolean choose(mage.constants.Outcome outcome, mage.choices.Choice choice,
                      mage.game.Game game) {
    if (choice.getMessage() != null && choice.getMessage().startsWith("Name a card")) {
        choice.setChoice("Storm Crow");            // not in this deck → exiles the library
        declareIntent("spell-combo:thoracle-consult-name");
        return true;
    }
    return super.choose(outcome, choice, game);    // CP6:878 → CP:785
}
```

**Stage deploy + GO/NO-GO fire (P1 §A + P2 `probeWins`).** In `priority`, on my main with
an empty stack, build the fire sequence — cast Consultation (exiles the library) then cast
Oracle — as a `List<ActivatedAbility>` of the two hand `SpellAbility`s, and fire ONLY if
`probeWins` confirms the sequence wins on a throwaway `createSimulationForAI` copy:

```java
@Override
public boolean priority(mage.game.Game game) {
    if (iWon(game)) {                                  // STOP-on-kill: never overshoot
        return super.priority(game);
    }
    if (!onMyMainEmptyStack(game)) {
        return super.priority(game);
    }
    mage.abilities.ActivatedAbility consult = castableFromHand(game, "Demonic Consultation");
    mage.abilities.ActivatedAbility oracle   = castableFromHand(game, "Thassa's Oracle");
    if (consult != null && oracle != null) {
        java.util.List<mage.abilities.ActivatedAbility> line =
                java.util.Arrays.asList(consult, oracle);   // A then B, one window
        if (probeWins(game, line)) {                        // GO/NO-GO: confirmed win only
            if (activateAbility(consult, game)) {           // deploy piece A (P1 §A: CASTS)
                declareIntent("spell-combo:thoracle");
                return false;                               // resume next priority for B
            }
        }
    }
    // pieces not both in hand yet → cast a tutor to assemble (P1 §A), else defer to CP7
    mage.abilities.ActivatedAbility tutor = castableFromHand(game, "Diabolic Tutor");
    if ((consult == null || oracle == null) && tutor != null && activateAbility(tutor, game)) {
        declareIntent("spell-combo:thoracle-assemble");
        return false;
    }
    return super.priority(game);                            // never pass(game)
}

/** P1 §A cast-from-hand: the castable hand SpellAbility named `name`, or null. */
private mage.abilities.ActivatedAbility castableFromHand(mage.game.Game game, String name) {
    for (mage.abilities.ActivatedAbility a : getPlayable(game, true)) {
        mage.MageObject src = game.getObject(a.getSourceId());
        if (src != null && name.equals(src.getName()) && a instanceof mage.abilities.SpellAbility) {
            return a;
        }
    }
    return null;
}
```

`probeWins` copies each step's ability and drives it on the sim copy, then scores the leaf
with the engine's own `GameStateEvaluator2` and returns true only at `WIN_GAME_SCORE` — so
the two-spell exile-then-Oracle sequence is confirmed lethal on a *throwaway* copy before a
single card is committed to the live game. Bounded by `PROBE_MAX_STEPS`.

## Why this makes combos SOLO-gateable again

Because the driver OWNS assembly (tutor + deploy) and OWNS the fire (probe-gated), the whole
line runs with **no opponent needed** — a solo goldfish is enough. The base AI would never
tutor-assemble and precast a multi-spell kill on its own, so absent the driver a combo deck
under-performs its true ceiling and can only be measured against a live opponent. The greedy
package encodes the plan the base AI lacks, so `gate_mode='solo'` is correct: the payoff
shows in own-turn kill speed, and the probe guarantees the driver never sets up just to lose.
Declare a fresh descriptive intent tag per owned decision (e.g. `spell-combo:thoracle`); the
gate asserts each fired and that the driven clock is never-worse than bare CP7 — a novel line
is gate-protected exactly like a seed one.
