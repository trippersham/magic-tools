# Quadrant Theory — a reasoning-led pre-mortem over a fact sheet

How to diagnose whether a Commander deck has a **plan for every game-state it needs**, given
its archetype. This is not a card-scoring tool. Quadrants are **questions about the deck's
plan**, not buckets you sort cards into. The diagnosis is an LLM reasoning task over two
inputs: the deck's stated `Strategy` and a **neutral fact sheet** emitted by
`deck_factsheet.py`. The output is a narrative pre-mortem plus a shopping list — never a
percentage bar chart.

## The reframe: quadrants are questions, not buckets

The retired v1 of this tool scored every card into a quadrant and tallied the result. That
was wrong (see "Why the card-scoring premise was retired"). The working model asks, for each
game-state, **what is the deck's plan?**

- **Development** — what is the plan to *not fall behind* early?
- **Parity** — what is the plan to *break a stall* / grind ahead when the board is even?
- **Winning** — what *is* the actual win: what is it, how fast, how interruptible?
- **Losing** — what is the *out* when behind, swept, or raced?

You answer these from the **engine** — the deck's Strategy plus the fact sheet's neutral
counts — not from a per-card tally. A card's quadrant is a contextual *role*, and role is
emergent from the deck it lives in.

## Where it came from

Quadrant Theory judges a card by asking not "is this card good?" but "*when* is this card
good?" — imagining game states and checking whether the card materially helps in each.

- **Conceived by Brian Wong**, first presented on *Limited Resources* #184 (May 2013).
- **Written up canonically by Marshall Sutcliffe** ("Quadrant Theory," WotC, Aug 2014),
  crediting Wong. (It is commonly mis-attributed to Reid Duke / ChannelFireball — the
  "Quadrant Theory Revisited" article on ChannelFireball is Sutcliffe's, not Duke's.)
- Originated for **Limited** single-card evaluation; adapted to Commander by EDHREC and
  Card Kingdom. Labels drift across sources — **Winning/Losing** are often called
  **Ahead/Behind**; **Parity** is sometimes **Stall**.

**Core principle (Sutcliffe):** "Losing" is the hardest and most valuable quadrant to be
great in — cards that help when you're behind are rare and decisive. Commander decks most
often under-invest here. This is why the **resilience profile** (below) is the lead signal.

## The deterministic / reasoning split

Two inputs, cleanly divided:

- **The script owns facts and counts.** `deck_factsheet.py` emits objective, verifiable
  numbers: curve, ramp, an interaction census by type and speed, a keyword census, card
  advantage, coverage %. It **never** assigns a role, wincon, engine, or quadrant.
- **Reasoning owns roles.** The LLM decides what the wincon is, what the engine does, what
  the loss-condition is, and what the plan is in each game-state — from the Strategy and the
  facts together.

**The portability rule** (the test for which side a claim belongs to):

> **Would the answer change if this card were in a different deck?**
> **No → it's a script fact. Yes → it's reasoning.**

"Blasphemous Act is a board wipe" is portable — it's true in any deck, so the script counts
it. "Blasphemous Act is this deck's clawback-when-behind" is contextual — true here, maybe
offense elsewhere — so it's reasoning. Arithmetic over 100 cards is where LLMs hallucinate;
role assignment is where regexes invert meaning. The split keeps each on the side it's good
at.

## The resilience profile is the lead signal

Sutcliffe's "Losing is the hardest quadrant" is the measurable core of the diagnosis, and it
is where Commander decks silently die. The fact sheet's **resilience profile** is the
concrete, deterministic read on it:

- **board wipes** (`interaction.board_wipes`) — can the deck reset a board it's losing?
- **instant-speed answers** (`interaction.instant_speed`) — can it respond, or only act on
  its own turn? (This is the highest-value fully-deterministic signal.)
- **protection** (`interaction.protection`) — can it defend its own key pieces?
- **ramp** (`mana.ramp_sources`) — can it recover tempo / cast its way out?
- **curve** (`shape.cmc_histogram`, `shape.avg_cmc`, `shape.top_end_count`) — is it too slow
  to stabilize before it dies?

