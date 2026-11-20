# 0002 Substantiveness is one generic determiner over a closed kind-set plus per-archetype data Requirements, never archetype-name branches

| Field | Value |
|---|---|
| Status | Accepted. Built at commit `0fca2ff`; in force. |
| Review | ratified by the owner on 2026-11-11. (Agent-authored; owner-ratified.) |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests -k substantiveness`. Asserts the kind determiners, `FAMILY_PREDICATES`, and `DEFAULT_REQUIREMENT`. |
| Code paths | [`determine_strategy_substantiveness.py`](../../pipeline/pipeline/transforms/strategy/determine_strategy_substantiveness.py), [`strategy_graph.py`](../../pipeline/pipeline/transforms/strategy/strategy_graph.py) (`requires_met` / the presence gate; shared with 0003 and the planned 0004). |

> **Three application sites, one framework.** The substantiveness question is asked at
> three places, all through Requirement KINDS dispatched by DATA — never a name branch:
> **(1) attribute admission** — `determine_node_admitted` (ADR 0004), level 1; **(2) archetype
> identity** — `requirement_for(arch, cfg)` → `_DETERMINERS`, level 2 (the body of this
> record) — and, as of 2026-11-15 (B6, [0014]/[0021]), this SAME level-2 verdict also gates
> **election candidacy** (`elect_engine_units`'s `earned_archetypes`), so one `satisfied` reading
> drives both the archetype LABEL and whether an archetype-claiming engine unit is elected (no
> model change, a second consumer); **(3) EVENT admission** — `determine_event(event, cmd_attrs, assets)` →
> `_EVENT_DETERMINERS`, level 3, gating whether a promiscuous event's engine unit may
> ENTER the mass election. The `producer_anchor` kind (added 2026-11-14) is the first
> level-3 kind. See the 2026-11-14 amendment below and **[0020](0020-event-substantiveness-producer-anchor.md)**.

## Context
Identity archetypes must be *earned* (a deck has to actually do the thing), and the bar
differs by archetype (payoff count, family density, a degree axis). The failure mode to
avoid is a growing chain of `if arch == '<name>'` substantiveness branches — mechanic
knowledge as code, un-extendable without editing the algorithm.

## Decision
Substantiveness is a `Requirement(kind, params)` dispatched by `_DETERMINERS` over a
**closed kind-set** — `{member_count, attr_density, axis_density}` — with **zero
archetype-name branches**. `requirement_for(arch, cfg)` selects an archetype's Requirement
by **data**: the EDHREC-new set → `member_count(3, noncmd=2)`; the `build_substantiveness`
map → the few density/axis bars; everything else → `DEFAULT_REQUIREMENT =
member_count(3, noncmd=0)`. The one family predicate (`is_graveyard_attr` in
`FAMILY_PREDICATES`) is deliberately **left as code** — a single family, so rule-of-two
does not earn a `{slugs}` table.

## Reasons
Mechanism-vs-parameters: one algorithm over a closed kind-set is auditable and extended by
adding a *kind* (shared, once) or a *data Requirement*, never a per-archetype branch. This
is the reference implementation of ADR 0001's doctrine.

## Options not taken
- **Per-archetype inline substantiveness** (an `arch == …` branch, or re-implementing the
  presence gate `n >= 3 or (has_cmd and n >= 2)` at each site). Rejected: that is exactly
  the copy-paste debt the fourth clause forbids — it already recurred (the presence gate
  exists four times; the consolidation collapses it, ADR 0004).

## Consequences
- `member_count(3, noncmd=0)` **short-circuits** (the `3` is inert when `noncmd <= 0`), so
  `satisfied == requires_met` for the default bar — which is why the spellslinger
  last-resort fallback is provably dead (deleted in the consolidation).
- `_gy_substantive` in `strategy_graph.py` is a parallel inline copy of `_determine_attr_density`; the
  consolidation deletes it and derives `gy_demoted` from `requirement_for()`.

## Amendment — 2026-11-14: `producer_anchor`, the third application site (EVENT admission)

A fourth Requirement KIND, `producer_anchor`, was added for a NEW application site: an
**event**'s admission to the mass election (ADR 0014). The Requirement-kind set now reads:

| kind | entity | site | determiner | registry |
|---|---|---|---|---|
| `member_count` | archetype | identity label | `_determine_member_count` | `_DETERMINERS` |
| `attr_density` | archetype | identity label | `_determine_attr_density` | `_DETERMINERS` |
| `axis_density` | archetype | identity label | `_determine_axis_density` | `_DETERMINERS` |
| `producer_anchor` | **event** | **election candidacy** | `_determine_producer_anchor` | `_EVENT_DETERMINERS` |

`producer_anchor` admits an event's unit iff the deck's commander carries at least one of
the event's own producer refs (`params['refs']`, default `produced_by`) ∪ an anchor-only
`params['extra_refs']`. It is ABSOLUTE and per-event — reads only commander attrs and the
event's own refs, never any unit's mass — so it is the event analog of `determine_node_admitted`'s
`is_cmd` arm, not a density bar. It dispatches through its OWN entry point
(`determine_event` → `_EVENT_DETERMINERS`), NOT the archetype `Requirement.satisfied`
path: the universal `requires_met` payoff gate does not apply to the event entity. The
one data row today is `mana-produced` (DATA-targeted; one row per promiscuous event, each
its own owner-graded measurement). The empirical case — why command-zone anchoring, and
not any density measure, carries event substantiveness — is **[0020](0020-event-substantiveness-producer-anchor.md)**.

## Amendment — 2026-11-16: `gy_demoted` hardcode removed — demotion is fully generic

The last archetype-name branch in `strategy_graph.py` — the hardcoded `'graveyard/reanimator'`
string in `gy_demoted` — is removed. Demotion (archetypes that clear `requires_met` but
fail `Requirement.satisfied()`) is now fully generic over the Requirement data, using the
same `earned_archetypes` set that feeds election candidacy. The old `gy_demoted` flag
tracked only graveyard; the generic `demoted_archetypes` list correctly reports all 6
demoted archetypes across the 170-deck corpus (graveyard x52 + mill/theft/enchantress/
vehicles/-1-1-counters x1 each) — a latent incompleteness now fixed. No consumed output
changed: `demoted_archetypes` is a diagnostic field read by no code path.

## Related records
- 0001 — the doctrine this implements.
- 0004 (planned) — the presence gate `determine_node_eligible` dedup routes through this framework.
- 0003 — `strategy_graph.py` is shared and byte-frozen.
- 0014 — the mass election whose candidate pool the level-3 gate filters.
- 0020 — event-substantiveness admission (the `producer_anchor` kind) + its empirical basis.
