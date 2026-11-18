# 0025 Event granularity matches the CR mechanic boundary: a keyword's grant FOLDS into its nearest tracked event (never its own event); distinct resources SPLIT into their own events

> **Two complementary halves of one doctrine — "event granularity = the CR mechanic boundary."**
> **(A) Grant/use (fold):** a keyword or static ability is never its own event; model its grant as a
> producer of the nearest bounded CR event its *use* manifests as. The original decision; `deathtouch-
> combat` collapse is the worked example.
> **(B) Resource (split):** a node that lumps **distinct CR resources** (each with its own producers
> AND consumers) is too coarse — split it, one event per resource. Added 2026-11-18 (Batch A);
> `player-counter → energy / rad / experience` and `spell-cast → spell-cast / spell-copied /
> spell-on-stack` are the worked examples. The one test for both halves: **does the thing have its own
> producers/consumers as a distinct CR mechanic?** No → fold it into the event it manifests as. Yes →
> it earns (or keeps) its own event.

| Field | Value |
|---|---|
| Status | **Accepted as the modeling principle; pending owner ratification.** Owner instruction (verbatim): *"Check Shelob, if it clears then yes yes simplify and capture in ADRs."* Shelob cleared (PRESERVED + IMPROVED), so the one violator — `deathtouch-combat` — was collapsed. The catalog was already consistent for every other keyword mechanic (29 of 30 events); this record states the rule and records the single application. |
| Review | Agent-authored 2026-11-18 from the grant-vs-use consistency audit (`research/grant-vs-use-event-modeling.md`): a read of how all 30 events handle the grant ("gets the keyword") vs use ("the ability resolves into an effect") distinction. Owner-adjudicated live via the Shelob gate. **Pending owner ratification**, as with 0023 / 0024. |
| Test | Review-enforced today; mechanical home is the WS3 catalog-integrity layer (`catalog_integrity.py`): a keyword-USE node whose use already corresponds to a tracked event is the next flag to add there (the role-consistency linter already catches the adjacent payoff-as-producer inversion). Parity: the `deathtouch-combat` collapse (30 → 29) and the Batch A splits (29 → 33) are each a proof-carrying re-baseline (`engine_parity_oracle` + `test_event_ontology` + `test_adjacency`) under [0003]. |
| Code paths | [`event_catalog.json`](../../pipeline/pipeline/transforms/strategy/data/event_catalog.json) (fold: `deathtouch-combat` removed; split: `player-counter` removed → `energy`/`rad`/`experience`, `spell-cast` → `spell-cast`/`spell-copied`/`spell-on-stack`), [`derive_card_events.py`](../../pipeline/pipeline/transforms/strategy/derive_card_events.py) (`SEED_MAP`), [`distill_strategies.py`](../../pipeline/pipeline/transforms/strategy/distill_strategies.py) (the `deathtouch` archetype *identity*; the `experience-counter` synth attr), [`combo_win.py`](../../pipeline/pipeline/transforms/strategy/combo_win.py) (`EVENT_EDGE_CLASSES` + `fid_route` — split re-routing), [`fid_event_adapter.json`](../../pipeline/pipeline/transforms/strategy/data/fid_event_adapter.json) (split fid re-routes). |

## Context

Three things are easy to conflate for any keyword mechanic, and the taxonomy already gives distinct
homes to the first two:

| Concept | Otag family (live) | Role in the engine |
|---|---|---|
| **Grant** ("gets keyword X") | `gives-X` (to others), `gains-X` (to self), `X-counter` | **producer** otag — the cause |
| **Characteristic-synergy** ("cares a creature HAS X") | `synergy-X`, `X-matters` | **consumer** otag — a payoff on a static property |
| **Use** ("ability X resolves into an effect") | — *(no otag; it is a game moment)* | an **Event** node, if one exists in the CR taxonomy |

The audit mapped all 30 events against this distinction. The catalog was **consistent for every
keyword except one**:

- `lifelink` → its grant produces `lifegain` (no `lifelink-combat` node).
- `double-strike` / `trample` / `evasion` → their grants produce `combat-damage-dealt`.
- `haste` → its grant produces `creature-attacks` ("can attack now" = the attack event).
- `infect` / `toxic` / `poison` → their grants produce the `poisoned` state-event (CR 104.3d SBA),
  which reaches `game-end` ([0024]).
- `prowess` → a *consumer* of `spell-cast`, never a grant/use node.
- `deathtouch` → its own standalone node `deathtouch-combat`, produced by the deathtouch granters and
  consumed by `synergy-deathtouch` + the tiny `old-*-deathtouch` payoffs. **The single over-model.**

`deathtouch` is the same *shape* of keyword as double-strike/trample/evasion — a static combat-damage
modifier ("any damage is lethal") with the same otag shape (`gives-deathtouch`/`gains-deathtouch` +
`synergy-deathtouch`) — yet it alone earned a node. Its own catalog text called it "a combat/removal
**pseudo-event**." The `deathtouch-combat.triggers` list even carried `combat` (a producer-shaped
umbrella used as a consumer), the same role-inversion class [0022]/R3 fixed on
`recursion-from-graveyard`.

## Decision

### The principle

> **A keyword or static ability is never its own event.** Model its grant (`gives-X` / `gains-X` /
> `X-counter`) as a **producer of the bounded CR trigger-or-state event that the keyword's use
> manifests as** — the nearest real event in the catalog. A keyword earns a **separate node only when
> its use is itself a bounded CR trigger-or-state event that no existing event already represents.**
> Honor the keyword-as-property payoff (`synergy-X` / `X-matters`) at the **attribute/archetype
> layer** ([0012] mechanic-class clause), not by minting an event.

This is **[0024]'s own test generalized from wincons to all mechanics**: *"does the [use] already
correspond to a tracked event? If yes, consume/produce it; if not, and it is a reusable game STATE,
name the state-event."* It is the same reasoning that **DROPPED `life-threshold`** (the existing
`lifegain` event already owns the win) applied to keywords: for a static combat keyword the use always
lands on an event that already exists (`combat-damage-dealt` / `creature-attacks` / `permanent-dies` /
`lifegain` / `poisoned`), so there is never a new node to mint.

### How each keyword classifies under the rule

| Keyword | Use manifests as | Under the rule |
|---|---|---|
| lifelink | `lifegain` (exists) | producer of lifegain — already consistent |
| double-strike / trample / evasion | `combat-damage-dealt` (exists) | producers of combat-damage-dealt — already consistent |
| haste | `creature-attacks` (exists) | producer of creature-attacks — already consistent |
| infect / toxic / poison | `poisoned` state (exists) | producer of the state-event — already consistent |
| prowess | `spell-cast` (consumer) | consumer, no grant node — already consistent |
| **deathtouch** | `combat-damage-dealt` (lethal combat damage) **and** `permanent-dies` (it kills what it fights) — both exist | producer of both; **no own node** — **RESTRUCTURE** |
| protection / hexproof / ward / indestructible / shroud / vigilance / defender | no tracked event (prevention / anti-event) | stay unmodeled at the event layer — already consistent |

### The defensible alternative, and why not

