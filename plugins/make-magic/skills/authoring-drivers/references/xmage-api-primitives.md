# XMage API primitives for a per-deck driver fill

Distilled from `research/xmage-api-surface.md`. A driver is a **thin** `ComputerPlayer7`
(CP7) subclass loaded on PlayerA via `-Dmakemagic.driverA=<FQCN>`. It owns ONLY the
decisions CP7 misplays and defers everything else to `super.*`. **This fork is
stripped/refactored vs upstream XMage — verify signatures against the shipped template's
helper block, not public XMage docs.** Base types live under `mage.*`; the idiom is
fully-qualified `mage.*` refs in method bodies so the import surface stays empty.

## Composing a novel line

The 8 seed patterns in the skill are **examples**, not a menu. When none fits the deck's
line, compose a new one from the primitives below — cast-from-hand (§A), the card-name
Choice steer (§B), and tutor-search assembly (§C) — plus the inherited helpers. There is no
"unsupported" line: any authored `priority`/`choose`/`chooseTarget` override is validated by
**behavior**, not by matching a template. The gate (declared-intent fired + never-worse than
bare CP7) is the contract; if your line declares its intent and does not regress the goldfish,
it is a legal driver regardless of which primitives it stitched together.

The template (`_DRIVER_TEMPLATE`) already supplies the ctor, copy ctor, the
`declareIntent(tag)` scaffold, and these **inherited helpers** — compose from them instead
of re-deriving:
- `findMine(game, name)` → my battlefield `Permanent` of that name, or null.
- `isMyPriority(game)` → I currently hold priority in the real game.
- `onMyMainEmptyStack(game)` → my priority, on either main phase, empty stack.
- `activatableByRule(game, sourceId, rulePrefix)` → first currently-activatable ability
  from that source whose rule text starts with the prefix (uses `getPlayable(game, true)`;
  matches by source + rule, **never by list index** — order is not stable).
