"""Event-candidate evaluation (seed discovery) loop (task 6.3 + R5c rewire).

The machinery here lets the concept-anchored adjacency EXTEND ITSELF from
evidence.  When an otag appears on corroborated cards for an event side but is
NOT yet in the side's seed closure, it becomes a **candidate seed** — provided
it passes the SAME :func:`~pipeline.transforms.strategy.derive_card_events.is_mechanic_exclusive`
gate as the seeds (so umbrellas like ``ramp`` can never re-enter).  A candidate
promotes to the seed set when ``>= N=3`` independent cards corroborate it.

Concept-anchored discovery (R5c, #74 / WS3 layer 2)
---------------------------------------------------
The producer side is scored against each event's **ratified PRODUCER CONCEPT**
(:data:`PRODUCER_CONCEPTS` — owner-ratified DATA, anchored on the CR rule), NOT
the circular *current closure*.  The circular reference is the bug the WS3
ratification found: ``mechanic_population(CURRENT seeds)`` is the reference, so a
genuinely-on-mechanic *new* family that is not yet in the closure scores foreign
exactly like a disconnected family — self-healing cannot tell them apart
(``impulse-seed-scope.md`` §A2: the double blind spot).  Anchoring the gate on
the stable concept (decoupled from the mutable seeds) fixes it: a producer family
dropped from the seeds is *re-derived* from the concept (the WS2 impulse-seed
ground-truth, ``TestWS2GroundTruthRederivation``).

Two gates beyond precision (precision is necessary-but-not-sufficient — the v2
ratification found ``group-slug`` 0.973 / ``inverted-effects`` 0.825 FALSE-pass):

* **role-purity** — a payoff-family (``synergy-*`` / ``*-matters`` / ``*-fuel-*``
  / ``*-cares``) otag is never admitted as a PRODUCER (that is the archetype
  layer, ADR 0012/0025).  Reuses :func:`.catalog_integrity.is_payoff_family`
  (one rule, one impl — ADR 0001).
* **decomposition-escalation** — when the concept can only be served by a COARSE
  role-impure otag (the monarch ``monarch-matters`` maker+payoff conflation),
  discovery does NOT promote the noisy otag; it emits an owner-queue
  :class:`DecompositionTicket` ("author a CardAttribute for <concept>").

**Fixed vs growable (DATA).** The synth-attr / curated-union producer events
(monarch, domain, permanent-dies, poisoned, rad, experience) are
:attr:`ConceptKind.FIXED_SYNTH` — their coverage IS the authored query, which
auto-evolves with the pool, so discovery SKIPS growing them (growing them would
re-introduce the coarse-otag noise the CardAttribute was authored to avoid).
Discovery's self-healing value is concentrated in the 5 bootstrap-gap (b)
:attr:`ConceptKind.GROWABLE` otag-family events.

The mechanism is SHADOW — nothing here is imported by the live derive path
(:mod:`.assets` / :mod:`.engine_hydration`), so the authoritative derived
adjacency (:func:`~pipeline.transforms.strategy.derive_card_events.derive_card_events`)
and the three P0 SHAs are structurally unchanged by this code.  The discovery
layer depends on the derive core + the layer-1 guards one-directionally; never
the reverse.  :func:`run_concept_shadow` / :func:`write_shadow_proposals` emit an
owner-review artifact; they mutate NO live catalog file.
"""

from __future__ import annotations

import enum
import json as _json
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

from pipeline.transforms.strategy.catalog_integrity import is_payoff_family
from pipeline.transforms.strategy.derive_card_events import (
    SEED_MAP,
    descendant_closure,
    is_mechanic_exclusive,
    load_taxonomy,
    mechanic_population,
)

#: Promotion threshold: a candidate must be corroborated by this many
#: independent cards (distinct oracle_ids) before it is eligible for promotion.
PROMOTION_THRESHOLD: int = 3


# ---------------------------------------------------------------------------
# Ratified producer concepts (R5c) — owner-ratified DATA, anchored on the CR
# rule, never auto-climbed.  This replaces the circular current-closure as the
# gate's reference.  See research/event-concepts-for-ratification-v2.md and
# ratified-concepts-implementation.md (owner-ratified 2026-11-18).
# ---------------------------------------------------------------------------


class ConceptKind(enum.Enum):
    """Whether discovery grows an event's producer concept.

    * ``GROWABLE`` — the concept is a real otag FAMILY that legitimately grows
      with the card pool (the 5 bootstrap-gap (b) events + the (a)-clean
      frontier events).  Discovery proposes on-concept members not yet seeded.
    * ``FIXED_SYNTH`` — the concept IS an authored CardAttribute query / curated
      union (monarch-maker, color-fixing, dies-removal, rad-counter,
      experience-counter, the poison-giver union).  Its coverage auto-evolves at
      distill time, so discovery must NOT grow it (growing it re-introduces the
      coarse-otag noise the attribute was authored to avoid).
    """

    GROWABLE = 'growable'
    FIXED_SYNTH = 'fixed-synth'


