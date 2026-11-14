# 0020 Event-substantiveness admission: the `producer_anchor` kind (command-zone production anchor)

| Field | Value |
|---|---|
| Status | Accepted. In force on branch `feat/strategy-engine`. Output-changing re-baseline (ADR 0003). |
| Review | pending owner review: agent-authored from the empirically-validated step-2 design (`event-substantiveness-empirical.md` + `mana-produced-correct-wiring.md`); the orchestrator reconciles, shows the owner the delta, and commits. |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests -q` (453 pass). The gate itself: `pytest -k "event_ontology or substantiveness"`. Parity gate: `uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle` — engine `987f2660…`, resolve `85752bb9…`, full-corpus `7e5190e8…`. Full-170 invariant (vs `7b53e6a`): **exactly 4 elected-set changes** (Selvala, Marwyn, Helga, Derevi gain `mana-produced`), **0 evictions**, **1 `named_strategy` change** (Ghave, from the consumer-side wiring, not the gate). |
| Code paths | [`substantiveness.py`](../../pipeline/pipeline/transforms/strategy/substantiveness.py) (`_eval_producer_anchor`, `_EVENT_EVALUATORS`, `EVENT_SUBSTANTIVENESS`, `event_admitted`), [`facts.py`](../../pipeline/pipeline/transforms/strategy/facts.py) (`_isolate_engines` — the candidate-pool filter), [`data/event_catalog_v4.json`](../../pipeline/pipeline/transforms/strategy/data/event_catalog_v4.json) (`mana-produced.produced_by`). |

## Context

Wiring the previously-inert `mana-produced` event (ADR 0012) into the mass election
(ADR 0014) made a *promiscuous resource event* electable. Mana production is near-ubiquitous
in Commander, so an un-gated mana unit dominated by raw mass: it elected on 13 decks under
the corrected wiring, of which only 4 were genuine mana strategies, and in the two densest
dork decks it EVICTED the deck's real engines (Ezuri's creature-attacks, Aang's six engines)
by sitting above the 0.6 keep-ratio floor. ADR 0014 deliberately left the election gated
only by `node_admitted` (a different output could not be gated inside the election by
mass-share — correctly rejected there as the wrong layer). What was missing is the EVENT
analog of `node_admitted`'s `is_cmd` arm: an ABSOLUTE, per-event admission check that decides
whether a promiscuous event's unit is an election candidate *at all*, answerable from the
deck alone before the election.

## Decision

Add a third substantiveness application site (ADR 0002): **event admission**, a new
Requirement KIND `producer_anchor`, dispatched by DATA through its own entry point.

- **Measure — command-zone PRODUCTION anchor.** An event's unit is admitted to election
  candidacy iff the deck's COMMANDER carries at least one attribute in the event's own
  producer refs. `_eval_producer_anchor(params, event, cmd_attrs, assets)` returns
  `bool(cmd_attrs & (set(assets.events[event][params['refs']]) | set(params['extra_refs'])))`.
  It is ABSOLUTE and per-event: it reads only the commander's node-admitted attrs and the
  event's own wiring refs — **never** total/top/other-unit mass, centrality, or share.
- **Data row (the only one today).** `EVENT_SUBSTANTIVENESS = {'mana-produced':
  Requirement('producer_anchor', {'refs': 'produced_by', 'extra_refs': ['ramp']})}`.
  An event with no row is admitted (preserves pre-gate behavior). Promiscuity-targeted by
  DATA — one row per promiscuous event, each its own owner-graded measurement.
- **The `ramp`-in-anchor-not-in-`produced_by` split (load-bearing).** The corrected
  `produced_by = [adds-multiple-mana, mana-producer, repeatable-treasures, ritual]` drops
  the umbrella `ramp` so it no longer inflates mana MASS (#71 ramp decomposition). But a
  ramp-only commander (Derevi) still anchors a genuine mana engine carried by the 99's
  specific producers, so `ramp` is carried ANCHOR-SIDE only, via `extra_refs` — a
  commander-tag check that contributes zero election mass. Dropping it (gateA) costs
  exactly one genuine deck (Derevi); keeping it anchor-side (gateB) is the chosen config.
- **Hook point.** In `_isolate_engines`, between unit construction and the `elected`
  keep-walk: `pool = [e for e in engines if event_admitted(e['event'], cmd_attrs, assets)]`;
  the keep-walk runs over `pool` (its `top_mass`/`total_mass` taken over the pool). The
  reported `engines` list stays COMPLETE — exclusion is election-candidacy only, so
  diagnostics, chains, hubs and the loop probe are untouched.
- **Generic.** Keyed on the event's Requirement DATA — no `if mana`, no deck-name branch,
  no new numeric knob. The measure degenerates to set-nonemptiness (|cmd ∩ refs| ≥ 1):
  structural, with no threshold to tune or drift.

## Reasons — why anchoring, not density (the empirical core)

Per-event *density* does NOT encode substantiveness for a resource event. Every absolute
density measure was swept and overlaps badly: specific-producer adens (Ezuri 29, Tayam 31
exceed every genuine deck but Marwyn; Urza sits at 6 below ten incidental decks),
consumer-side adens (Ezuri/Tovolar/Lathliss out-consume every genuine deck), and the
specific:generic ratio (Ezuri is the corpus max). Incidental dork-dense decks are
compositionally indistinguishable from — often denser than — genuine mana decks. The one
absolute quantity that separates them is WHERE production is anchored: every genuine deck's
commander IS a mana producer; the incidental decks' commanders carry no mana attr or are
tagged only as sinks/consumers. A commander that *consumes* the resource does not earn the
resource event; one that *produces* it does. Combination clauses (anchor AND/OR depth≥T,
consumer clauses) are all strictly worse (false-gate Urza or re-admit Ezuri at every T).

**Robustness.** The gate quantity is integer and bimodal (0 vs 1–3); no ±k ordinary-card
swap can flip any deck's verdict — only the commander's own otags enter, and commander
attrs are node-admitted unconditionally (ADR 0004). A ±2-adens perturbation of the mana
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

## Related records
- 0002 — the generic substantiveness framework; `producer_anchor` is its first level-3 kind.
- 0003 — the byte-parity re-baseline this change carries (old→new SHAs + the proof).
- 0004 — `node_admitted`'s `is_cmd` arm makes the anchor well-defined (a commander's
  `produced_by` otag is never dropped from `nodes`).
- 0012 — the Event ontology; promiscuity-scaled substantiveness is realized here.
- 0014 — the mass election whose candidate pool this gate filters (hook point + `lead`
  removal amended there).
- Issues: #71 (ramp decomposition — `ramp` out of mass, anchor-only), #72 (keep-ratio
  starvation — Omnath/Urza, the structural-threshold follow-up).