- `steerTarget(target, source, game, id)` → guarded `canTarget`→`addTarget`; returns
  whether it was added (so you fall through to `super` when it wasn't).
- `declareIntent(tag)` → emits `DRIVER_INTENT tag=<tag> name=<name>` to stderr the first
  time each owned decision fires; the gate asserts every declared tag. Call it at the point
  the owned line actually executes.

---

## Overridable decision hooks (the ones a fill uses)

All declared on the `Player` interface; extend CP7 so `super.<x>` reaches CP7 → CP6 → CP.

| Hook | When the engine calls it | Fill use |
|---|---|---|
| `boolean priority(Game game)` | Every time this player receives priority (real game). | **The primary hook.** Guard on `isMyPriority` + step + empty stack, execute the line via `activateAbility`, else `return super.priority(game)`. Never an "own-main ⇒ never defer" branch. |
| `boolean chooseTarget(Outcome, Target, Ability source, Game)` | Choosing targets for a spell/ability going on the stack. | Steer ONLY your line's ability — match `source` name/`getSourceId()` + `outcome`, then `steerTarget(...)`; else `super`. |
| `boolean choose(Outcome, Target, Ability, Game)` | Non-target choose dialogs — **discard resolves here** (`Outcome.Discard`), **sacrifice too** (`Outcome.Sacrifice`). | Match on `outcome` + the card/permanent name, `steerTarget`, else `super`. No dedicated `chooseDiscard` hook in this fork. |
| `boolean chooseUse(Outcome, String message, Ability, Game)` | Any yes/no "use X?" prompt. | **Default is YES** (`outcome != AIDontUseIt`), so optional "may" abilities fire aggressively. Return false to suppress one that derails the line; match on `message`/`source`. |
| `boolean chooseMulligan(Game game)` | Mulligan each hand. | Light land-window + key-piece keep; return false to keep. Visible info only. |
| `boolean playMana(Ability, ManaCost unpaid, String, Game)` | Paying a cost when multiple sources exist. | High-risk; override only to force a specific tap order. Prefer to leave alone. |

**Never override:** `selectAttackers` / `selectBlockers` (CP6 impls) — the force-attack
HARD RULE. `chooseBlockerOrder` / `chooseAttackerOrder` **do not exist in this fork** —
don't emit them.

---

## Read/query + action helpers (beyond the inherited block)

Read: `game.getTurnStepType()` → `mage.constants.PhaseStep` (`PRECOMBAT_MAIN`,
`POSTCOMBAT_MAIN`, `DECLARE_ATTACKERS`, …); `game.getStack()` (`.isEmpty()`, iterable of
`StackObject`); `game.getBattlefield().getAllActivePermanents(getId())` (my permanents);
`game.getPermanent(uuid)`; `game.getObject(sourceId)` (name of a spell's source);
`game.getOpponents(getId())`; `getPlayable(game, true)` (**hidden=true** — enumerate
castable/activatable abilities; `false` under-reports); `hand.getCards(game)`;
`getGraveyard().getCards(game)`; `getManaPool().getManaCount()`; `p.getCounters(game)
.getCount(mage.counters.CounterType.P1P1)`; `p.isCreature(game)` / `isLand(game)`;
`p.isControlledBy(getId())`; `card.getName()` / `getManaValue()`.

Act: `activateAbility(ActivatedAbility, game)` — **the workhorse**; put an ability on the
stack, returns success (check it before `declareIntent`). The engine then invokes your
`chooseTarget`/`playMana` for that ability.

Executing-a-line idiom (from Mikaeus): iterate `getPlayable(game, true)`, `continue` unless
`a.getSourceId().equals(myPermanent.getId())`, match by `a.getRule()` prefix, then
`if (chosen != null && activateAbility(chosen, game)) { declareIntent(tag); return false; }`
and `return super.priority(game)` on the fall-through (ability momentarily gone → let CP7
advance triggers/SBAs, you get priority back and resume).

---

## Novel-line primitives (worked, copy-pasteable)

Grounded in this fork's `Mage.Player.AI/.../ComputerPlayer.java` (CP) and
`Mage.Player.AI.MAD/.../ComputerPlayer6.java` (CP6). A `Driver extends CP7 extends CP6
extends CP`, so `super.<x>` from a driver reaches **CP6**; CP6 delegates to CP when its own
answer-list is empty (see §B/§C).

### §A — Cast a spell FROM HAND

`getPlayable(game, true)` (PlayerImpl:4288, **hidden=true**) enumerates hand `SpellAbility`s
as `ActivatedAbility`s (PlayerImpl:4312 `if (hidden && fromZone.match(Zone.HAND))` →
`isPlaySpell = ability instanceof SpellAbility`). `activateAbility(ability, game)`
(PlayerImpl:1666) **casts** such a spell — the same call that activates a permanent ability.
Difference vs the Mikaeus idiom: `findMine` only sees battlefield `Permanent`s, so it can
never surface a card still in hand; a castable spell comes from `getPlayable`, and you match
it by the **hand card's name** via `game.getObject(a.getSourceId()).getName()`, not by
`findMine`.

```java
@Override
public boolean priority(mage.game.Game game) {
    if (onMyMainEmptyStack(game)) {
        for (mage.abilities.ActivatedAbility a : getPlayable(game, true)) {
            mage.MageObject src = game.getObject(a.getSourceId());
            if (src != null && "Kenrith, the Returned King".equals(src.getName())
                    && a instanceof mage.abilities.SpellAbility) {
                if (activateAbility(a, game)) {   // this CASTS the spell from hand
                    declareIntent("cast_kenrith");
                    return false;
                }
            }
        }
    }
    return super.priority(game);              // never pass(game)
}
```

### §B — The `choose(Outcome, Choice, Game)` card-name steer

Signature: `boolean choose(Outcome, mage.choices.Choice, Game)` — **CP:785**, overridden at
**CP6:878**. `super.choose(...)` from a driver hits CP6:878, which falls through to CP:785
when CP6's `choices` answer-list is empty (CP:785 then does creature-type / mana-color / random
handling). Use it to answer a "name a card" dialog (Demonic Consultation, etc.): inspect
`choice.getChoices()` and `choice.setChoice(name)` with a card the deck does **not** run, so
the reveal-until-named exiles the whole library.

```java
@Override
public boolean choose(mage.constants.Outcome outcome, mage.choices.Choice choice,
                      mage.game.Game game) {
    // Demonic Consultation "name a card" — free choice, not a fixed option list
    if (choice.getMessage() != null && choice.getMessage().startsWith("Name a card")) {
        choice.setChoice("Storm Crow");       // not in this deck → mills the library
        declareIntent("consult_name_storm_crow");
        return true;
    }
    return super.choose(outcome, choice, game);   // reaches CP6:878 → CP:785
}
```

Note: a fixed-option dialog constrains you to `choice.getChoices()` (`.contains(name)` before
`setChoice`); a true "name a card" dialog is free-form and takes any string.

### §C — TUTOR-search assembly (cast-from-hand + steer the search)

The load-bearing new idiom: **own** the assembly. Cast a tutor from hand (§A), then steer the
library search it puts on the stack to fetch a **named missing combo piece**. A tutor's search
resolves through the `Cards`/`TargetCard` overloads, not the `Target` ones:
- `chooseTarget(Outcome, mage.cards.Cards, mage.target.TargetCard, Ability, Game)` — **CP:893**,
  overridden **CP6:891**.
- `choose(Outcome, mage.cards.Cards, mage.target.TargetCard, Ability, Game)` — **CP:898**,
  overridden **CP6:911**.

Both CP6 overrides delegate to CP (`if (targets.isEmpty()) return super....`) when CP6 has no
queued answer, so a driver override that adds its own target wins outright. Match by the
**source being your tutor**, enumerate the offered `Cards`, and `target.addTarget(pieceId,
source, game)` the named piece if present; else `super`.

```java
@Override
public boolean chooseTarget(mage.constants.Outcome outcome, mage.cards.Cards cards,
                            mage.target.TargetCard target, mage.abilities.Ability source,
                            mage.game.Game game) {
    mage.MageObject s = game.getObject(source.getSourceId());
    if (s != null && "Diabolic Tutor".equals(s.getName())) {
        for (mage.cards.Card c : cards.getCards(game)) {   // getCards → Set<Card>
            if (c != null && "Thassa's Oracle".equals(c.getName())) {
                target.addTarget(c.getId(), source, game);   // fetch the missing piece
                declareIntent("tutor_for_thassas_oracle");
                return true;
            }
        }
    }
    return super.chooseTarget(outcome, cards, target, source, game);  // CP6:891 → CP:893
}
```

Cast the tutor in `priority` via §A; steer its fetch here. Together they let a driver
**assemble** a combo (tutor → fetch → precast) rather than wait for CP7 to draw the piece.

---

## Why a driver must OWN assembly (goldfish caveat)

A solo goldfish runs the *base* AI on the opponent and, absent a driver, on the piloted deck
too. The base AI will not proactively tutor for a missing piece and precast a multi-spell
sequence to set up a combo — it evaluates spells locally, not as a plan. So a combo that
relies on the base AI to **assemble** it (tutor + hold + sequence) simply won't fire in a
goldfish, and the deck under-performs its true ceiling. That is exactly why the driver should
own assembly (§C) instead of deferring it: the driver encodes the plan the base AI lacks.

`ComputerPlayer6.createSimulation` replaces every player in a simulated game with a
`SimulatedPlayer2` — **the driver object does not exist inside any rollout**. So your
overrides run ONLY when the *real* game calls them on the *real* PlayerA; you literally
cannot corrupt the search by overriding a decision method.

`CP7.copy()` returns a plain `ComputerPlayer7`, not a `Driver` — intentional. If a driver
overrode `copy()` to return `Driver`, it would leak driver-typed players into nested game
copies where the SP2 swap is not re-applied, and its overrides could fire mid-search.
**Do not override `copy()`.** Provide only the copy ctor `Driver(final Driver d){ super(d); }`
the template already has. The behavioral gate rejects a rendered driver that overrides
`copy()` or `selectAttackers`.

---

## Gotchas (the source of most `phase:compile` and `phase:intent-not-fired` failures)

- **Wrong package** — types are `mage.*` (e.g. `mage.game.permanent.Permanent`,
  `mage.abilities.ActivatedAbility`, `mage.constants.Outcome`, `mage.counters.CounterType`).
  Use fully-qualified refs to keep `imports` empty and avoid resolution errors.
- **`super.<x>` reaches the wrong impl** — `choose(Outcome,Choice,Game)` and the
  `Cards`/`TargetCard` choose/chooseTarget are overridden in BOTH CP and CP6; `super`
  reaches the **CP6** version. `selectAttackers`/`selectBlockers` are CP6's.
- **`chooseUse` default is YES**, not no — an optional trigger that breaks your loop must be
  explicitly suppressed with `return false`.
- **`priority()` deferral must `return super.priority(game)`**, never `pass(game)` (void,
  skips CP7's per-step switch).
- **`getPlayable(game, true)`** — pass `hidden=true`; `false` misses hand/not-yet-obvious
  options. Match abilities by `getSourceId()` + `getRule()` prefix, never by index.
- **Guard `steerTarget`'s return** — add a target only when it returns true, else fall
  through to `super`; never hand the engine an illegal/empty choice.
- **Names must match exactly** — an `intent-not-fired` failure is often just a
  card-name/rule-prefix typo, or a guard threshold (mana reserve, board width) the goldfish
  window never reaches. Verify the exact `card.getName()` and the ability's `getRule()`
  prefix from the deck before loosening anything else.
- **Test-mode short-circuits `chooseMulligan`** — know the baseline the harness runs.