@dataclass(frozen=True)
class ProducerConcept:
    """An event's ratified PRODUCER concept — the gate's stable reference.

    ``anchors`` is the owner-ratified otag set that semantically defines "what
    PRODUCES this event" (an otag family / taxonomy neighborhood).  It is DATA,
    frozen independently of the mutable ``SEED_MAP`` seeds, so a producer family
    dropped from the seeds is still re-derivable from the concept (self-heal).
    """

    event: str
    anchors: tuple[str, ...]
    kind: ConceptKind
    note: str = ''


#: The six FIXED-reference events (synth-attr / curated-union producers).  Their
#: producer concept is an authored query, authoritative + auto-evolving; the
#: ``anchors`` record the authored producer attribute(s) for provenance only —
#: discovery skips growing them (ratified-concepts-implementation.md §R5c).
_FIXED_CONCEPTS: dict[str, ProducerConcept] = {
    'monarch': ProducerConcept(
        'monarch', ('monarch-maker',), ConceptKind.FIXED_SYNTH,
        "authored synth attr `monarch-maker` (oracle:'become the monarch')",
    ),
    'domain': ProducerConcept(
        'domain', ('color-fixing',), ConceptKind.FIXED_SYNTH,
        "authored synth attr `color-fixing` (oracle:'mana of any color' + rainbow-land etc.)",
    ),
    'permanent-dies': ProducerConcept(
        'permanent-dies', ('dies-removal',), ConceptKind.FIXED_SYNTH,
        'authored subtractive synth attr `dies-removal` (otag:removal minus exile/-bounce/-tuck) + sacrifice family',
    ),
    'poisoned': ProducerConcept(
        'poisoned',
        ('gives-infect', 'gives-poisonous', 'gives-toxic', 'poison-opponents', 'poisonous', 'gains-poisonous'),
        ConceptKind.FIXED_SYNTH,
        'curated poison-giver union (bounded below the refused `poison-mechanics` parent)',
    ),
    'rad': ProducerConcept(
        'rad', ('rad-counter',), ConceptKind.FIXED_SYNTH,
        'text-synth attr `rad-counter` (no otag — synthetic-inert)',
    ),
    'experience': ProducerConcept(
        'experience', ('experience-counter',), ConceptKind.FIXED_SYNTH,
        "oracle-text synth attr `experience-counter` (oracle:'experience counter')",
    ),
}

#: The five GROWABLE bootstrap-gap (b) events — the frozen ratified concept is
#: the owner-approved on-mechanic producer FAMILY (snapshot of the ratified
#: produced_by seeds, now DECOUPLED from the mutable SEED_MAP so a dropped seed
#: self-heals).  These are where shadow surfaces real new members.
_GROWABLE_B_CONCEPTS: dict[str, ProducerConcept] = {
    'creature-enters-battlefield': ProducerConcept(
        'creature-enters-battlefield',
        (
            'flicker', 'flicker-creature', 'multiple-bodies', 'reanimate-artifact-creature',
            'reanimate-creature', 'reanimate-nonland', 'reanimate-permanent',
            'repeatable-creature-tokens', 'sneak-creature', 'sneak-from-library', 'tokens',
        ),
        ConceptKind.GROWABLE,
        'creature onto battlefield: creature-token-gen + reanimate + flicker + onto-bf (sneak/cheat-into-play)',
    ),
    'artifact-enters': ProducerConcept(
        'artifact-enters',
        ('artifacts', 'repeatable-artifact-tokens', 'repeatable-treasures', 'sneak-artifact'),
        ConceptKind.GROWABLE,
        'artifact onto battlefield: artifacts + artifact-tokens + treasures + sneak-artifact',
    ),
    'land-enters-battlefield': ProducerConcept(
        'land-enters-battlefield',
        ('extra-land', 'land-ramp', 'reanimate-land', 'recursion-land', 'sneak-land', 'tutor-land-to-battlefield'),
        ConceptKind.GROWABLE,
        'land onto battlefield: land-ramp + extra-land + sneak-land + reanimate/recursion-land + tutor-land-to-bf',
    ),
    'token-created': ProducerConcept(
        'token-created',
        (
            'artifacts', 'creates-bigger-body', 'leaves-body-behind', 'multiple-bodies',
            'out-of-color-token', 'repeatable-artifact-tokens', 'repeatable-clues',
            'repeatable-creature-tokens', 'repeatable-food', 'repeatable-noncreature-tokens',
            'repeatable-token-generator', 'repeatable-treasures', 'tokens',
        ),
        ConceptKind.GROWABLE,
        'mints a token: repeatable-token-generator + death-to-token + off-color + chain',
    ),
    'creature-copied': ProducerConcept(
        'creature-copied',
        ('clone', 'copy-creature', 'copy-self', 'copy-token'),
        ConceptKind.GROWABLE,
        'mints a creature copy: clone + copy-creature + copy-token + copy-self',
    ),
}

