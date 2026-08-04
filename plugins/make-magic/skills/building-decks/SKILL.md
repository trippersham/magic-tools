---
name: building-decks
description: >
  Build, optimize, and evaluate MTG Commander decks. TRIGGER when: user asks to
  "optimize a deck", "recommend cards", "evaluate a card for a deck", "propose swaps",
  "vet a trade", "find upgrades", "what's good for [deck]", "should I run [card] in [deck]",
  "build a deck around [commander]", or any question about deck strategy and card fit.
  Also trigger for "recommend from set", "what should I add from [set]", "upgrade suggestions".
  Also trigger for deck balance / Quadrant Theory: "is my deck balanced", "diagnose [deck]",
  "what's [deck] missing", "what quadrant is weak", "what should I shore up", "is [deck] too
  glass-cannon".
user-invocable: true
---

# Building Decks

The **orchestrator** for a guided deck build. Its job is small: each turn, read the deck,
figure out the earliest thing that's missing or stale, and route to the skill that fixes
it. It does not own strategy, diagnosis, discovery, or simulation — those are delegated.

<primary-constraint>
**Enforce the data; guide the flow.**

The deck is ALWAYS a valid typed `Deck` — the local decks store enforces every invariant
(singleton, commander count, quantity-correct swaps, the shrink guard). You never do
dict-surgery, never maintain a parallel draft structure. So you do not need to *enforce
the flow*: the flow is **advisory guidance**, with exactly one hard, ceremony-backed gate
— the push at COMMIT. Everything before COMMIT is "here's what's stale, here's the next
step, want to sim first?" — never pretend-enforcement.

The old state machine tried to enforce the flow (transitions, `session.json`, `draft.*`,
stale flags → theater) while leaving the data brittle. That is deleted. The data is hard;
the flow is soft.
</primary-constraint>

<red-flags>
If you catch yourself about to:
- **Track phase in a session file or a `draft.*` field** — STOP. Phase is DERIVED every
  turn from the fetched deck (below). There is no stored state, no `session.json`, no
  `draft.working_deck` / `draft.candidates` / `draft.stale`, no transitions.
- **Hand-edit a decklist dict, or apply swaps yourself** — STOP. All deck edits go through
  the typed CLI verbs (`deck-swap`, `deck-add`, `deck-remove`, `set-strategy`, …). The
  store applies and guards them.
- **Evaluate a card without the deck's Strategy** — STOP. Read the Strategy first;
  evaluate fit, not raw power (see Operation A).
- **Explore options by mutating the real deck** — STOP. To try things without touching the
  deck of record, work on a `new-draft --from "<deck>"` copy, then `promote-deck` +
  `archive-deck` the exploration when done. Simple, confident edits can go straight to the
  synced deck (commit-through).
</red-flags>

## The data surface: the `collection` CLI

Every deck read, edit, and lifecycle step goes through **one backend-agnostic CLI** — the
same whether the source of record is local YAML or Airtable. Deck reads/edits route
through the local decks store (a synced deck is pulled current on read; edits commit
through to the source, or stay local on an ephemeral draft).

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection <verb> [args...]
```

**Mode banner — run this first.** Announce the source of record, then proceed:
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection status   # {"backend": "local"|"airtable", ...}
```

The verbs this orchestrator uses:

| Purpose | Verb |
|---|---|
| Read a deck (strategy, assessment, focus_otags, cards[]) | `get-deck "<deck>" [--field …]` |
| Neutral fact sheet (for ASSESS) | `factsheet "<deck>"` |
| List decks — `[synced]` vs `[ephemeral]` | `list-decks [--archived]` |
| Set the human-authored aim | `set-strategy "<deck>" "<text>"` |
| Set the reasoning-authored reality-check | `set-assessment "<deck>" "<text>"` |
| Set the curated intended identity | `set-focus-otags "<deck>" <otags…>` |
| Apply a size-preserving swap (records `--why` in the ledger) | `deck-swap "<deck>" --add "<in>" --cut "<out>" [--role R] [--why …]` |
| Add / remove copies (grow a skeleton / trim) | `deck-add "<deck>" "<card>" [--qty N] [--why …]` · `deck-remove …` |
| Start an EPHEMERAL draft (clean-slate, or `--from` = a local COPY) | `new-draft "<name>" [--from "<src>"] [--commander …] [--format …]` |
| Promote an ephemeral draft to a synced deck (through the save ceremony) | `promote-deck "<draft>" --to "<source-name>"` |
| Step back one edit | `undo-deck "<deck>"` |
| Hide / restore an exploration draft | `archive-deck "<draft>"` · `unarchive-deck "<draft>"` |
| Archetype-fidelity combo signal (for VALIDATE) | `deck-combos "<deck>"` |
| Sync (normally opaque; you rarely type these) | `pull` · `push` · `sync "<deck>"` |

