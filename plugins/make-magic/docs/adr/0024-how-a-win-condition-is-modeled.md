# 0024 How a win condition is modeled: a wincon is an event that reaches game-end; its enablers are the producers of that event

| Field | Value |
|---|---|
| Status | **Accepted as the modeling principle; pending owner ratification.** Realized: `game-end`, `library-depleted`, `poisoned` (through `1e77c18`), and now **`domain`** (CR 104.2a; produced_by the authored `color-fixing` CardAttribute, triggers the authored `five-color-win` consumer; event count 29 → 30) are state-events of this shape. **`life-threshold` was specced here but DROPPED** on realization: the existing `lifegain` event already owns the lifegain-win end-to-end (`altwin::lifegain` = Felidar/Angel/Test + the `life-total-matters-self`/`lifegain-matters` payoff triggers), so a `life-threshold` event would double-count — it is subsumed, not added. See the realization amendment below + ADR 0003. |
| Review | Agent-authored 2026-11-17 from the session's measurements — `research/win-walk-completeness.md` (the walk is an infinite-closure detector; thresholds/states are inexpressible there) and `research/altwincon-decomposition-and-modeling.md` (the `alt-wincon/combo` catch-all drops every enabler→win link). Owner-adjudicated live. **Pending owner ratification**, as with 0023. |
| Test | Review-enforced today. Mechanical enforcement is the WS3 catalog-integrity layer (`ws3-integrity-system-design.md`): the recurring CR audit flags a wincon modeled in the wrong home (a threshold/state wincon wired as a walk edge; a catch-all wincon identity), and the dead-ref validator covers the producer seeds. **Honest gap:** until those land, this is review-enforced — same posture as 0001 clause 4 / 0023. Parity: any wiring of a new state-event is a proof-carrying re-baseline (`engine_parity_oracle` + `test_event_ontology`). |
| Code paths | [`event_catalog.json`](../../pipeline/pipeline/transforms/strategy/data/event_catalog.json) (`game-end`, `library-depleted`, `poisoned`; `domain`/`life-threshold` to be added), [`combo_win.py`](../../pipeline/pipeline/transforms/strategy/combo_win.py) (`EVENT_EDGE_CLASSES`, the `altwin::<event>` rule-edges + `combo_win_verdict` — the combo producer), [`derive_card_events.py`](../../pipeline/pipeline/transforms/strategy/derive_card_events.py) (`SEED_MAP` — the producer seeds of each wincon event), [`distill_strategies.py`](../../pipeline/pipeline/transforms/strategy/distill_strategies.py) (the archetype *identity* that names the plan — **not** the enabler→win link). |

## Context

A "win condition" is not one kind of thing, and the engine has modeled only one kind honestly.
Over the 2026-11-17 session two measurements exposed the gap:

- The combo **win-walk** (`combo_win.py`, ADR 0016) is an **infinite-closure detector** — it fires
  when an *unbounded* event reaches `game-end`. It has no magnitude arithmetic and no
  source-identity. "40 life", "20 artifacts", "10 poison", "21 commander damage", "a land of each
  basic type" are **inexpressible** there: they are not infinite loops. Forcing one in manufactures
  false wins (mapping Coalition Victory to `altwin::token-created` would call *any* infinite-token
  deck a winner — `research/win-walk-completeness.md`).
- The distill layer carried a **catch-all** identity `alt-wincon/combo`
  (`otag:wincon OR otag:alternate-win-condition`) that collapsed all 87 alternate-win cards into one
  identity and **dropped every enabler**. The owner's question — *"how is land-fetch → Coalition
  Victory represented?"* — had the honest answer **nowhere**
  (`research/altwincon-decomposition-and-modeling.md`).

The resolution is not a new subsystem and not a per-event parameter. The engine **already models
wincons as events**: `game-end` is the terminal event; `library-depleted` and `poisoned` are
state-events that reach it. This record states that as the single principle so the next wincon —
authored by a person or an agent — is modeled uniformly instead of by special case.

## Decision

### The principle

> **A win condition is an event that reaches `game-end`. Model its enablers as the producers of
> that event.**

One model: `producer → (state-)event → … → game-end`. There is no separate "wincon representation,"
no archetype-side mechanism for the enabler→win link, and no per-event win parameter. The distill
**archetype** still names the deck's plan (`domain`, `deckout`, `voltron`) — but the **mechanical
enabler→win link lives in the event graph**, not in the archetype. Events carry the wiring;
archetypes name the plan.

### How to find the producer (guidance, not structure)

The producer of a wincon event takes one of three shapes. This is a *decision guide* for the author,
not three different representations:

1. **An infinite loop** → the **combo win-walk** closes it (`combo_win_verdict`; the `altwin::<event>`
   classes). This is the only home with no threshold — the loop IS unbounded. (ADR 0016.)
2. **A tally of a tracked event** (life, artifacts in play, cards in a graveyard, poison counters) →
   the wincon is a **consumer** of that event, and its enablers are the event's **producers**
   (lifegain producers → the life wincon). Substantiveness is the event grain
   (producer-anchored, ADR 0020). The threshold value is a **magnitude**, not a node (below).