#: The owner-ratified producer-concept table (the R5c DATA anchor).  Events not
#: listed default (via :func:`producer_concept`) to a GROWABLE concept whose
#: anchors == the current ``produced_by`` seeds — the (a)-clean legend "concept
#: == current seeds" (v2 §(a)); the next climb up the taxonomy is a refused
#: umbrella, so discovery proposes only genuinely on-mechanic off-closure members.
PRODUCER_CONCEPTS: dict[str, ProducerConcept] = {**_FIXED_CONCEPTS, **_GROWABLE_B_CONCEPTS}


def producer_concept(event: str) -> ProducerConcept:
    """The ratified PRODUCER concept for *event* (the gate's stable reference).

    Explicit entries in :data:`PRODUCER_CONCEPTS` win; otherwise the concept
    defaults to a ``GROWABLE`` concept whose anchors are the event's current
    ``produced_by`` seeds — the owner-ratified (a)-clean "concept == current
    seeds" case.
    """
    if event in PRODUCER_CONCEPTS:
        return PRODUCER_CONCEPTS[event]
    entry = SEED_MAP.get(event)
    anchors = tuple(entry['produced_by'].get('seeds', [])) if entry else ()
    return ProducerConcept(
        event, anchors, ConceptKind.GROWABLE,
        'default: concept == current produced_by seeds (class-a clean / frontier-confirmed)',
    )


@dataclass(frozen=True)
class DecompositionTicket:
    """Owner-queue request: author a CardAttribute instead of promoting a coarse otag.

    Emitted when a growable concept can only be served by a COARSE, role-impure
    otag (one that mixes producers and payoffs) — the monarch/color-fixing
    pattern (#74's "don't whack-a-mole, escalate to the right fix").
    """

    event: str
    coarse_otag: str
    reason: str
    role_impure_examples: tuple[str, ...]


@dataclass(frozen=True)
class ConceptDiscovery:
    """Outcome of concept-anchored producer discovery for one event."""

    event: str
    candidates: tuple[CandidateSeed, ...]
    role_rejected: tuple[str, ...]
    decomposition_ticket: DecompositionTicket | None
    skipped_fixed: bool


def discover_for_concept(
    concept: ProducerConcept,
    current_seeds: Sequence[str],
    otag_evidence: Mapping[str, AbstractSet[str]],
) -> ConceptDiscovery:
    """Concept-anchored producer discovery (the R5c core) — shadow, pure.

    Scores producer candidates against *concept* (the ratified reference) rather
    than *current_seeds* (the circular closure).  A candidate otag is proposed iff
    it is NOT already in the current seed closure, IS in the taxonomy, is
    **mechanic-exclusive against the CONCEPT population** (the fix — the gate's
    reference is the concept, not the mutable seeds), and passes **role-purity**
    (not a payoff-family otag).

    * ``FIXED_SYNTH`` concepts are SKIPPED (0 proposals) before any lake access.
    * A coarse, role-impure (payoff-family) concept anchor emits a
      :class:`DecompositionTicket` instead of promoting the noisy otag.
    """
    # (1) Fixed reference — the authored query is authoritative; never grow it.
    if concept.kind is ConceptKind.FIXED_SYNTH:
        return ConceptDiscovery(concept.event, (), (), None, skipped_fixed=True)

    # (2) Decomposition-escalation — a payoff-family (role-impure) concept anchor
    # means no clean PRODUCER otag serves the concept (the monarch-matters
    # maker+payoff conflation).  Emit a ticket; do NOT promote the coarse otag.
    coarse = sorted(a for a in concept.anchors if is_payoff_family(a))
    if coarse:
        ticket = DecompositionTicket(
            event=concept.event,
            coarse_otag=coarse[0],
            reason=(
                f'the producer-concept anchor {coarse[0]!r} is a payoff-family (cares-about) otag — '
                f'it mixes producers and payoffs, so no clean producer otag serves this concept. '
                f'Author a CardAttribute (producer-only query) for the concept instead of promoting '
                f'the coarse otag (ADR 0009 query-membership / ADR 0017 native-first).'
            ),
            role_impure_examples=tuple(coarse),
        )
        return ConceptDiscovery(concept.event, (), (), ticket, skipped_fixed=False)

    # (3) Concept-anchored discovery proper.
    taxonomy = load_taxonomy()
    concept_closure: set[str] = set()
    for anchor in concept.anchors:
        concept_closure |= descendant_closure(anchor, taxonomy)
    seed_closure: set[str] = set()
    for seed in current_seeds:
        seed_closure |= descendant_closure(seed, taxonomy)
    mechanic = mechanic_population(concept.anchors)

    candidates: dict[str, set[str]] = {}
    role_rejected: set[str] = set()
    for oracle_id, card_otags in otag_evidence.items():
        # On-concept corroboration: the card carries at least one concept otag.
        if not (card_otags & concept_closure):
            continue
        for otag in card_otags:
            if otag in seed_closure:  # already wired
                continue
            if otag not in taxonomy:  # synthetic / not a real otag
                continue
            # The gate's reference is the CONCEPT population (the R5c fix).
            if not _cached_exclusivity_check(otag, mechanic):
                continue
            # Role-purity: a payoff is never a producer.
            if is_payoff_family(otag):
                role_rejected.add(otag)
                continue
            candidates.setdefault(otag, set()).add(oracle_id)

    cand_seeds = tuple(
        CandidateSeed(event=concept.event, side='produced_by', otag=otag, corroborating_cards=frozenset(cards))
        for otag, cards in sorted(candidates.items())
    )
    return ConceptDiscovery(concept.event, cand_seeds, tuple(sorted(role_rejected)), None, skipped_fixed=False)