Lead the pre-mortem with this. A deck can look powerful and still fold to a sweeper or a
faster deck because it has zero instant-speed answers — that gap is invisible to a wincon
count but obvious in the resilience profile.

## Coverage % is a confidence signal, not a verdict

`coverage.uncategorized_pct` is the share of nonland cards that matched **no** interaction /
ramp / repeatable-draw census category. It is a **feature**, read as trust:

- **High uncategorized %** → the deck's value is synergy-carried and invisible to
  precision-first counts. **Weight the Strategy over the numbers.** A go-wide or aristocrats
  deck can be 60%+ uncategorized and completely functional — the counts just can't see a 3/3
  that wins through a board plan.
- **Low uncategorized %** → the deck's plan lives in explicit-text cards (removal, ramp,
  draw), so the counts are a more faithful picture.

Coverage % never appears as a verdict. It tells you *how much to trust the other numbers.*

## Fact-sheet field reference

Every field `deck_factsheet.py factsheet <decklist>` emits. **Cite these exactly — any drift
between this list and the script's output is a bug.**

| Path | Type | Meaning |
|------|------|---------|
| `deck` | string\|null | Deck name (first comment line of the list), or null |
| `shape.nonland_count` | int | Nonland cards resolved |
| `shape.land_count` | int | Lands resolved |
| `shape.cmc_histogram` | object | Nonland counts bucketed `0`,`1`,…,`6`,`7+` |
| `shape.avg_cmc` | float | Mean CMC, nonland only |
| `shape.top_end_count` | int | Nonland cards at CMC ≥ 6 |
| `mana.ramp_sources` | int | Nonlands that produce mana **or** fetch a land onto the battlefield |
| `mana.fixing_sources` | int | Ramp sources producing >1 color or any-color |
| `mana.pip_counts` | object | Colored/colorless pip counts across nonland costs (`W`,`U`,`B`,`R`,`G`,`C`) |
| `keywords` | object | Scryfall `keywords` census, nonzero only (e.g. `Flying`, `Flash`) |
| `interaction.board_wipes` | int | Destroy-all / damage-to-each-creature / mass -X/-X sweepers |
| `interaction.spot_removal` | int | Explicit "destroy/exile target \<permanent-type>" |
| `interaction.counterspells` | int | "counter target" |
| `interaction.protection` | int | hexproof/indestructible/ward/shroud kw, "protection from", or phase-out |
| `interaction.instant_speed` | int | Type-line Instant **or** Flash keyword |
| `card_advantage.repeatable_draw` | int | Recurring draw triggers ("at the beginning of…draw", "whenever…draw a card") |
| `card_advantage.one_shot_draw` | int | "draw N cards" not covered by a repeatable trigger |
| `structural.etb_creatures` | int | Creatures with a "when(ever) … enters" trigger |
| `structural.graveyard_recursion_present` | bool | Any "return … from … graveyard to the battlefield" |
| `coverage.categorized_pct` | float | % of nonland cards hitting ≥1 census category |
| `coverage.uncategorized_pct` | float | The synergy tell (see above) |
| `coverage.uncategorized_cards` | string[] | Names of the uncategorized cards |
| `cards[]` | object[] | Per-card records: `name`, `cmc`, `type_line`, `keywords`, `produced_mana`, `is_land`, `oracle_text` |
| `missing` | string[] | Names the Scryfall cache could not resolve |

The script never emits a quadrant, role, or "good"/"bad" label. If you find yourself wanting
one from the JSON, that judgment is yours to make.

## Archetype frames the pre-mortem's expectations

Read the deck's `Archetype:` line (`references/strategy-schema.md`). Archetype sets what
"healthy" looks like per quadrant — it **frames the expectations you bring to the
pre-mortem**, it does not score coverage:

- **Aggro / burn / voltron** — may legitimately run thin on Losing; speed is the plan. A
  shallow Losing is a note, not automatically a flaw.
