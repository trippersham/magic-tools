# 0016 Closure-mechanism win-walk: the authoritative combo win-detection path

| Field | Value |
|---|---|
| Status | Accepted. In force on branch `feat/strategy-engine`. |
| Review | pending owner review: agent-authored as part of the Phase 2B step 5b-ii flip, owner-approved on the measured 5b-i delta. |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests/test_combo_win.py -q` (30 assertions: fid routing, closure walk verdicts, guard evaluation, sink dissolution, authoritative wiring). Parity gate: 253 promotions, 5 FP drops, 25 decks 0→win — exact match to the validated 5b-i shadow delta. |
| Code paths | [`combo_win.py`](../../pipeline/pipeline/transforms/strategy/combo_win.py) (the walk), [`data/card_edges.json`](../../pipeline/pipeline/transforms/strategy/data/card_edges.json) (card edge classes), [`strategy_graph.py`](../../pipeline/pipeline/transforms/strategy/strategy_graph.py) (imports combo_win for combo classification), [`combo.py`](../../pipeline/pipeline/transforms/strategy/combo.py) (regex + frozen sink list deleted). |

## Context

The strategy engine classifies each deck's combos as win or non-win to drive
the combo surface (T5) and the sim DRIVE gate. The original implementation used
two regex paths:

1. **`is_game_win_result(result)`** — a regex over the combo's result string
   that matched lethal phrasing ("Win the game", "Infinite damage", etc.).
2. **`LETHAL_MANA_SINK_NAMES`** — a frozen list of 22 X-spell finisher cards;
   an infinite-mana combo was promoted to a win when the deck contained one.

This had three problems: (a) the regex was a string heuristic that missed
card-closed wins (a combo producing infinite mana + Impact Tremors in deck =
win, but the regex only saw "Infinite mana"), (b) the frozen sink list was
hand-curated and incomplete, and (c) the regex couldn't evaluate scope/guard
constraints (Sheoldred's draw-drain fires on opponent draw only, not self-draw).

Phase 2B step 5b-i validated a SHADOW closure walk alongside the regex path,
producing a measured delta: +253 new wins across 50 decks (25 flipping from
zero win-combos), 5 regex false-positive drops, 0 WRONG promotions. The owner
approved the delta.

Phase 2B step 5b-ii FLIPPED the walk to authoritative and deleted the regex path.

## Decision

### The closure walk is the authoritative win-detection path

`combo_win.py` is the single source of truth for whether a combo is a win in a
given deck. It is imported by `strategy_graph.py` and runs for each detected combo.

The walk is **deck-aware**: a combo's win-ness depends on the deck's card
composition. An infinite-mana combo is a win only if the deck contains a card
in the `mana_sink` edge class (e.g. Fireball, Walking Ballista).

### Walk mechanism

1. **`fid_route(fid)`** classifies each of the combo's produced features (via
   the fid-to-Event adapter, ADR 0007) into a route: `terminal-win`,
   `sba-drain`, `deckout-opp`, `combat`, `intermediate`, `self-deplete`,
   `state`, `oneshot`, `inert`.

2. **`combo_win_verdict(fids, deck_card_names)`** runs a priority ladder:
   - Rule-closed (immediate): literal `game-end` fid or SBA drain.
   - Card-closed: an intermediate event (e.g. `mana-produced`) is converted by
     a deck card in a matching edge class (e.g. `mana_sink`, `draw_drain`,
     `etb_drain`, `death_drain`, `cast_drain`, `gain_drain`) or an `altwin::`
     edge class. Guard evaluation (see below) filters hits.
   - Rule-closed (deferred): deferred `game-end`, opponent deck-out, RAD rule.
   - Action-closed: combat bodies (immediate with haste, deferred without),
     turn monopoly.
   - Card-needed: intermediate events with no card edge → not a win.
   - Terminal draw, self-loss, state, unclosable, none.

3. **Guard evaluation** prevents false promotions:
   - **Scope guard:** opponent-draw drainers (Sheoldred, Nekusar, etc.) fire
     only on opponent-scoped draw events, not self-draw.
   - **Characteristic guard:** color-gated drainers (Aragorn the Uniter) are
     conservatively blocked (can't determine spell color from fids).
   - **Identity guard:** "another"-identity drainers (Plague Belcher) are
     conservatively blocked.
   - **Infinity-dominance:** an infinite-magnitude stream satisfies ordinal
     gates (Helix Pinnacle's 100-mana threshold).

### Frozen sink list dissolved

The `LETHAL_MANA_SINK_NAMES` tuple (22 hand-curated X-spell finishers) is
**deleted** from `combo.py`. Mana sinks are now the `mana_sink` card edge class
in `data/card_edges.json`, derived from the EventEffect ontology. This is
extensible (add a card to the edge class data) and auditable (the data file is
diffable), replacing a frozen code constant.

### Card edge classes

`data/card_edges.json` maps edge class names to sets of card names. Each class
represents cards that convert a produced event into a game-ending outcome:

- `mana_sink` — infinite mana → lethal damage/drain (Fireball, Exsanguinate, ...)
- `draw_drain` — infinite draw → opponent life loss (Sheoldred, Nekusar, ...)
- `etb_drain` — infinite ETB → life loss (Impact Tremors, Purphoros, ...)
- `death_drain` — infinite death → life loss (Blood Artist, Zulaport Cutthroat, ...)
- `cast_drain` — infinite spell casts → life loss (Aetherflux Reservoir, ...)
- `gain_drain` — infinite lifegain → drain (Vito, Marauding Blight-Priest, ...)
- `mana_to_combat` — infinite mana → combat bodies
- `altwin::*` — alternate win conditions keyed by event

### Regex deleted

`_WIN_RESULT_RE`, `_INFINITE_MANA_RE`, `is_game_win_result()`,
`is_infinite_mana_result()`, `LETHAL_MANA_SINK_NAMES`, and `LETHAL_MANA_SINKS`
are all deleted from `strategy/combo.py`. The `combo_detect.py` module (used by
the sim driver) retains its own independent copies — this record governs only
the strategy engine path.

## Reasons

- **Recall.** The walk detected 253 wins the regex missed across 170 decks.
  25 decks flipped from zero win-combos to having at least one. This is the
  primary motivation: the regex was a lossy heuristic.
- **Precision.** The walk dropped 5 regex false positives (combos the regex
  called wins that the walk correctly classifies as non-wins via guard
  evaluation). 0 WRONG promotions.
- **Deck-awareness.** The walk's card-edge check is intrinsically deck-aware.
  The regex couldn't know what cards were in the deck (it operated on the combo
  result string alone); the frozen sink list was the only deck-aware element,
  and it was incomplete.
- **Guard safety.** Sheoldred/Nekusar not promoted on self-draw (the 5b-i
  validation found 4/4 Sheoldred promotions were wrong under the prototype).
  Aragorn not promoted on non-red casts. These guards are impossible with regex.
- **Extensibility.** Adding a new card edge (a new mana sink, a new ETB drain)
  is a data change to `card_edges.json`, not a code change. The frozen list
  required code edits.

## Options not taken

- **Keep the regex as a fast pre-filter.** Rejected: the walk is fast enough
  (microseconds per combo) and the regex is a maintenance liability that drifts.
- **Merge card edges into the event catalog.** Rejected: card edges are
  per-card, not per-event. They belong in their own data file.
- **Wire fids into `normalize_combo()`.** Rejected: the walk is deck-dependent;
  `normalize_combo()` operates per-combo without deck context. Win classification
  happens in `compute_facts()` where the deck is available.

## Consequences

- `combo.py::normalize_combo()` sets `win=False` as a placeholder. The
  authoritative win flag is set by `strategy_graph.py::compute_facts()` via the walk.
- `resolve()` output fields `n_win_mana_sink` and `mana_sinks_present` are
  always 0/empty (the predicate/mana-sink distinction is dissolved).
- `core_combos[].win_kind` values are now walk-mechanism labels: `'rule'`,
  `'action'`, `'card'`, `'non-win'` (replacing `'predicate'`/`'mana-sink'`).
- The sim driver (`driver_batch.py`) still uses `combo_detect.is_game_win_result`
  from the infrastructure layer — this record does not govern that path.
- Re-baseline: engine isolation SHA unchanged (`a6dfa607`); resolve composite
  `7d35fe19…` → `d409069e…`; full-corpus `de5313af…` → `0901bc62…` (ADR 0003).

## Related records

- 0003 — Byte-parity: this change is a sanctioned re-baseline with parity proof.
- 0007 — fid-to-Event adapter: the data layer the walk consumes.
- 0012 — Event ontology: the bounded trigger taxonomy events map into.
- 0011 — EventEffect guards: the guard model the walk's guard evaluation uses.