class GateVerdict(enum.Enum):
    """Three-way corroboration gate outcomes."""

    #: The otag is in the current seed closure — accept immediately.
    ACCEPT_VIA_SEED = 'accept-via-seed'

    #: The otag is on-mechanic and passes exclusivity, but is not yet in
    #: the closure — accept provisionally and propose as a candidate seed.
    ACCEPT_PROVISIONAL = 'accept-provisional'

    #: The otag fails exclusivity or is genuinely loose — reject.
    REJECT = 'reject'


@dataclass(frozen=True)
class GateResult:
    """Outcome of the three-way gate for a single otag."""

    verdict: GateVerdict
    otag: str
    reason: str


@dataclass(frozen=True)
class CandidateSeed:
    """A candidate seed otag discovered from evidence."""

    event: str
    side: str  # 'triggers' | 'produced_by'
    otag: str
    corroborating_cards: frozenset[str]  # oracle_ids

    @property
    def corroboration_count(self) -> int:
        return len(self.corroborating_cards)


class CandidateSeedStore:
    """In-memory store for candidate seeds with corroboration tracking.

    Records candidate otags and the independent cards that corroborate them.
    A candidate promotes when its corroboration count reaches
    :data:`PROMOTION_THRESHOLD`.  Reuses the ADR-0008 provisional->verified
    lifecycle concept.
    """

    def __init__(self) -> None:
        # Key: (event, side, otag) -> set of oracle_ids
        self._candidates: dict[tuple[str, str, str], set[str]] = {}

    def record_corroboration(
        self,
        event: str,
        side: str,
        otag: str,
        card_oracle_id: str,
    ) -> None:
        """Record that *card_oracle_id* corroborates *otag* for *event.side*."""
        key = (event, side, otag)
        if key not in self._candidates:
            self._candidates[key] = set()
        self._candidates[key].add(card_oracle_id)

    def promotable_candidates(
        self,
        threshold: int = PROMOTION_THRESHOLD,
    ) -> list[CandidateSeed]:
        """Return candidates with >= *threshold* independent corroborating cards."""
        result: list[CandidateSeed] = []
        for (event, side, otag), cards in sorted(self._candidates.items()):
            if len(cards) >= threshold:
                result.append(
                    CandidateSeed(
                        event=event,
                        side=side,
                        otag=otag,
                        corroborating_cards=frozenset(cards),
                    )
                )
        return result

    def all_candidates(self) -> list[CandidateSeed]:
        """Return ALL candidates regardless of corroboration count."""
        result: list[CandidateSeed] = []
        for (event, side, otag), cards in sorted(self._candidates.items()):
            result.append(
                CandidateSeed(
                    event=event,
                    side=side,
                    otag=otag,
                    corroborating_cards=frozenset(cards),
                )
            )
        return result

    @property
    def total_candidates(self) -> int:
        return len(self._candidates)


def _closure_for_side(event_name: str, side: str) -> frozenset[str]:
    """Compute the descendant-closure of all seeds for *event_name.side*."""
    taxonomy = load_taxonomy()
    entry = SEED_MAP.get(event_name)
    if entry is None:
        return frozenset()
    seeds = entry[side].get('seeds', [])
    closure: set[str] = set()
    for seed in seeds:
        closure |= descendant_closure(seed, taxonomy)
    return frozenset(closure)


