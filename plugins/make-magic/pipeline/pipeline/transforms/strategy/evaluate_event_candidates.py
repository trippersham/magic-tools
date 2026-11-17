"""Event-candidate evaluation (seed discovery) loop (task 6.3).

The machinery here lets the concept-anchored adjacency EXTEND ITSELF from
evidence.  When an otag appears on corroborated cards for an event side but is
NOT yet in the side's seed closure, it becomes a **candidate seed** — provided
it passes the SAME :func:`~pipeline.transforms.strategy.derive_card_events.is_mechanic_exclusive`
gate as the seeds (so umbrellas like ``ramp`` can never re-enter).  A candidate
promotes to the seed set when ``>= N=3`` independent cards corroborate it.

The mechanism is SHADOW (the promotion is behind a flag, owner-gated) — the
authoritative derived adjacency (:func:`~pipeline.transforms.strategy.derive_card_events.derive_card_events`)
is unchanged by this code.  This module imports the seed-rule foundation from
:mod:`pipeline.transforms.strategy.derive_card_events` one-directionally: the
discovery layer depends on the derive core, never the reverse.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

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

    if otag_evidence is None:
        otag_evidence = _load_default_evidence()

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
    import json as _json

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
    import json as _json

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