3. **An assembled game-state** (all five colors + all basic land types; an empty library; ten poison)
   → the state **is itself an event** — a *state-event* — that the enablers produce.
   `library-depleted` and `poisoned` are the existing precedent; `domain` (for Coalition Victory,
   produced by color-fixing) is the next one. The wincon consumes the state-event → `game-end`.

The practical test between (2) and (3): *does the win requirement already correspond to a tracked
event?* If yes, consume it (2). If not, name the reusable state as a state-event and produce it (3).

### State-events are keyed by game-state KIND, never by threshold value

Add a state-event for a **reusable game-state** — `poisoned`, `library-depleted`, `domain`,
`life-threshold`. **Never** model different thresholds of the same state as separate events: there is
one `life-threshold` event, not `life-40` / `life-50`; one `poisoned`, not `poison-10`. The specific
threshold (40 life, 10 poison) is a **magnitude** carried on the producer/edge, exactly as `poisoned`
already works (it is the poison *state*, not `poison-10`). This keeps the event catalog bounded by
the game's state kinds, not by numbers.

And add a state-event **only** for a reusable state. A one-off, card-specific condition (Biovisionary's
"four creatures named Biovisionary", a coin-flip wincon) does **not** earn an event — it is a
card-level edge or stays in the residual tail. Minting a per-card event is its own slop (the
proliferation ADR 0023 guards against).

### Anti-patterns (what this rules out)

- **A threshold or state wincon wired as a walk edge** — architecturally impossible to do correctly
  (the walk has no magnitude/state), and it manufactures false wins. (The 2026-11-17 combat
  magnitude-guard fix removed three such false wins; see 0016.)
- **A catch-all wincon identity** that names the payoff and drops the enablers (`alt-wincon/combo`).
  This is a conflation of the same family as `monarch-or-extra-turn` (ADR 0023 / the 2026-11-17 CR
  audit); decompose it so each wincon's enabler→win link is modeled as above. **Realized 2026-11-17
  (R1b):** decomposed into `deckout/thoracle` + `domain` distill identities + family riders; see the
  second realization amendment below.
- **A per-threshold event** (`life-40`) or a **per-card event** for a one-off condition.

### Relationship to the other records

- **0016** — the win-walk is Home-1's mechanism: infinite-closure only. This record states that
  boundary; 0016 owns the walk itself.
- **0020 / 0022** — event producers/consumers + producer-anchored substantiveness are the mechanism
  for Homes 2 and 3. A wincon event's producer seed is an ordinary `SEED_MAP` producer.
- **0012** — the event ontology already admits state/terminal nodes (`game-end`, `library-depleted`,
  `poisoned`), not only CR triggers; `domain`/`life-threshold` extend that set under the same rule
  (one node per state kind).
- **0002 / 0004** — substantiveness still gates whether a wincon's producers are *earned* in a deck.
- **0009 / 0017 / 0023 (principle 8)** — when no otag cleanly names a wincon-event producer (e.g.
  `color-fixing` for `domain`, a `commander-source` for commander damage), **author a CardAttribute
  query** for it (the `rad-counter` / `monarch-maker` precedent), do not seed a coarse tag.

If you add a wincon event, a state-event, or change how a wincon's producers are modeled, update this
record in the same change.

## Realization amendment — 2026-11-17 (`domain` wired; `life-threshold` dropped)

The principle was realized as a proof-carrying re-baseline (ADR 0003). Three things landed and one
was deliberately *not*:

- **`color-fixing` producer CardAttribute** — authored as a SYNTH attribute (distills to None, like
  `monarch-maker`/`rad-counter`): `oracle:'mana of any color' OR otag:rainbow-land` (709 fixers, 92%
  produce ≥3 of WUBRG; measured payoff-contamination 2/709). The `produced_mana ⊇ 3-of-WUBRG` FIELD
  form the design floated is **off-limits** — ADR 0009 forbids growing the query evaluator with a
  new `produced_mana:` atom, so the producer is this union inside the closed vocabulary. **Known
  gap:** basic-land-TYPE fixers (fetchlands, Prismatic Omen) are not covered — no otag is clean for
  them (`fetchland` is 1–2 types; `dual-land`/basic-land-search drag in generic ramp). Documented,
  deferred.
- **`five-color-win` consumer CardAttribute** — `otag:alternate-win-condition AND oracle:'each
  color'` = exactly {Coalition Victory, Call the Spirit Dragons}. The coarser
  `otag:alternate-win-condition AND otag:lifegain` was rejected (it drags in cast-count/combat/group-
  hug wincons and misses Test of Endurance).
- **`domain` state-event** — `produced_by=['color-fixing']`, `triggers=['five-color-win']`, WIN
  rule-edge `EVENT_EDGE_CLASSES['domain']=['altwin::domain']` ([0016]); mirrors `poisoned`. It is
  **engine-inert on the corpus** (no deck runs the 2 wincon cards, and the consumer — 2 cards total,
  neither a legal commander — can never reach the `determine_node_admitted` support≥3 bar, [0004]),
  exactly as `poisoned` is walk-inert. The wiring is proven live by synth-membership + derived-catalog
  match; the enabler→win link is the strategy-graph event edge `color-fixing → domain → five-color-win`,
  which is the point of this record. `altwin::domain` is `[]` (the two wincon cards stay in
  `_altwin_unmapped` until a domain fid route exists — same posture as poison fid 1800 → None).