**Rule B — "separate when a distinct `synergy-X` consumer exists"** (the implicit `deathtouch-combat`
rationale: give the keyword its own node so `gives-X` producer and `synergy-X` consumer converge into
one engine unit). Rejected because (a) a static keyword is not a bounded CR trigger ([0012]); (b)
applied consistently it mints ~20 keyword-use events (`synergy-flying` alone is 116 cards, far more
than `synergy-deathtouch`'s 30) — exactly the per-concept proliferation [0023]/[0024] guard against;
(c) the characteristic-synergy is a property pairing [0012] already routes to the archetype layer.
Both rules agree on the only thing that matters — **`deathtouch-combat` should not be a standalone
event** — differing only on *where* the keyword-tribal payoff is honored (Rule A: the archetype layer,
where it already lives; Rule B: a new event, which is slop). Rule A is the decision.

## The worked application — collapse `deathtouch-combat` (2026-11-18)

Realized as a proof-carrying re-baseline under [0003]:

- **Removed** the `deathtouch-combat` event from `event_catalog.json` + its `SEED_MAP` entry. Event
  count **30 → 29**.
- **Rewired the grants** (`deathtouch-counter`, `gains-deathtouch`, `gives-deathtouch`) as producers
  of **`combat-damage-dealt`** (the combat-damage keyword, matching double-strike/trample/evasion)
  **and `permanent-dies`** (deathtouch damage is lethal → it kills what it fights — the Shelob
  death→food signal). `gives-deathtouch`'s closure already carries `gives-deathtouch-noncreature`
  (R5a), so no separate seed is needed.
- **The synergy payoff moved to the archetype layer** — `synergy-deathtouch` and the small
  `old-blocking-deathtouch` (35) / `old-damage-deathtouch` (23) payoffs are keyword-tribal
  characteristics, already carried by the `deathtouch` distill identity (`distill_strategies.py`, R5a:
  `gains-/gives-deathtouch OR deathtouch-counter OR synergy-deathtouch OR old-*-deathtouch …`). They
  are **dropped from the event layer**, not rehomed as event triggers — the rule's point. The
  `deathtouch` archetype identity is byte-unchanged.
- **`EVENT_EDGE_CLASSES` unchanged** — `deathtouch-combat` was never a key (it had no win-edge), so no
  rule-edge moves.

**The Shelob gate (the decision criterion).** Measured OFF (`a696bd0`, with `deathtouch-combat`) vs ON
(collapsed) on the Shelob fixture `owned__spiders-play-with-their-food`: **PRESERVED + IMPROVED.** The
`deathtouch-combat` node never elected on Shelob OFF (it was engine-inert — support never reached the
[0004] admission bar); Shelob's deathtouch always lived in the distill archetype identity. ON, the
deathtouch grants become producers of Shelob's existing `permanent-dies` aristocrats engine
(mass **68 → 74**, `gives-deathtouch` joins its producer set) — the "deathtouch kills what it fights →
death → food/aristocrats" signal is now an honest producer edge instead of a dead isolated node.
Shelob's `archetype_identity` / `ranked_identity` / `pre_combo_name` (aristocrats/sacrifice) /
`bracket` (3) / `confidence` (LOW) are **byte-identical** OFF→ON.

**The other deathtouch decks.** Full 170-deck gauntlet, exactly **2 records moved** (both
graveyard/reanimator, n_nodes invariant, 0 pre_combo / bracket / confidence flips):
- **Nethroi** — `permanent-dies` engine strengthens (mass **49 → 54**, gains `gives-deathtouch`);
  identity unchanged. IMPROVED.
- **Kathril** — the **only** deck where `deathtouch-combat` had elected as a node OFF (a mass-38
  standalone engine: grant producers + `synergy-deathtouch`/`old-*` consumers — the self-contained
  keyword-tribal loop this rule rejects). ON, that pseudo-engine is gone; Kathril's four real engines
  (card-enters-graveyard, counter-placed, creature-enters-battlefield, land-enters-battlefield) and
  its `deathtouch` archetype identity (**#3, share 0.275 — byte-identical**) and headline/bracket/
  confidence are all preserved. The removed engine unit **is** the collapse target, not a regression.

Zero spurious new leads anywhere; the only engine-unit removed is the intended pseudo-engine. A
cleanup bonus: the `combat` role-ambiguity advisory in `validate_catalog()` (produced-by seed of
combat-damage-dealt/creature-attacks AND a triggers seed of deathtouch-combat) is gone.

## Decision — the resource side (the split half; added 2026-11-18, Batch A)

The grant/use half folds things that are **not their own mechanic** (a keyword is a property, not a CR
event). The mirror-image failure is a node that is **too coarse**: one event standing in for several
**distinct CR resources**, each with its own producers and consumers. The doctrine's second half:

> **Distinct resources split into their own events.** When one event node lumps mechanics that are
> separate CR resources — each with (a) its own producer concept and (b) its own consumer/payoff —
> split it into one event per resource. The granularity test is the same as the fold half, read the
> other way: *does this thing have its own producers/consumers as a distinct CR mechanic?* If **yes**,
> it earns its own event (don't lump it under a generic node); if **no**, fold it. A generic node like
> `player-counter` ("any counter on a player") or `spell-on-stack` used as a catch-all **dilutes every
> engine built on it** — a producer of resource X forms spurious edges to a payoff for resource Y that
> it never actually fuels. Splitting removes the cross-resource over-promise at the structural layer,
> the same way the grant/use fold removes the keyword-tribal pseudo-engine.

This is **[0024]'s state-event reasoning run forward**: [0024] names a *new* state-event when a reusable
game state has no tracked home; this clause *subdivides* an existing node when it secretly holds several.
Both keep **one event = one bounded CR mechanic**.

### The worked example — `player-counter → energy / rad / experience` (A2)

The former `player-counter` node ("a counter on a player") lumped **three unrelated CR resources**:
**energy** (CR 122.1a; producer `energy-generator` 137, consumer `counter-fuel-energy`), **rad**
(Fallout; producer the text-synth `rad-counter`, consumer self-mill `mill-*`), and **experience**
(C15+; producer the NEW text-synth `experience-counter`, consumer = the card's own scaling ability).
An energy producer has nothing to do with a rad self-mill payoff, yet on the shared node every
energy-generator formed a (dead-but-present) edge to every rad/experience consumer, and — worse — the
walk let an infinite-**energy** combo "win" through `altwin::player-counter` (Azor's Elocutors, a
**filibuster**-counter card that is none of the three). Split into `energy` / `rad` / `experience`,
each resource's producers reach only its own consumers; `proliferate` (it adds to *any* counter a
player has) is the one **shared** producer seeded into all three. `altwin::player-counter` is now
unmapped (walk-inert) — filibuster has no producer concept among the three, so the spurious
energy→filibuster win is gone (a future filibuster event would re-map it, mirroring poison/domain).

### The worked example — `spell-cast → spell-cast / spell-copied / spell-on-stack` (A1)

A cast (CR 601) and a copy (CR 707.10/.12) are **distinct stack events**: a copy is **not** cast, so it
must not reach the cast-only payoffs (storm, prowess), yet `magecraft` (CR 702.157a, "cast **or** copy")
sees both. The old `spell-cast` lumped copy producers (`copy-*`) with cast producers and let copies
reach storm/prowess. Split: **`spell-cast`** keeps the real cast-generators (`impulse-cast`,
`free-cast-another`) and the cast payoffs; **`spell-copied`** owns the `copy-*` producers (no copy-only
payoff otag exists, so its triggers are empty); **`spell-on-stack`** is the **union** (cast ∪ copy) and
the `magecraft` home — a copy engine lands on spell-copied + spell-on-stack and sees magecraft there
**without** over-promising storm/prowess. (This is also why a resource can need a *union* node, not only
disjoint ones: magecraft is a real payoff on the superset.)

## The Batch A worked application — the two splits (2026-11-18)

Realized as a proof-carrying re-baseline under [0003], event count **29 → 33** (6 new:
`spell-cast`[redefined] / `spell-copied` / `spell-on-stack`, `energy` / `rad` / `experience`; 2
removed: old `spell-cast`, `player-counter`). `combo_win.py`: `EVENT_EDGE_CLASSES['spell-cast']` keeps
`cast_drain` + `altwin::spell-cast` (Approach wins on the second **cast**); `spell-on-stack` gets
`cast_drain` only (magecraft drains on cast-or-copy, but you cannot cast Approach by copying);
`spell-copied` has no win-edge; `energy`/`experience` have none (an infinite resource is not itself a
win); `rad` routes self-deplete in `fid_route`; `altwin::player-counter` unmapped. `fid_event_adapter`:
7 magecraft/copy fids → `spell-on-stack`, energy fids (317/2018) → `energy`, experience fids
(1179/2646) → `experience`. The `experience-counter` synth (`oracle:'experience counter'`, 19 members —
all 5 original C15 commanders + 11 later + 3 tokens/land, **0 FP**) distills to `None` (edge-less, like
`rad-counter`/`monarch-maker`).

**Measured OFF (`34b2a08`) vs ON (157 gauntlet + 17 fixtures).** **0** pre_combo_name / bracket /
confidence / archetype_identity / ranked_identity / combo_identity flips across all 157 gauntlet decks;
**1** benign `n_nodes` +1 (`simic__ezuri`, now carries the `experience-counter` attr node). 10/157
gauntlet engine relocations, **all** `spell-cast ↔ spell-copied/spell-on-stack`: genuine cast decks
(carrying `impulse-cast`/`free-cast-another`) **KEEP** `spell-cast`; pure-copy decks correctly drop the
mis-attributed copy→storm/prowess engine; copy+magecraft decks (veyran, kalamax, JeskaiStriker, shiko)
relocate to `spell-on-stack` (copy→magecraft). **0** `player-counter` engines existed in the gauntlet
(A2's structural impact there is nil; its effect is the combo-walk re-route + the Ezuri attr). Exactly
**1/17** fixtures moved — `owned__the-fantastic-four`, a spellslinger that **keeps** `spell-cast` via
`free-cast-another` with its copies relocated; `pre_combo_name`=spellslinger / bracket 3 / MED
preserved. 0 spurious new engines (every `spell-on-stack` appearance is a real copy+magecraft deck; the
only engines removed were the mis-attributed copy→cast-payoff ones). `validate_catalog()` 0 blocking.

## Consequences

- `deathtouch-combat` is removed (fold) and `player-counter` / old `spell-cast` are split; the catalog
  is now **33 events**, each one bounded CR mechanic. The fold half keeps keywords off the event layer;
  the split half keeps distinct resources off a shared generic node. Both are the one doctrine:
  event granularity = the CR mechanic boundary.
- `synergy-deathtouch` loses its (only) event home; the `deathtouch` distill identity is its correct
  and unchanged home.
- Three parity SHAs re-baselined by the deathtouch fold (the 17 hermetic fixtures carry the
  deathtouch-granting family via Shelob): `FULL_CORPUS 855edd45… → ab1a6074…`,
  `ENGINE 45490628… → f4b98a79…`, `RESOLVE be2981c3… → 7b1245a1…`. See [0003].
- Three parity SHAs re-baselined again by the Batch A splits (the fantastic-four fixture carries the
  copy/cast family): `FULL_CORPUS ab1a6074… → 03a5880c…`, `ENGINE f4b98a79… → 7a83891a…`,
  `RESOLVE 7b1245a1… → a63f268b…`. See [0003].
- **Optional, consistent-direction, NOT done here** (owner-graded coverage, deferrable): widening
  `combat-damage-dealt.produced_by` to the specific evasion/damage grants (`gives-flying`,
  `gives-menace`, `gives-unblockable`, `gives-first-strike`) beyond the generic `gives-evasion`. This
  is an under-coverage fill in the rule's direction, not a grant-vs-use inconsistency.

## Related

- **[0012]** — Events are the game's bounded CR trigger taxonomy; **mechanic-class is a CardAttribute,
  not an Event** (the clause this rule generalizes). Event count 30 → 29 is amended there.
- **[0024]** — the "does it already correspond to a tracked event?" test + the `life-threshold` drop
  this record extends from wincons to all keyword mechanics.
- **[0020] / [0022]** — event producers + producer-anchored substantiveness: a keyword grant's new
  home is an ordinary `SEED_MAP` producer seed.
- **[0023]** — anti-proliferation / name-by-what-it-is (a `*-combat` pseudo-event is an authoring
  artifact, not a CR event).
- **[0003]** — the proof-carrying re-baseline this collapse carries.