**Edits commit through the target.** A `set-*` / `deck-*` edit on a SYNCED deck writes to
its source of record; the same edit on an EPHEMERAL draft stays purely local. You never
pick a "stage vs write" mode — the target's ephemerality decides. That is exactly why
"explore without touching the real deck" = work on an ephemeral `new-draft --from` copy.

## Prerequisites

- **uv** — the CLI and helper scripts run via `uv run` (PEP 723 inline metadata).
- **A populated backend** — each existing deck has a Strategy per
  `references/strategy-schema.md`. Local mode reads `collection/` YAML under
  `MAKE_MAGIC_DATA_DIR`; Airtable mode needs the base cloned and the connector enabled.

---

## Entry modes

Two ways in. Pick by whether a deck already exists.

**Improve an existing deck.** Two sub-modes:
- **Direct edit (commit-through).** For simple, confident changes — a known swap, a
  strategy refresh, a diagnosis — operate on the SYNCED deck by name. Every `set-*` /
  `deck-swap` commits through to the source of record immediately. This is the default for
  small, deliberate work.
- **Explore in a draft (real deck untouched).** When you want to try several directions,
  A/B a rebuild, or make speculative swaps without risk, make a local exploration COPY:
  ```bash
  ${CLAUDE_PLUGIN_ROOT}/scripts/collection new-draft "<deck> (explore)" --from "<deck>"
  ```
  All edits land on the draft (ephemeral → local only; the original is never touched).
  When the exploration is good, commit it back and clean up:
  ```bash
  ${CLAUDE_PLUGIN_ROOT}/scripts/collection promote-deck "<deck> (explore)" --to "<deck>"
  ${CLAUDE_PLUGIN_ROOT}/scripts/collection archive-deck "<deck> (explore)"
  ```

