---
paths:
  - "**/pipeline/transforms/strategy/win_walk.py"
  - "**/pipeline/transforms/strategy/data/card_edges.json"
  - "**/pipeline/transforms/strategy/combo.py"
---

# Closure-mechanism win-walk

Combo win-detection uses the closure walk (ADR 0016), not regexes. The walk is deck-aware — a combo's win-ness depends on the deck's card composition.

<win-walk>

- The **authoritative** combo win-detection path is `win_walk.py::closure_walk()`, imported by `facts.py`. Do not re-add regex patterns (`_WIN_RESULT_RE`, `is_game_win_result`) or frozen sink lists (`LETHAL_MANA_SINK_NAMES`) to `combo.py`. Reason: the walk subsumes both with higher recall (253 new wins), precision (5 FP drops), and deck-awareness. See `plugins/make-magic/docs/adr/0016-closure-mechanism-win-walk.md`.
- **Card edge classes** live in `data/card_edges.json`. To add a new mana sink or drain converter, edit the data file — never add a code constant. Reason: extensibility via data, not frozen lists (ADR 0016 Decision §"Frozen sink list dissolved").
- **Guard evaluation** (`_guard_passes`) prevents false promotions (Sheoldred scope, Aragorn color). Do not bypass or weaken guards without updating ADR 0016 and re-verifying `test_win_walk.py`.
- If you change the walk, update ADR 0016 in the same change and re-verify `test_win_walk.py` + the parity oracle. Reason: the decision's one home is the record.

</win-walk>
