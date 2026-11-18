# 0020 Event-substantiveness admission: the `produced_by_commander` kind (command-zone production anchor)

> Kind renamed `producer_anchor` → `produced_by_commander` on 2026-11-16 (naming-clarity
> pass; see amendment). The Requirement kind is internal dispatch DATA — never emitted —
> so the rename moved NO parity SHA. Historical references below keep the old name.

| Field | Value |
|---|---|
| Status | Accepted. In force on branch `feat/strategy-engine`. Output-changing re-baseline (ADR 0003). |
| Review | pending owner review: agent-authored from the empirically-validated step-2 design (`event-substantiveness-empirical.md` + `mana-produced-correct-wiring.md`); the orchestrator reconciles, shows the owner the delta, and commits. |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests -q` (453 pass). The gate itself: `pytest -k "event_ontology or substantiveness"`. Parity gate: `uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle` — engine `987f2660…`, resolve `85752bb9…`, full-corpus `7e5190e8…`. Full-170 invariant (vs `7b53e6a`): **exactly 4 elected-set changes** (Selvala, Marwyn, Helga, Derevi gain `mana-produced`), **0 evictions**, **1 `named_strategy` change** (Ghave, from the consumer-side wiring, not the gate). |
| Code paths | [`determine_strategy_substantiveness.py`](../../pipeline/pipeline/transforms/strategy/determine_strategy_substantiveness.py) (`_determine_produced_by_commander`, `_EVENT_DETERMINERS`, `EVENT_SUBSTANTIVENESS`, `determine_event`), [`strategy_graph.py`](../../pipeline/pipeline/transforms/strategy/strategy_graph.py) (`elect_engine_units` — the candidate-pool filter), [`data/event_catalog.json`](../../pipeline/pipeline/transforms/strategy/data/event_catalog.json) (`mana-produced.produced_by`). |

## Context

Wiring the previously-inert `mana-produced` event (ADR 0012) into the mass election
(ADR 0014) made a *promiscuous resource event* electable. Mana production is near-ubiquitous
in Commander, so an un-gated mana unit dominated by raw mass: it elected on 13 decks under
the corrected wiring, of which only 4 were genuine mana strategies, and in the two densest
dork decks it EVICTED the deck's real engines (Ezuri's creature-attacks, Aang's six engines)
by sitting above the 0.6 keep-ratio floor. ADR 0014 deliberately left the election gated
only by `determine_node_admitted` (a different output could not be gated inside the election by
mass-share — correctly rejected there as the wrong layer). What was missing is the EVENT
analog of `determine_node_admitted`'s `is_cmd` arm: an ABSOLUTE, per-event admission check that decides
whether a promiscuous event's unit is an election candidate *at all*, answerable from the
deck alone before the election.

## Decision

Add a third substantiveness application site (ADR 0002): **event admission**, a new
Requirement KIND `produced_by_commander` (named `producer_anchor` at authoring; renamed
2026-11-16), dispatched by DATA through its own entry point.

- **Measure — command-zone PRODUCTION anchor.** An event's unit is admitted to election
  candidacy iff the deck's COMMANDER carries at least one attribute in the event's own
  producer refs. `_determine_produced_by_commander(params, event, cmd_attrs, assets)` returns
  `bool(cmd_attrs & (set(assets.events[event][params['refs']]) | set(params['extra_refs'])))`.
  It is ABSOLUTE and per-event: it reads only the commander's node-admitted attrs and the
  event's own wiring refs — **never** total/top/other-unit mass, centrality, or share.
- **Data row (the only one today).** `EVENT_SUBSTANTIVENESS = {'mana-produced':
  Requirement('produced_by_commander', {'refs': 'produced_by', 'extra_refs': ['ramp']})}`.
  An event with no row is admitted (preserves pre-gate behavior). Promiscuity-targeted by
  DATA — one row per promiscuous event, each its own owner-graded measurement.
- **The `ramp`-in-anchor-not-in-`produced_by` split (load-bearing).** The corrected
  `produced_by = [adds-multiple-mana, mana-producer, repeatable-treasures, ritual]` drops
  the umbrella `ramp` so it no longer inflates mana MASS (#71 ramp decomposition). But a
  ramp-only commander (Derevi) still anchors a genuine mana engine carried by the 99's
  specific producers, so `ramp` is carried ANCHOR-SIDE only, via `extra_refs` — a
  commander-tag check that contributes zero election mass. Dropping it (gateA) costs
  exactly one genuine deck (Derevi); keeping it anchor-side (gateB) is the chosen config.
- **Hook point.** In `elect_engine_units`, between unit construction and the `elected`
  keep-walk: `pool = [e for e in engines if determine_event(e['event'], cmd_attrs, assets)]`;
  the keep-walk runs over `pool` (its `top_mass`/`total_mass` taken over the pool). The
  reported `engines` list stays COMPLETE — exclusion is election-candidacy only, so
  diagnostics, chains, hubs and the loop probe are untouched.
- **Generic.** Keyed on the event's Requirement DATA — no `if mana`, no deck-name branch,
  no new numeric knob. The measure degenerates to set-nonemptiness (|cmd ∩ refs| ≥ 1):
  structural, with no threshold to tune or drift.

## Reasons — why anchoring, not density (the empirical core)

Per-event *density* does NOT encode substantiveness for a resource event. Every absolute
density measure was swept and overlaps badly: specific-producer attr_weight (Ezuri 29, Tayam 31
exceed every genuine deck but Marwyn; Urza sits at 6 below ten incidental decks),
consumer-side attr_weight (Ezuri/Tovolar/Lathliss out-consume every genuine deck), and the
specific:generic ratio (Ezuri is the corpus max). Incidental dork-dense decks are
compositionally indistinguishable from — often denser than — genuine mana decks. The one
absolute quantity that separates them is WHERE production is anchored: every genuine deck's
commander IS a mana producer; the incidental decks' commanders carry no mana attr or are
tagged only as sinks/consumers. A commander that *consumes* the resource does not earn the
resource event; one that *produces* it does. Combination clauses (anchor AND/OR depth≥T,
consumer clauses) are all strictly worse (false-gate Urza or re-admit Ezuri at every T).

**Robustness.** The gate quantity is integer and bimodal (0 vs 1–3); no ±k ordinary-card
swap can flip any deck's verdict — only the commander's own otags enter, and commander
attrs are node-admitted unconditionally (ADR 0004). A ±2-attr_weight perturbation of the mana
unit gives 3 benign flips gated vs 10 (incl. genuine-engine evictions) ungated — the gate
strictly REDUCES boundary sensitivity because 16 decks' elections no longer depend on mana
mass at all.

## Full-170 effect (measured; live pipeline ≡ the validated offline harness)

Vs committed `7b53e6a` (mana unwired): **4 elected-set changes** — Selvala, Marwyn, Helga,
Derevi gain `mana-produced`; **0 evictions**; every other deck byte-identical to the
pre-wiring election; **1 `named_strategy` change** — Ghave (aristocrats/sacrifice →
alt-wincon/combo), which comes from wiring the CONSUMER side (`bottomless-mana-sink` on
Ghave's commander), not the gate. The final mana-elected cohort is exactly the 4 genuine
decks, **0 false admits**, **0 genuine regressions**. Of 170 decks, 54 carry a mana unit;
only the 4 anchored-and-unstarved decks elect it.

**Note (2026-11-17, R1b):** Ghave's `alt-wincon/combo` here is the combo-override HEADLINE
label (`naming.ALT_WINCON_COMBO`) — a win-combo overrode the archetype identity — **not** the
distill `alt-wincon/combo` identity. When that distill catch-all was decomposed into honest
archetype names (`deckout/thoracle` + `domain` + family riders; ADR 0023/0024, held-file
re-baseline in ADR 0003), Ghave **did not re-flip**: its label is the untouched combo-override
axis. The two axes are separate (`headline_strategy` returns the combo label when
`combo_override_applied`, else the `archetype_identity` leader).

## Options not taken

- **A mass-share / density floor inside the election.** Rejected (ADR 0014): wrong layer,
  and empirically every density measure overlaps (above).
- **gateA (refs = `produced_by` only, no `ramp`).** Rejected: false-refuses Derevi (ramp-only
  commander) — costs one genuine deck for no benefit.
- **Extending the row to other promiscuous events now** (land-enters churn 26, permanent-dies
  11). Deferred: owner decision is `mana-produced` only; each extension is its own graded
  measurement (the mechanism needs no code change — one data row each).

## Honest residue

- **Three bounded false-admits** (Tayam, Ur-Dragon, Giada) ride single-card tagger judgments
  (`ramp`/`mana-dork` on the commander). No measured absolute quantity removes them without
  false-gating Urza. Harm is bounded: all three are clean secondary additions — no genuine
  engine is displaced. Under the corrected wiring (#71) their mana units fall below 0.6·top,
  so the gate never has to decide them, and they do not appear in the full-170 delta above.
- **Omnath (Locus of Mana) and Urza** lose their pre-correction mana election under ANY
  corrected-wiring composition — their corrected mana mass sits below the 0.6·top relative
  floor (land-enters / artifact-enters dwarf it). This is **keep-ratio starvation (#72)**, a
  threshold mechanism question, NOT a gate question — no anchor check or makes-mana tag list
  fixes a relative-mass starvation without re-importing umbrella mass. Confirmed unchanged vs
  `7b53e6a` (where mana was unwired) — not a regression.
- **Single-card tag dependency cuts both ways:** a lake refresh that moves `ramp` on/off a
  commander re-opens the census (same lifecycle as the df table / parity baselines).

## Later changes
- **2026-11-16 — kind renamed `producer_anchor` → `produced_by_commander`** (naming-clarity
  pass). Renamed the determiner (`_determine_produced_by_commander`), the `_EVENT_DETERMINERS` dispatch
  key, and the `EVENT_SUBSTANTIVENESS` Requirement kind string. The kind is internal dispatch DATA
  consumed only by `determine_event` — it is **never serialized into the emitted facts**, so the
  rename is behavior-preserving and moved NO parity SHA (verified: it was part of the 2026-11-16
  emitted-key re-baseline, [0014] / [0003], and the SHA shift there is attributable entirely to the
  emitted-key renames + `named_strategy` removal, not this one). The gate's measure and semantics
  are unchanged.
- **2026-11-17 — impulse cheat-into-play seeds (#74 decision 5; ADR 0003 re-baseline).** The
  `produced_by` admission gate now sees two new onto-battlefield producer families on the
  `creature-enters-battlefield` and `artifact-enters` events (`sneak-from-library` closure +
  `sneak-artifact`, wired in `SEED_MAP` — see [0022]). The gate itself is **unchanged**; the new
  producers simply clear it where a deck carries ≥3 (or commander-carried ≥2) of the family. Live
  full-171 delta: **5 decks elected a cheat-into-play unit** (Ureni correct lead flip; Gishath,
  Zilortha, Aang sub-lead rises; Nick Fury gains `creature-enters-battlefield` 0.064, NEW),
  **0 evictions, 0 `named_strategy`/`archetype_identity` changes, 0 spurious leads.** Full-corpus
  SHA `07eb6673…` → `188992d2…`; engine/resolve SHAs unchanged. Proof:
  `~/.claude/plans/trippersham/magic-tools/2026-11-11-strategy-abstraction-consolidation/research/impulse-seed-scope.md`.

- **2026-11-17 — Tier-A producer corrections (#74 follow-on; ADR 0003 re-baseline).** The
  `produced_by` admission gate now sees corrected producer families on several events (SEED_MAP,
  [0022]): `card-enters-graveyard` gains the `sacrifice-outlet` closure (replacing dead `sac`, the
  1,554-card family), `spell-cast` gains `impulse-cast`, `game-end` gains `alternate-win-condition`,
  `creature-copied` uses `clone` (was dead `clones`), and `food-created` loses the false
  `repeatable-treasures` producer (Treasure≠Food). The gate mechanism is **unchanged** (still DATA-keyed,
  only `mana-produced` carries a `produced_by_commander` Requirement); these are producer-SET
  corrections, not gate changes. Live OFF/ON delta: `card-enters-graveyard` dominates (sacrifice-outlet
  family), **0 engines lost** except 3 intended `food-created` false-positives. Full-corpus
  `188992d2…` → `38dd7733…`; engine `7a73aba9…` → `1eb27096…`; resolve `1e54293c…` → `00fbb836…`.
  Proof + per-change delta: `~/.claude/plans/trippersham/magic-tools/2026-11-17-concept-anchored-self-healing/subagents/tier-a-corrections/outbox.md`.

- **2026-11-17 — decomposition + gaps producers (#74; ADR 0003 re-baseline).** The `produced_by`
  admission gate now sees three corrected/new producer sets (SEED_MAP, [0022]): `monarch` swaps its
  producer otag `monarch-matters` → the `monarch-maker` synth CardAttribute (the maker/payoff
  decomposition), `player-counter` gains the `energy-generator` producer (CR 122.1a), and the new
  `poisoned` event carries the poison-giver producers. The gate mechanism is **unchanged** (still
  DATA-keyed; only `mana-produced` carries a `produced_by_commander` Requirement) — these are
  producer-SET changes. Live OFF/ON delta: exactly 1 gauntlet deck moved (monarch engine in
  WakandaForever_2026); energy/poison proven live on synthetic decks. Only FULL_CORPUS moved:
  `38dd7733…` → `604f6673…`; engine + resolve unchanged. Proof:
  `~/.claude/plans/trippersham/magic-tools/2026-11-17-concept-anchored-self-healing/subagents/monarch-energy-poison/outbox.md`.

## Related records
- 0002 — the generic substantiveness framework; `produced_by_commander` is its first level-3 kind.
- 0003 — the byte-parity re-baseline this change carries (old→new SHAs + the proof).
- 0004 — `determine_node_admitted`'s `is_cmd` arm makes the anchor well-defined (a commander's
  `produced_by` otag is never dropped from `nodes`).
- 0012 — the Event ontology; promiscuity-scaled substantiveness is realized here.
- 0014 — the mass election whose candidate pool this gate filters (hook point + `lead`
  removal amended there).
- Issues: #71 (ramp decomposition — `ramp` out of mass, anchor-only), #72 (keep-ratio
  starvation — Omnath/Urza, the structural-threshold follow-up).