def three_way_gate(
    otag: str,
    events: dict[str, dict],
    event_name: str,
    side: str,
) -> GateResult:
    """Classify *otag* via the three-way corroboration gate.

    - **accept-via-seed**: otag is already in the side's seed closure.
    - **accept-provisional**: otag passes ``is_mechanic_exclusive`` against
      the side's mechanic but is not yet in the closure — propose as
      candidate seed.
    - **reject**: otag fails exclusivity or has no cards (genuinely loose).

    GENERIC: no per-event branches, no hand-listed umbrellas.
    """
    closure = _closure_for_side(event_name, side)

    # (1) Already in closure — immediate accept.
    if otag in closure:
        return GateResult(
            verdict=GateVerdict.ACCEPT_VIA_SEED,
            otag=otag,
            reason='in seed closure',
        )

    # (2) Check mechanic exclusivity.
    entry = SEED_MAP.get(event_name)
    if entry is None:
        return GateResult(
            verdict=GateVerdict.REJECT,
            otag=otag,
            reason=f'event {event_name!r} not in SEED_MAP',
        )

    seeds = entry[side].get('seeds', [])
    if not seeds:
        return GateResult(
            verdict=GateVerdict.REJECT,
            otag=otag,
            reason=f'{event_name}.{side} has no seeds',
        )

    mechanic = mechanic_population(seeds)
    excl = is_mechanic_exclusive(otag, mechanic)

    if excl.ok:
        return GateResult(
            verdict=GateVerdict.ACCEPT_PROVISIONAL,
            otag=otag,
            reason=f'exclusive (precision={excl.precision:.3f}) but not in closure',
        )

    return GateResult(
        verdict=GateVerdict.REJECT,
        otag=otag,
        reason=f'not exclusive: {excl.reason}',
    )


def discover_candidates(
    events: dict[str, dict],
    event_name: str,
    side: str,
    otag_evidence: dict[str, set[str]] | None = None,
) -> list[CandidateSeed]:
    """Discover candidate seed otags for *event_name.side* from evidence.

    For each card in *otag_evidence* (oracle_id -> set of otags), if the card
    carries at least one otag in the current closure (i.e. it is a corroborated
    card for this event/side), collect all its OTHER otags that are:

    - NOT already in the closure
    - In the taxonomy (not synthetic)
    - Mechanic-exclusive for the side's mechanic

    Returns candidate seeds with their corroborating cards.

    GENERIC: no per-event branches, no hand-listed candidates or blocklists.

    R5c: the **producer** side is now anchored on the ratified PRODUCER CONCEPT
    (:func:`producer_concept`) with role-purity + fixed-vs-growable, delegating to
    :func:`discover_for_concept` — a FIXED concept returns ``[]`` and a coarse
    concept proposes nothing (its ticket is surfaced via the richer
    :func:`discover_for_concept` / :func:`run_concept_shadow` path).  The
    **triggers** (consumer/payoff) side keeps the current-closure behaviour — the
    ratified concepts govern producers only.

    Parameters
    ----------
    events
        The derived events dict (from ``derive_card_events``).
    event_name
        Event to discover candidates for.
    side
        ``'triggers'`` or ``'produced_by'``.
    otag_evidence
        ``{oracle_id: {otag, ...}}`` evidence from the corpus.  If ``None``,
        loads the default residue_cards.jsonl.
    """
    if otag_evidence is None:
        otag_evidence = _load_default_evidence()

    # Producer side — concept-anchored (R5c).
    if side == 'produced_by':
        entry = SEED_MAP.get(event_name)
        if entry is None:
            return []
        current_seeds = entry['produced_by'].get('seeds', [])
        return list(discover_for_concept(producer_concept(event_name), current_seeds, otag_evidence).candidates)

    # Trigger side — current-closure anchoring (producers concepts govern producers).
    closure = _closure_for_side(event_name, side)
    if not closure:
        return []

    entry = SEED_MAP.get(event_name)
    if entry is None:
        return []

    seeds = entry[side].get('seeds', [])
    if not seeds:
        return []

    mechanic = mechanic_population(seeds)
    taxonomy = load_taxonomy()

    # Accumulate: candidate_otag -> set of corroborating oracle_ids
    candidates: dict[str, set[str]] = {}

    for oracle_id, card_otags in otag_evidence.items():
        # Is this card corroborated for this event/side?
        if not (card_otags & closure):
            continue

        # Collect otags NOT in closure, IN taxonomy, AND exclusive
        for otag in card_otags:
            if otag in closure:
                continue
            if otag not in taxonomy:
                continue
            # Check exclusivity (cached)
            if not _cached_exclusivity_check(otag, mechanic):
                continue

            if otag not in candidates:
                candidates[otag] = set()
            candidates[otag].add(oracle_id)

    return [
        CandidateSeed(
            event=event_name,
            side=side,
            otag=otag,
            corroborating_cards=frozenset(cards),
        )
        for otag, cards in sorted(candidates.items())
    ]


