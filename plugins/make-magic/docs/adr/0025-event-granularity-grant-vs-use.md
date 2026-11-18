# 0025 Event granularity: a keyword's grant produces its nearest tracked event; a keyword is never its own event

| Field | Value |
|---|---|
| Status | **Accepted as the modeling principle; pending owner ratification.** Owner instruction (verbatim): *"Check Shelob, if it clears then yes yes simplify and capture in ADRs."* Shelob cleared (PRESERVED + IMPROVED), so the one violator — `deathtouch-combat` — was collapsed. The catalog was already consistent for every other keyword mechanic (29 of 30 events); this record states the rule and records the single application. |
| Review | Agent-authored 2026-11-18 from the grant-vs-use consistency audit (`research/grant-vs-use-event-modeling.md`): a read of how all 30 events handle the grant ("gets the keyword") vs use ("the ability resolves into an effect") distinction. Owner-adjudicated live via the Shelob gate. **Pending owner ratification**, as with 0023 / 0024. |
| Test | Review-enforced today; mechanical home is the WS3 catalog-integrity layer (`catalog_integrity.py`): a keyword-USE node whose use already corresponds to a tracked event is the next flag to add there (the role-consistency linter already catches the adjacent payoff-as-producer inversion). Parity: the `deathtouch-combat` collapse is a proof-carrying re-baseline (`engine_parity_oracle` + `test_event_ontology` + `test_adjacency` 30 → 29) under [0003]. |
| Code paths | [`event_catalog.json`](../../pipeline/pipeline/transforms/strategy/data/event_catalog.json) (the `deathtouch-combat` node removed; `combat-damage-dealt` / `permanent-dies` producer lists), [`derive_card_events.py`](../../pipeline/pipeline/transforms/strategy/derive_card_events.py) (`SEED_MAP` — the deathtouch grants reseeded onto combat-damage-dealt + permanent-dies), [`distill_strategies.py`](../../pipeline/pipeline/transforms/strategy/distill_strategies.py) (the `deathtouch` archetype *identity* — the correct, unchanged home of the keyword-tribal payoff), [`combo_win.py`](../../pipeline/pipeline/transforms/strategy/combo_win.py) (`EVENT_EDGE_CLASSES` — `deathtouch-combat` was never a key; unchanged). |

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

## Consequences

- `deathtouch-combat` is removed; the catalog is now consistent for **all 29 events** (4 keyword-
  collapsed hosts + 1 keyword-consumer + 24 keyword-free). The rule is already how the catalog behaves
  everywhere; this record names it so the next keyword is modeled uniformly.
- `synergy-deathtouch` loses its (only) event home; the `deathtouch` distill identity is its correct
  and unchanged home.
- Three parity SHAs re-baselined (the 17 hermetic fixtures carry the deathtouch-granting family via
  Shelob): `FULL_CORPUS 855edd45… → ab1a6074…`, `ENGINE 45490628… → f4b98a79…`,
  `RESOLVE be2981c3… → 7b1245a1…`. See [0003].
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