- **`life-threshold` DROPPED.** The practical (2)-vs-(3) test in this record asks "does the win
  requirement already correspond to a tracked event?" For lifegain the answer is **yes**: `lifegain`
  is already a first-class event whose `EVENT_EDGE_CLASSES` owns `altwin::lifegain` (Felidar/Angel/
  Test) and whose triggers carry the `life-total-matters-self`/`lifegain-matters` payoffs. Adding a
  separate `life-threshold` state-event would double-count that win-edge, and the only payoff-pure
  consumer collapses to the same three cards. So the lifegain-win is modeled by **consuming the
  existing `lifegain` event** (Home 2), not by minting a new state-event. This is the record's own
  anti-duplication guidance applied to itself.

Event count 29 → 30. Measured OFF (code@`736afe0`) vs ON: 105/157 gauntlet decks moved, 0 label/
identity flips, 0 spurious leads, 0 evictions; all three parity SHAs re-baselined (ADR 0003). Proof:
`subagents/r1-realize-adr0024/outbox.md`.

## Realization amendment — 2026-11-17 (the `alt-wincon/combo` distill catch-all decomposed; R1b)

The naming half of this record ("the distill **archetype** still names the deck's plan — `domain`,
`deckout`, `voltron`") was realized as a second proof-carrying re-baseline (ADR 0003). The distill
catch-all identity `alt-wincon/combo` (`otag:wincon OR otag:alternate-win-condition`) — the
anti-pattern this record and ADR 0023 both name — is decomposed:

- **`deckout/thoracle`** — a new distill identity for the self-deplete (empty-your-own-library) win,
  keyed on a new `deckout-win` SYNTH payoff `otag:alternate-win-condition AND otag:empty-library`
  (5 cards: Lab Maniac / Jace WoM / Fblthp / Doctor Doom / Platinum Persecutor; 0 false positives).
  The design's coarser `otag:mill-self OR oracle:'no cards in'` disjuncts were **rejected on
  measurement**: `mill-self` is a broad graveyard-fuel enabler (Central Elevator is not a deckout
  win) and `oracle:'no cards in'` matches "no cards in **hand**" (Barren Glory / The Cheese Stands
  Alone — empty-board wins). Known miss: Thassa's Oracle carries no distinguishing otag/phrase (the
  design's flagged "honest core of the problem"); its infinite case is still closed by the walk
  (`altwin::self-deplete`).
- **`domain`** — the `five-color-win` SYNTH payoff (above) now carries an archetype edge `→ domain`
  (it previously distilled to None). This is exactly "the distill archetype names the plan: domain" —
  the payoff (not the `color-fixing` PRODUCER, which stays edge-less so a fixer never renames a ramp
  deck) names a deck that runs the five-color win. Inert on the corpus (2 non-commander cards, never
  node-admitted).
- **Riders, not new identities** — every other wincon rides its existing family once the catch-all is
  gone: a lifegain-win rides `lifegain` (its win is the `altwin::lifegain` event-edge), a poison-win
  rides `poison` (`altwin::poisoned`), a cast-count win rides `spellslinger`. The `otag:alternate-win-
  condition AND otag:lifegain` coarse set was NOT authored (it drags in cast-count/combat/group-hug
  wincons — the same reason it was rejected for `five-color-win`).
- **The standalone-state tail distills to None** — the genuine singletons (Phage, Barren Glory,
  coin-flip, Door to Nothingness, …) get no archetype name: a one-off build-around win does not name
  the deck, and a `misc-wincon` bucket broad enough to catch them re-bundles unrelated mechanics (the
  ADR 0023 conflation). Their win is modeled in the event graph (Home 2) or the walk (Home 1).
- **Dead-ref fixed** — the catch-all's 2nd disjunct `otag:alternate-win-condition` resolved to no
  CardAttribute (the lake slug surfaces as the attribute NAME `wincon`); removed with the line.
- **The combo-override headline `alt-wincon/combo` (`naming.ALT_WINCON_COMBO`) is a SEPARATE axis**
  from `archetype_identity` and is **untouched**. This is why **Ghave did not re-flip** (its label was
  always the combo-override, not this distill identity) — the research doc's Ghave caveat resolves to
  "no re-flip," confirming the two axes are cleanly separated.

Measured OFF/ON (157 gauntlet + 17 fixtures): exactly 2 gauntlet decks (Azami, Baral) + 2 fixtures
(Baral, Peace Offering) moved — the 4 that admit `wincon` as a node; **0 headline flips, 0
archetype_identity membership flips, 0 confidence-tier flips, 0 spurious leads**. ENGINE SHA
unchanged; RESOLVE + FULL_CORPUS re-baselined (ADR 0003). Proof:
`subagents/r1b-decompose-altwincon/outbox.md`.