@cache
def _cached_exclusivity_check(otag: str, mechanic: frozenset[str]) -> bool:
    """Cached wrapper: is *otag* mechanic-exclusive for *mechanic*?"""
    return is_mechanic_exclusive(otag, mechanic).ok


def _load_default_evidence() -> dict[str, set[str]]:
    """Load oracle_id -> set(otags) from the default residue_cards.jsonl."""

    evidence_path = (
        Path(__file__).resolve().parent.parent.parent.parent / 'subagents' / '5.3b-work' / 'residue_cards.jsonl'
    )
    result: dict[str, set[str]] = {}
    if not evidence_path.exists():
        return result
    with evidence_path.open() as f:
        for line in f:
            c = _json.loads(line)
            result[c['oracle_id']] = set(c.get('otags', []))
    return result


# ---------------------------------------------------------------------------
# Shadow measurement (task 6.3) — what would the seed-discovery loop discover?
# ---------------------------------------------------------------------------


@dataclass
class CandidateEvaluationReport:
    """Results of running the seed-discovery loop in shadow mode."""

    #: Total candidate otags discovered across all events/sides.
    total_candidates: int = 0

    #: Candidates that would promote at N=3.
    promotable_count: int = 0

    #: Number of umbrella-like otags that were PROPOSED (should be 0).
    umbrella_proposals: int = 0

    #: Projected corroboration percentage with promoted candidates.
    projected_unlock_pct: float = 0.0

    #: Current corroboration percentage (baseline).
    current_unlock_pct: float = 0.0

    #: Per-event candidate details.
    per_event: dict[str, dict[str, Any]] = field(default_factory=dict)

    #: The promotable candidates themselves.
    promotable: list[CandidateSeed] = field(default_factory=list)

    #: All candidates (for the research report).
    all_candidates: list[CandidateSeed] = field(default_factory=list)

    #: Extended adjacency slugs (what promotion would add).
    projected_new_slugs: dict[str, dict[str, set[str]]] = field(default_factory=dict)


def run_candidate_evaluation_shadow(
    otag_evidence: dict[str, set[str]],
) -> CandidateEvaluationReport:
    """Run the event-candidate discovery + promotion in SHADOW mode.

    Does NOT modify the authoritative adjacency.  Returns a report of what
    the seed-discovery loop would discover, promote, and unlock.

    Parameters
    ----------
    otag_evidence
        ``{oracle_id: {otag, ...}}`` evidence from the corpus residue.
    """

    from pipeline.transforms.strategy.engine_hydration import _load_events

    events = _load_events()
    store = CandidateSeedStore()
    report = CandidateEvaluationReport()

    # Phase 1: Discover candidates for every event/side
    for event_name, entry in SEED_MAP.items():
        event_report: dict[str, Any] = {}
        for side in ('triggers', 'produced_by'):
            seeds = entry[side].get('seeds', [])
            if not seeds:
                event_report[side] = {'candidates': 0, 'promotable': 0, 'slugs': []}
                continue

            candidates = discover_candidates(events, event_name, side, otag_evidence)
            for cand in candidates:
                for card in cand.corroborating_cards:
                    store.record_corroboration(event_name, side, cand.otag, card)

            event_report[side] = {
                'candidates': len(candidates),
                'slugs': [c.otag for c in candidates],
            }

        report.per_event[event_name] = event_report

    # Phase 2: Identify promotable candidates
    promotable = store.promotable_candidates()
    all_cands = store.all_candidates()

    report.total_candidates = store.total_candidates
    report.promotable_count = len(promotable)
    report.promotable = promotable
    report.all_candidates = all_cands

    # Phase 3: Project adjacency extension
    taxonomy = load_taxonomy()
    new_slugs: dict[str, dict[str, set[str]]] = {}
    for cand in promotable:
        closure = _closure_for_side(cand.event, cand.side)
        added = descendant_closure(cand.otag, taxonomy) - closure
        if added:
            if cand.event not in new_slugs:
                new_slugs[cand.event] = {}
            if cand.side not in new_slugs[cand.event]:
                new_slugs[cand.event][cand.side] = set()
            new_slugs[cand.event][cand.side] |= added

    report.projected_new_slugs = new_slugs

    # Phase 4: Project unlock on edge-author edges
    edges_dir = Path(__file__).resolve().parent.parent.parent.parent / 'subagents' / '5.3b-work'

    role_a_edges: list[dict] = []
    import glob as _glob

    for f in sorted(_glob.glob(str(edges_dir / 'edges' / 'chunk-*-edges.jsonl'))):
        with open(f) as fh:
            for line in fh:
                e = _json.loads(line)
                if e.get('trigger_event') and e.get('produced_event'):
                    role_a_edges.append(e)

    if not role_a_edges:
        return report

    def _corroborates(edge: dict, ev_dict: dict[str, dict]) -> bool:
        otags = otag_evidence.get(edge['oracle_id'], set())
        t_list = set(ev_dict.get(edge['trigger_event'], {}).get('triggers', []))
        p_list = set(ev_dict.get(edge['produced_event'], {}).get('produced_by', []))
        return bool(otags & t_list) and bool(otags & p_list)

    n_edges = len(role_a_edges)
    current_corr = sum(1 for e in role_a_edges if _corroborates(e, events))
    report.current_unlock_pct = current_corr / n_edges * 100

    # Build projected events with promoted candidates added to the closure
    projected_events = _build_projected_events(events, promotable)
    projected_corr = sum(1 for e in role_a_edges if _corroborates(e, projected_events))
    report.projected_unlock_pct = projected_corr / n_edges * 100

    return report