- **Control / stax** — *must* be deep on Losing and Parity, or it folds. Thin Losing here is
  a genuine break.
- **Midrange / value** — wants a plan in all four; closest to "balanced."
- **Combo** — needs Development setup and a Winning payoff; protection reads as its Losing
  plan (survive to combo).

A thin quadrant is only a problem relative to what the archetype *needs* there. Judge against
the archetype, never against flat even coverage.

## Worked example — Ozai (reasoning, not a bar chart)

Deck: **Ozai** (Rakdos, BR) — `Archetype: Burn / Big Mana / Firebending`; the Strategy's win
condition is X-cost burn fueled by ramp, cost reduction, and impulse draw.

Suppose the fact sheet reports: `avg_cmc` high with a real top end (`top_end_count` elevated),
`ramp_sources` strong, `card_advantage.one_shot_draw` and impulse-style effects present but
`repeatable_draw` thin, `interaction.spot_removal` a few, `interaction.instant_speed` low,
`interaction.board_wipes` 0–1, `interaction.protection` ~0, and a moderate
`coverage.uncategorized_pct` (the X-spells and firebending payoffs are the uncategorized
tail).

Reasoning the pre-mortem from Strategy + facts:

- **Development** — ramp is the plan and the facts confirm it (`ramp_sources` strong); the
  risk is the high curve if ramp doesn't show. OK, with a curve caveat.
- **Parity** — impulse/one-shot draw refuels a stall but bleeds card economy over a long
  grind; `repeatable_draw` is thin, so parity is *powered but leaky*.
- **Winning** — **the win is real and identified: a big X-cost burn spell off a ramped mana
  pool.** It is fast once online but interruptible (a single counter or a fog turn blanks the
  payoff turn). This is the wincon — the deck absolutely *can* close.
- **Losing** — this is the hole. `instant_speed` low + `protection` ~0 + `board_wipes` 0–1
  means almost no way to answer a board it's behind on or protect the X-spell turn. The
  resilience profile is the weak point.

**Loss condition:** a faster or more resilient board races Ozai before the mana is assembled,
and Ozai has no instant-speed answer to stabilize — it dies with the winning spell still in
hand.

**Prescription:** add instant-speed interaction and protection for the payoff turn; steady
the card economy so the impulse-draw bleed doesn't strand it. **Not** "you can't close, add
finishers" — the finisher exists and is the whole plan. The v1 tool would have filed the
X-spells as uncategorized, seen Winning at ~10%, and told the user to add win conditions to a
deck whose entire identity *is* its win condition. The real problems are resilience-when-
behind and impulse-draw bleed.

## Why the card-scoring premise was retired

The v1 tool scored each card into a quadrant and reported percentages. This inverted
synergy-, combo-, aristocrats-, voltron-, and spellslinger-driven decks — most of a normal
pod. Quadrant theory was invented for **Limited**, where a card ≈ its own text, so scoring a
card in a vacuum is valid. In **Commander**, a card's value is emergent from the engine, so a
per-card tally files wincons as defense (Blood Artist → "lifegain/Losing"), engines as
clawback (Splendid Reclamation → "recursion/Losing"), and reports ~every deck as having no
finisher (Winning 0–10%). This is the documented **synergy blind spot**, and it is unfixable
by broadening regexes: **quadrant membership is a role, and role is contextual.** The fix is
structural — facts from the script, roles from reasoning.

## Limitations (state these; do not pretend otherwise)

- **Synergy blind spot.** Precision-first counts cannot see value that lives in the board
  plan or a combo. That is exactly what `coverage.uncategorized_pct` measures and warns you
  about — when it's high, trust the Strategy over the numbers.
- **cEDH / high-power combo is out of scope.** In those decks game stages aren't
  well-defined (it plays like Legacy/Vintage combo), and quadrant theory breaks down. Do not
  apply this diagnostic to a cEDH list.
- **Weighting is qualitative and contested.** Sources disagree on exact quadrant weights;
  archetype expectations are heuristics for reading shape, not quotas to hit.