**Build clean-slate.** Start ephemeral, build locally (zero network), then promote:
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection new-draft "<name>" --commander "<card>" --format Commander
# … FRAME → ASSESS → REFINE → VALIDATE on the draft …
${CLAUDE_PLUGIN_ROOT}/scripts/collection promote-deck "<name>" --to "<name>"
```

**Which to pick — and when it's genuinely ambiguous.** Direct-edit for one or two
confident changes to a deck the user clearly means to change. Explore-draft when the user
signals uncertainty ("let me try…", "what if…", "compare…") or the change is large. If it
is ambiguous which the user wants — e.g. "optimize my Ozai deck" could mean either — ASK
one line: *"Edit Ozai directly, or spin up an exploration copy to try things first?"*
Don't guess; the difference is whether the real deck moves.

---

## The derived phase — reason, don't transition

There is **no state machine.** Each turn, `get-deck "<deck>"` and reason about the
earliest artifact that is missing or stale. "Stale" is derived: an edit upstream makes
everything downstream stale by definition (a changed deck ⇒ its Assessment and its last
sim no longer describe it). You re-derive; you never track it.

```
no strategy                                  → FRAME    (distilling-strategy)
strategy present, assessment missing/stale   → ASSESS   (assessing-decks)
assessment fresh, upgrades wanted            → REFINE   (refining-decks → deck-swap the picks)
deck changed since last sim (or none)        → VALIDATE (simulating-games + deck-combos fidelity)
user satisfied                               → COMMIT   (promote-deck / push, through the ceremony)
```

- **Reverse is just an edit.** Want to revisit the Strategy after refining? `set-strategy`.
  That makes the Assessment stale → next turn re-derives to ASSESS. No backward events.
- **Loops are just re-derivation.** REFINE changes the deck → the deck moved → ASSESS and
  VALIDATE are stale → you re-run them on the current deck. Nothing to invalidate by hand.
- **Undo** any applied edit with `undo-deck "<deck>"` (restores the prior ledger version).

Because the deck is always the live, valid `Deck`, every delegated skill reads the CURRENT
deck with `get-deck` — there is no held/stale copy to reconcile.

### Delegation table

| Phase | Delegate to | The orchestrator's job |
|---|---|---|
| FRAME | **distilling-strategy** | route in when `strategy` is absent/needs refresh; it writes via `set-strategy` / `set-focus-otags` (clean-slate: `new-draft` first) |
| ASSESS | **assessing-decks** | route in when the Strategy is present but the Assessment is missing or the deck moved; it reads `factsheet` + writes `set-assessment` |
| REFINE | **refining-decks** | route in when the user wants upgrades; it proposes ranked swaps and APPLIES the accepted ones with `deck-swap` |
| VALIDATE | **simulating-games** + `deck-combos` | route in when the deck changed since the last sim; run games AND the archetype-fidelity fold (below) |
| COMMIT | (this skill) | `promote-deck` an ephemeral draft, or `push` a synced deck — through the save ceremony (the one hard gate) |

---

## VALIDATE — sim depth + archetype-fidelity

Two parts, and the second keeps the first honest.

1. **Run the games** via **simulating-games** on the current deck (a-posteriori win-rate).
2. **Archetype-fidelity fold** — before you present the sim as a verdict, run the
   deterministic combo signal:
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/collection deck-combos "<deck>"
   ```
   It reports the named-card combos present in the deck plus a `combo_data_available` flag.
   - **Combos present** (or the deck is a voltron / free-mana engine by your reasoning):
     Forge under-pilots these archetypes, so treat the sim as a **FLOOR, not a verdict.**
     Do NOT discard a good combo swap on a low win-rate — the engine can't pilot the
     combo, so the low number is a measurement artifact, not evidence the swap is bad.
   - **`combo_data_available: false`** (sparse lake — the check couldn't run): this is
     **INCONCLUSIVE, not a clean bill.** Say so plainly: "the combo check couldn't run, so
     I can't confirm the sim captured the deck's fastest line." Never present an
     unavailable check as "no combos found / trust the sim."
   - **`available: true`, no combos, no voltron/free-mana read:** the sim is a fair verdict.

---

## COMMIT — the one hard gate

Committing is the only ceremony-backed checkpoint. It promotes an ephemeral draft or
pushes a synced deck through the source's `save_deck` ceremony (shrink guard, cascade
prompts, known-good baseline capture, drift refusal). At commit, the freshness of the
final deck is recorded as provenance — a sim that was never re-run against the final deck
is durably marked "shipped un-validated." You do not manage that; it falls out of the
push.

```bash
# clean-slate or a promoted exploration draft:
${CLAUDE_PLUGIN_ROOT}/scripts/collection promote-deck "<draft>" --to "<source-name>"
# a synced deck whose local edits should reach the source explicitly:
${CLAUDE_PLUGIN_ROOT}/scripts/collection push "<deck>"
```

---

## Ad-hoc operations (not a guided build)

Some questions are single-shot and don't need the phase loop.

### A. Evaluate a card for a deck ("Is X good in Y?")

<evaluation-workflow>

**Step 1 — Read the deck's Strategy** (the `<primary-constraint>` applies):
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection get-deck "<deck>" --field strategy   # or the whole deck
```
Address the deck **by name** (`list-decks` disambiguates); no record ids.

**Step 2 — Fetch + tag the card:**
```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_cache.py get-card "<card name>"
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/card_tagger.py tag-card "<card name>"
```

**Step 3 — Reason about fit.** Parse the Strategy (archetype, win conditions, `What makes
a card good here`, `What doesn't fit`) and compare against the card's otag buckets (the
tagger's `tags` — a data-grounded fit signal that INFORMS but doesn't decide), oracle
text, CMC slot, and type.

<reference file="strategy-schema.md" section="Key Mechanics Vocabulary">
strategy-schema.md — the keyword vocabulary and how tags map to strategy keywords.
</reference>

