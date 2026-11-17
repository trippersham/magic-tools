# 0017 Within-card edge derivation: native CardAttributes first, else haiku-hydrated into the machine store — never code-side prose parsing

> Numbering: this record sits outside the Design-2/3 planned block 0007–0016 (it was
> authored ahead of that fold, from a probe that violated the rule). It is closely related
> to planned 0008 (machine-store / `CardAttribute`+provenance), 0011 (`EventEffect` guards),
> and 0014 (engine election); during the fold it may be cross-linked or merged into 0008.

| Field | Value |
|---|---|
| Status | Accepted. Records committed behavior and bars the prose-parse anti-pattern. |
| Review | pending owner review: agent-authored; records the model as built, after a probe prototype violated it (see Context). |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests -q` (relay + machine-store tests cover both sources). A grep gate (`rg -n "oracle_text" pipeline/transforms/strategy` outside `query.py`/fixtures returns nothing new) guards against reintroduced prose parsing. |
| Code paths | [`edge_roles.py`](../../pipeline/pipeline/transforms/strategy/edge_roles.py) (`CURATED_EDGE_ROLES`, `Provenance(source='curated')`), [`machine_vocab.py`](../../pipeline/pipeline/transforms/strategy/machine_vocab.py) (`CardAttribute`, `ProvisionalAttributeStore`, `Provenance(source='haiku'|'spellbook')`, `edge_queries`), [`relay.py`](../../pipeline/pipeline/transforms/strategy/relay.py) (`build_relay_augmented_event_edges` — consumes both sources) |

## Context
A within-card edge is an `EventEffect`: `trigger_event → produced_event` with provenance.
The engine needs these to detect relay cycles and commander-anchored units (e.g. a
spellslinger commander's `spell-cast → token-created`). There are exactly two legitimate
sources of such an edge, and both already exist in committed code:

- **Native** — a curated `EdgeRole` keyed on a native CardAttribute/otag, carrying
  `Provenance(source='curated')` (the 6 hand-authored roles in `CURATED_EDGE_ROLES`).
- **Haiku** — a `CardAttribute` hydrated into the `ProvisionalAttributeStore`, carrying
  `Provenance(source='haiku')` and a `lifecycle` of provisional/verified.

A November 2026 probe prototype (engine-resolution waterfall) introduced a **third**
mechanism that is not in the design: a **regex over the commander's `oracle_text`**
("Whenever you cast …") to synthesize the produced-event end of the edge. It worked on the
sample but is the same anti-pattern this engine already rejected for card membership
(bespoke regex at the margins; queries/structured predicates beat prose parsing), and it is
brittle in exactly the predicted ways (a "Whenever X **attacks**, you may cast…" sentence
mis-parsed as a cast trigger; empty `oracle_text` on DFCs — issue #68 — falling back to an
over-broad otag blob). The rule it violated was real but lived **only in the committed code
and in conversation, never in an ADR** — which is why the probe (and the dispatch prompt
that steered it) missed it.

## Decision
A within-card edge is derived by a two-step waterfall, and by nothing else:

1. **Native CardAttributes first.** If native otags/CardAttributes name **both ends** of the
   edge (`trigger_event` and `produced_event`), author it as a curated `EdgeRole`
   (`Provenance(source='curated')`). Otags reliably name the *trigger* end (e.g.
   `cast-trigger-you`) but frequently **not** the structured *produced* end (token vs draw
   vs damage); when either end is not natively expressible, step 1 does not apply.
2. **Else haiku hydrates it into the machine store.** The missing edge is authored as a
   `CardAttribute` with `Provenance(source='haiku')`, `lifecycle=provisional`, feeding the
   same deterministic walk via `build_relay_augmented_event_edges`.

**No code-side prose parsing of `oracle_text` to derive edges, ever.** Card text is read by
the Scryfall query evaluator (`query.py`, for membership predicates) and by haiku (for edge
authoring) — not by bespoke regex in the transform layer.

### Two haiku roles are distinct and ranked
- **(A) Haiku-as-edge-author** — bounded, structured `trigger_event → produced_event` into
  the machine store, feeding the deterministic walk so the **engine still falls out of graph
  math**. This is the primary role and the subject of this record.
- **(B) Haiku-as-engine-synthesizer** — a freeform read of the whole decklist that *names* the
  engine directly. This is a separate, weaker last-resort fallback (the resolution
  waterfall's tier-2), not an edge-derivation mechanism, and must not substitute for (A)
  where (A) can author the edge.

## Reasons
- Consistency with ADR 0001 (knowledge placement): an edge over a card's internal causality
  is either a reference-data fact (curated) or an authored provisional fact (machine store);
  prose regex is neither home.
- The no-frozen-lists / queries-beat-regex principle: a hand-written regex over card text is
  a frozen, brittle enumeration of phrasings, the same class of smell as a name-list query.
- Provenance integrity: every edge must carry `source ∈ {curated, haiku, spellbook}` and a lifecycle;
  a regex-synthesized edge has no honest provenance and would pollute the verified set.

## Options not taken
- **Regex/NLP over `oracle_text` in the transform layer.** Rejected: brittle (attack-vs-cast
  mis-parse), blind on DFCs (#68), and provenance-less. It is the anti-pattern that prompted
  this record.
- **Haiku names the engine for every hard deck (engine-synthesizer everywhere).** Rejected: it abandons
  the graph derivation wherever a bounded edge (edge-author) would let the walk find the engine;
  engine-synthesizer is the last resort, not the default.

## Consequences
- The commander-anchored unit is still **constructed deterministically** from whatever edge
  exists, and it then **competes on mass** in the election like any other unit (ADR-to-come
  on engine election) — it is never auto-promoted into the engine.
- Results previously attributed to "deterministic tier-1" for the spellslinger family
  (Sokka, Talrand, Kykar) are re-attributed to **the edge-author role: haiku-authored edges feeding the
  deterministic walk** — same engine output, honest provisional provenance on the edge.
- Issue #68 (DFC `oracle_text` dropped at lake ingest) is a prerequisite for native/haiku
  edge quality on double-faced commanders; it is the root cause of the one bad election
  observed in the 170-deck regression (Ral, Monsoon Mage).

## Addendum (B1, 2026-11-14)

A deterministic combo-lowered edge path exists (`combo_edges.py`): it lowers EventEffects
from Spellbook combo fid-routes, provenance `source='spellbook'`.  This is NOT text-authoring
(no regex/NLP), so it does not violate the two text-authoring sources — it is the ADR 0021
Phase-A *Derive* box re-viewing fid structure.

## Addendum (monarch-maker, 2026-11-17, #74)

The `monarch` event's producer end is named by a **native query-membership CardAttribute**,
`monarch-maker` (`oracle:'become the monarch'`, `SYNTH_ALWAYS_ATTRIBUTES` in
`distill_strategies.py`, 62 makers). It is the first branch of the waterfall (native CardAttribute
names one end), not a haiku-hydrated provisional: membership is a deterministic oracle-text query
(like `rad-counter`), it distills to None (no archetype edge), and it supplies the `produced_by`
side of the `monarch` catalog node so the maker→payoff edge forms against the native payoff otag
`monarch-matters`. This is the sanctioned use of a synth CardAttribute to **decompose** a single
Scryfall tag that conflates producer and consumer roles — no prose parsing in code, no new source
value ([0008]).

## Related records
- 0001 — knowledge placement; an edge is curated reference data or an authored provisional
  fact, never prose parsed in code.
- 0003 — `edge_roles.py` participates in the byte-frozen curated layer.
- 0004 — single-implementation discipline: one derivation waterfall, not a third path.