def _build_projected_events(
    events: dict[str, dict],
    promotable: list[CandidateSeed],
) -> dict[str, dict]:
    """Build a projected events dict with promoted candidates in the adjacency.

    This is SHADOW-ONLY — the authoritative adjacency is not modified.
    """
    taxonomy = load_taxonomy()
    projected: dict[str, dict] = {}

    for event_name, ev in events.items():
        projected[event_name] = dict(ev)

    for cand in promotable:
        if cand.event not in projected:
            continue
        ev = dict(projected[cand.event])
        current_list = set(ev.get(cand.side, []))
        # Add the promoted candidate's descendant closure
        current_list |= descendant_closure(cand.otag, taxonomy)
        ev[cand.side] = sorted(current_list)
        projected[cand.event] = ev

    return projected


# ---------------------------------------------------------------------------
# Concept-anchored shadow run (R5c) — the owner-review proposals artifact.
#
# SHADOW-ONLY: these read the live lake + the frozen catalog and RETURN / WRITE a
# proposals report.  They mutate no live catalog file (SEED_MAP / event_catalog.
# json / the derived closure) — the owner ratifies promotions later, the same gate
# the ratification walk-through used.
# ---------------------------------------------------------------------------


@cache
def build_lake_evidence() -> dict[str, frozenset[str]]:
    """``oracle_id -> frozenset(otags)`` from the live ``normalized/card_otag.parquet``.

    The strongest evidence set (every card→otag over the full corpus), the same
    source ``impulse-seed-scope.md`` §A1 used.  Cached (the lake is immutable
    within a run).  Requires the lake; callers gate on lake availability.
    """
    import duckdb

    from pipeline.transforms.strategy.derive_card_events import _taxonomy_path

    path = _taxonomy_path().parent.parent / 'normalized' / 'card_otag.parquet'
    if not path.exists():
        raise FileNotFoundError(f'card_otag.parquet not found at {path}; hydrate with `collection hydrate-lake`')

    con = duckdb.connect(':memory:')
    try:
        rows = con.execute(f"SELECT CAST(oracle_id AS VARCHAR), slug FROM read_parquet('{path}')").fetchall()
    finally:
        con.close()

    acc: dict[str, set[str]] = {}
    for oracle_id, slug in rows:
        acc.setdefault(oracle_id, set()).add(slug)
    return {oid: frozenset(slugs) for oid, slugs in acc.items()}


@dataclass(frozen=True)
class ShadowProposalsReport:
    """Aggregate concept-anchored shadow report (owner-review queue).

    * ``growable`` — ``event -> ConceptDiscovery`` for every growable event.
    * ``skipped_fixed`` — the fixed-reference events discovery did not grow.
    * ``tickets`` — the decomposition tickets raised.
    * ``promotion_threshold`` — the N a candidate must reach to be promotable.
    """

    growable: dict[str, ConceptDiscovery]
    skipped_fixed: tuple[str, ...]
    tickets: tuple[DecompositionTicket, ...]
    promotion_threshold: int = PROMOTION_THRESHOLD

    def promotable(self, event: str) -> list[CandidateSeed]:
        """Candidates for *event* with ``>= promotion_threshold`` corroborating cards."""
        disc = self.growable.get(event)
        if disc is None:
            return []
        return [c for c in disc.candidates if c.corroboration_count >= self.promotion_threshold]