**Step 4 — Present the verdict:** Yes/No/Maybe + confidence; which mechanics match; the
card's role in the game plan; comparison to existing options at that CMC/role; caveats
(anti-synergies). If the user then wants to APPLY it, that's a `deck-swap` (pair it with a
cut) — which enters the REFINE flow.

</evaluation-workflow>

### B. Vet a trade ("Is trading X for Y good for deck Z?")

<trade-workflow>

**Step 1 — Read the deck's Strategy** (as Operation A, Step 1).

**Step 2 — Tag and score both cards:**
```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/card_tagger.py tag-card "<card giving up>"
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/card_tagger.py tag-card "<card receiving>"
```
Score each against the Strategy; keep the **synergy reason and the flexibility reason
separate** (a multi-game-state card is a prize; an only-when-ahead card is a trap — see
card-evaluation.md).

**Step 3 — Compare prices** (from the tagger output, or live):
```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_cache.py get-card "<name>"
```

**Step 4 — Present the verdict:** giving-up vs receiving (fit + price), the strategy-fit
delta, the price delta, and Accept / Decline / Even with rationale. Recording the trade
itself is a **managing-inventory** action.

</trade-workflow>

> **Set-release recommendations, whole-deck diagnosis, and ranked upgrade discovery are
> NOT here** — they are delegated: diagnosis → **assessing-decks**, ranked swaps / "what
> should I add from [set]?" → **refining-decks**. Route to them (they are the ASSESS and
> REFINE phases); do not re-implement their rubrics in this skill.

---

## Critical Constraints

<constraint name="color-identity">
**Never recommend cards outside the deck's color identity.** Commander rules require every
card to be within the commander's color identity. The tagger outputs `color_identity` for
every card — filter against the deck's identity.
</constraint>

<constraint name="runtime-strategy">
**Strategy lives in the backend, not in this skill.** Strategies evolve; decks get rebuilt.
Always read the Strategy at runtime via `get-deck "<deck>" --field strategy`. See
`references/strategy-schema.md` for the convention and vocabulary.
</constraint>

<constraint name="dfc-handling">
**For double-faced cards, check `card_faces[0]` when top-level fields are null.** Scryfall
returns `null` for `image_uris` / `mana_cost` / `oracle_text` at the top level for DFCs;
the data lives in `card_faces[0]`. The tagger handles this automatically.
</constraint>

<constraint name="address-by-name">
**Address decks by name through the CLI — never juggle record IDs.** `get-deck "<deck>"`
returns exactly one deck (strategy, focus_otags, assessment, cards[]) in a single call, in
either backend. No record-id lookup at the skill layer.
</constraint>

<constraint name="edits-through-the-store">
**All deck edits go through the typed CLI verbs — never hand-edit a decklist.** The local
decks store enforces every `Deck` invariant on `deck-swap` / `deck-add` / `deck-remove` /
`set-*`: quantity-correct, singleton, commander-safe, shrink-guarded. There is no
dict-surgery and no parallel draft structure to keep in sync.
</constraint>

---

## Reference Guides

| When you need to... | Read |
|---------------------|------|
| Understand strategy field format and keyword vocabulary | [references/strategy-schema.md](references/strategy-schema.md) |
| Diagnose deck balance (Quadrant Theory pre-mortem, fact-sheet fields) | [references/quadrant-theory.md](references/quadrant-theory.md) |
| Invoke tagger scripts or interpret scoring tiers | [references/card-evaluation.md](references/card-evaluation.md) |
| (Optional / ad-hoc) Airtable table/field IDs for MCP exploration | [references/airtable-schema.md](references/airtable-schema.md) |
| (Optional / ad-hoc) Efficient Airtable MCP reads / edge cases | [references/airtable-patterns.md](references/airtable-patterns.md) |

## Optional / ad-hoc (Airtable-only, read-mostly)

When the backend is Airtable and you (a human) are connected via `/mcp`, you may run
`mcp__airtable__*` **reads** (`list_records`, `search_records`, `get_record`,
`describe_table`) for exploratory poking. **Skills WRITE only through the `collection`
CLI** — no executable step here creates/updates/deletes via `mcp__airtable__*`.