def run_concept_shadow(otag_evidence: Mapping[str, AbstractSet[str]]) -> ShadowProposalsReport:
    """Run concept-anchored producer discovery over every event, in SHADOW.

    Returns the proposals report; mutates nothing.  Fixed-reference events are
    recorded as skipped; growable events carry their candidate proposals,
    role-rejections, and any decomposition ticket.
    """
    growable: dict[str, ConceptDiscovery] = {}
    skipped: list[str] = []
    tickets: list[DecompositionTicket] = []

    for event_name in SEED_MAP:
        concept = producer_concept(event_name)
        disc = discover_for_concept(concept, SEED_MAP[event_name]['produced_by'].get('seeds', []), otag_evidence)
        if disc.skipped_fixed:
            skipped.append(event_name)
            continue
        growable[event_name] = disc
        if disc.decomposition_ticket is not None:
            tickets.append(disc.decomposition_ticket)

    return ShadowProposalsReport(
        growable=growable,
        skipped_fixed=tuple(skipped),
        tickets=tuple(tickets),
    )


def format_shadow_proposals_md(report: ShadowProposalsReport) -> str:
    """Render *report* as the owner-review proposals artifact (Markdown)."""
    lines: list[str] = []
    lines.append('# Concept-anchored discovery — SHADOW proposals (owner-review queue)')
    lines.append('')
    lines.append(f'**Generated:** {date.today().isoformat()} · **Mechanism:** R5c concept-anchored discovery')
    lines.append(f'**Promotion threshold:** N = {report.promotion_threshold} independent corroborating cards')
    lines.append('')
    lines.append(
        'This is SHADOW output. Nothing here is promoted to the live catalog '
        '(`SEED_MAP` / `event_catalog.json`); the owner ratifies promotions, the same gate the '
        'ratification walk-through used. Precision is necessary-but-not-sufficient — review each '
        'proposed producer for genuine producer-role fit before promoting.'
    )
    lines.append('')

    # Fixed-reference events (skipped by design).
    lines.append('## Fixed-reference events — NOT grown (authored query is authoritative)')
    lines.append('')
    for event in report.skipped_fixed:
        concept = producer_concept(event)
        lines.append(f'- `{event}` ← {concept.note}')
    lines.append('')

    # Decomposition tickets.
    lines.append('## Decomposition tickets — author a CardAttribute (do not promote the coarse otag)')
    lines.append('')
    if report.tickets:
        for t in report.tickets:
            lines.append(f'- **`{t.event}`** — coarse otag `{t.coarse_otag}`: {t.reason}')
            lines.append(f'  - role-impure anchors: {", ".join(f"`{x}`" for x in t.role_impure_examples)}')
    else:
        lines.append('_None on the current pool._')
    lines.append('')

    def _event_block(event: str) -> None:
        disc = report.growable[event]
        promotable = report.promotable(event)
        if not disc.candidates and not disc.role_rejected:
            return
        concept = producer_concept(event)
        lines.append(f'### `{event}`')
        lines.append(f'_concept: {concept.note}_')
        lines.append('')
        lines.append(
            f'- candidates: {len(disc.candidates)} '
            f'(promotable at N>={report.promotion_threshold}: {len(promotable)}) · '
            f'role-rejected (payoff-family vetoed): {len(disc.role_rejected)}'
        )
        if promotable:
            lines.append('- promotable producer proposals (otag . corroborating cards):')
            for c in sorted(promotable, key=lambda c: -c.corroboration_count):
                lines.append(f'  - `{c.otag}` . {c.corroboration_count}')
        if disc.role_rejected:
            lines.append(f'- vetoed payoff-family otags: {", ".join(f"`{x}`" for x in disc.role_rejected)}')
        lines.append('')

    # Growable proposals — the 5 ratified-concept (b) bootstrap-gap events FIRST
    # (discovery's designed self-healing surface), then the default (a) frontier
    # events (concept == current seeds; off-closure proposals are the
    # necessary-but-not-sufficient triage tail for owner curation).
    ratified = sorted(e for e in report.growable if e in _GROWABLE_B_CONCEPTS)
    default_frontier = sorted(e for e in report.growable if e not in _GROWABLE_B_CONCEPTS)

    lines.append('## Growable (b) events — ratified producer concept (the self-healing surface)')
    lines.append('')
    for event in ratified:
        _event_block(event)

    lines.append('## Default-frontier (a) events — concept == current seeds')
    lines.append('')
    lines.append(
        '_The next taxonomy climb is a refused umbrella; off-closure candidates here are the '
        'precision-necessary-but-not-sufficient tail (owner triage), not ratified broadenings._'
    )
    lines.append('')
    for event in default_frontier:
        _event_block(event)

    return '\n'.join(lines)


def write_shadow_proposals(
    path: Path,
    otag_evidence: Mapping[str, AbstractSet[str]] | None = None,
) -> ShadowProposalsReport:
    """Run the concept shadow and WRITE the owner-review artifact to *path*.

    Returns the report.  Writes only *path* (the proposals artifact); mutates no
    live catalog file.
    """
    if otag_evidence is None:
        otag_evidence = build_lake_evidence()
    report = run_concept_shadow(otag_evidence)
    path.write_text(format_shadow_proposals_md(report))
    return report
