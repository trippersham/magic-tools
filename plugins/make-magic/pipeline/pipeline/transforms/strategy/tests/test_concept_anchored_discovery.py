"""R5c acceptance tests — concept-anchored, role-pure, shadow-only discovery.

These are the TDD gate for the R5c rewire of ``discover_candidates`` /
``discover_for_concept`` (``evaluate_event_candidates.py``): discovery scores
candidates against each event's RATIFIED PRODUCER CONCEPT (owner-ratified DATA,
``PRODUCER_CONCEPTS``), not the circular current-closure, gated by

  * **role-purity** — a payoff-family (``synergy-*`` / ``*-matters`` /
    ``*-fuel-*`` / ``*-cares``) otag is never admitted as a producer, AND
  * **decomposition-escalation** — a concept whose only otag anchor is a coarse
    role-impure tag emits an "author a CardAttribute" owner-queue ticket instead
    of promoting the coarse otag.

Five acceptance tests (the prompt's "done" definition):

1. **Ground-truth re-derivation (WS2):** given the creature-enters producer
   CONCEPT, discovery re-derives the known impulse-seed family
   (``sneak-from-library`` / ``impulse-onto-battlefield`` /
   ``consult-onto-battlefield``) from a producer-seed set that does NOT contain
   it — the headline proof that concept-anchoring self-heals (and the extreme:
   an EMPTY producer set recovers from the concept).
2. **Payoff-veto:** no payoff-family otag is ever proposed as a producer.
3. **Fixed-reference skip:** monarch / domain / permanent-dies / poisoned / rad /
   experience propose 0 producers (their authored query is authoritative).
4. **Decomposition-escalation:** a coarse-otag concept (the monarch
   ``monarch-matters`` pattern) yields a decomposition TICKET, not an otag.
5. **Shadow-only:** running discovery mutates NO live catalog (SEED_MAP + the raw
   ``event_catalog.json`` byte-identical after a run).
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from pipeline.transforms.strategy.tests.conftest import LAKE_AVAILABLE

_skip_no_lake = pytest.mark.skipif(not LAKE_AVAILABLE, reason='lake parquets not available')

_CATALOG_PATH = Path(__file__).resolve().parent.parent / 'data' / 'event_catalog.json'

#: The six fixed-reference (synth-attr / curated-union producer) events — their
#: coverage IS the authored query, so discovery must not grow them.
FIXED_EVENTS = ('monarch', 'domain', 'permanent-dies', 'poisoned', 'rad', 'experience')

#: The five bootstrap-gap (b) growable events — discovery's self-healing value.
GROWABLE_B_EVENTS = (
    'creature-enters-battlefield',
    'artifact-enters',
    'land-enters-battlefield',
    'token-created',
    'creature-copied',
)


# ---------------------------------------------------------------------------
# 1. Ground-truth re-derivation (WS2) — the headline self-heal proof
# ---------------------------------------------------------------------------


@_skip_no_lake
class TestWS2GroundTruthRederivation:
    """Concept-anchoring re-derives the WS2 impulse-seed family from a producer
    set that no longer (or never) contains it."""

    def test_rederives_impulse_family_with_sneak_seed_removed(self) -> None:
        """Remove ``sneak-from-library`` from the SEEDS but keep it in the frozen
        CONCEPT → discovery recovers the exact WS2 family."""
        from pipeline.transforms.strategy.derive_card_events import SEED_MAP
        from pipeline.transforms.strategy.evaluate_event_candidates import (
            build_lake_evidence,
            discover_for_concept,
            producer_concept,
        )

        concept = producer_concept('creature-enters-battlefield')
        all_seeds = SEED_MAP['creature-enters-battlefield']['produced_by']['seeds']
        seeds = [s for s in all_seeds if s != 'sneak-from-library']
        assert 'sneak-from-library' not in seeds, 'guard: the WS2 seed must be absent from the reduced set'

        result = discover_for_concept(concept, seeds, build_lake_evidence())
        slugs = {c.otag for c in result.candidates}

        assert {'sneak-from-library', 'impulse-onto-battlefield', 'consult-onto-battlefield'} <= slugs, (
            f'concept-anchoring failed to re-derive the WS2 impulse family; got {sorted(slugs)[:20]}'
        )

    def test_empty_producer_set_recovers_from_concept(self) -> None:
        """The extreme: a WIPED (empty) producer seed set still recovers the
        impulse family from the concept anchor."""
        from pipeline.transforms.strategy.evaluate_event_candidates import (
            build_lake_evidence,
            discover_for_concept,
            producer_concept,
        )

        concept = producer_concept('creature-enters-battlefield')
        result = discover_for_concept(concept, [], build_lake_evidence())
        slugs = {c.otag for c in result.candidates}

        assert {'sneak-from-library', 'impulse-onto-battlefield', 'consult-onto-battlefield'} <= slugs, (
            'an empty producer set did not self-heal from the concept anchor'
        )

    def test_old_closure_anchoring_cannot_self_heal_from_empty(self) -> None:
        """Contrast: the OLD closure-anchored ``discover_candidates`` returns
        nothing from an empty seed set (the circular-reference bug WS3 fixes)."""
        # With an empty concept too (closure == seeds == empty) there is nothing
        # to anchor on — proving the reference, not the gate, was the blocker.
        from pipeline.transforms.strategy.evaluate_event_candidates import (
            ConceptKind,
            ProducerConcept,
            build_lake_evidence,
            discover_for_concept,
            producer_concept,
        )

        empty_concept = ProducerConcept('creature-enters-battlefield', (), ConceptKind.GROWABLE, 'empty')
        result = discover_for_concept(empty_concept, [], build_lake_evidence())
        assert result.candidates == (), 'an empty CONCEPT yields nothing (the reference recovers, not the gate)'

        # sanity: the real concept DOES recover (already covered above)
        real = discover_for_concept(producer_concept('creature-enters-battlefield'), [], build_lake_evidence())
        assert {c.otag for c in real.candidates}


# ---------------------------------------------------------------------------
# 2. Payoff-veto — role-purity
# ---------------------------------------------------------------------------


@_skip_no_lake
class TestPayoffVeto:
    """A payoff-family otag is NEVER admitted as a producer (archetype layer,
    ADR 0012/0025); precision alone is necessary-but-not-sufficient."""

    def test_no_candidate_is_payoff_family(self) -> None:
        from pipeline.transforms.strategy.catalog_integrity import is_payoff_family
        from pipeline.transforms.strategy.evaluate_event_candidates import (
            build_lake_evidence,
            discover_for_concept,
            producer_concept,
        )

        ev = build_lake_evidence()
        for event in GROWABLE_B_EVENTS:
            result = discover_for_concept(producer_concept(event), [], ev)
            offenders = [c.otag for c in result.candidates if is_payoff_family(c.otag)]
            assert not offenders, f'{event}: payoff-family otag(s) admitted as producers: {offenders}'

    def test_payoff_family_otags_are_vetoed_into_role_rejected(self) -> None:
        """The veto is observable: at least one payoff-family otag that reaches
        the gate is recorded as role-rejected (not silently absent)."""
        from pipeline.transforms.strategy.catalog_integrity import is_payoff_family
        from pipeline.transforms.strategy.evaluate_event_candidates import (
            build_lake_evidence,
            discover_for_concept,
            producer_concept,
        )

        result = discover_for_concept(producer_concept('creature-enters-battlefield'), [], build_lake_evidence())
        assert result.role_rejected, 'expected at least one payoff-family veto on the wiped creature-enters run'
        assert all(is_payoff_family(o) for o in result.role_rejected)


class TestBookkeepingVeto:
    """Scryfall bookkeeping families (cycle-/typal-/type-/unique-) catalogue a
    card's provenance or identity, not what it does — never an event producer
    (owner-ratified 2026-11-18).  ``typal-*`` stays authoritative for tribal
    detection in distill; this veto is the PRODUCER side only."""

    def test_no_candidate_is_bookkeeping_family(self) -> None:
        from pipeline.transforms.strategy.catalog_integrity import is_bookkeeping_family
        from pipeline.transforms.strategy.evaluate_event_candidates import (
            build_lake_evidence,
            discover_for_concept,
            producer_concept,
        )

        ev = build_lake_evidence()
        for event in GROWABLE_B_EVENTS:
            result = discover_for_concept(producer_concept(event), [], ev)
            offenders = [c.otag for c in result.candidates if is_bookkeeping_family(c.otag)]
            assert not offenders, f'{event}: bookkeeping otag(s) admitted as producers: {offenders}'

    def test_bookkeeping_otags_are_recorded_as_skipped(self) -> None:
        """The skip is observable: the inflated creature-enters run records
        cycle-/typal- bookkeeping otags as bookkeeping_rejected, not as
        promotable candidates."""
        from pipeline.transforms.strategy.catalog_integrity import is_bookkeeping_family
        from pipeline.transforms.strategy.evaluate_event_candidates import (
            build_lake_evidence,
            discover_for_concept,
            producer_concept,
        )

        result = discover_for_concept(producer_concept('creature-enters-battlefield'), [], build_lake_evidence())
        assert result.bookkeeping_rejected, 'expected cycle-/typal- bookkeeping skips on the wiped creature-enters run'
        assert all(is_bookkeeping_family(o) for o in result.bookkeeping_rejected)

    def test_bookkeeping_family_matcher(self) -> None:
        from pipeline.transforms.strategy.catalog_integrity import is_bookkeeping_family

        assert is_bookkeeping_family('cycle-c18-planeswalker')
        assert is_bookkeeping_family('typal-fish')
        assert is_bookkeeping_family('type-errata-egg')
        assert is_bookkeeping_family('unique-mana-symbol')
        # Real producer mechanics are NOT bookkeeping.
        assert not is_bookkeeping_family('impulse-to-zone')
        assert not is_bookkeeping_family('reanimate-from-opponent')
        assert not is_bookkeeping_family('pseudo-proliferate')  # pseudo-* = real "acts like X"


# ---------------------------------------------------------------------------
# 3. Fixed-reference skip (hermetic — the FIXED branch returns before the lake)
# ---------------------------------------------------------------------------


class TestFixedReferenceSkip:
    """The synth-attr / curated-union producer events are FIXED — discovery
    proposes 0 (their authored query auto-evolves with the pool)."""

    def test_fixed_events_classified_fixed(self) -> None:
        from pipeline.transforms.strategy.evaluate_event_candidates import (
            ConceptKind,
            producer_concept,
        )

        for event in FIXED_EVENTS:
            assert producer_concept(event).kind is ConceptKind.FIXED_SYNTH, f'{event} must be FIXED_SYNTH'

    def test_fixed_events_propose_zero(self) -> None:
        from pipeline.transforms.strategy.evaluate_event_candidates import (
            discover_for_concept,
            producer_concept,
        )

        # Non-empty evidence that WOULD corroborate if the event were growable.
        evidence = {f'card-{i}': {'dies-removal', 'sacrifice-outlet', 'synergy-sacrifice'} for i in range(10)}
        for event in FIXED_EVENTS:
            result = discover_for_concept(producer_concept(event), [], evidence)
            assert result.skipped_fixed is True, f'{event} must be skipped as fixed'
            assert result.candidates == (), f'{event} proposed producers despite being fixed'
            assert result.decomposition_ticket is None

    def test_discover_candidates_returns_empty_for_fixed_produced_by(self) -> None:
        """The rewired backward-compat wrapper also returns [] for fixed events."""
        from pipeline.transforms.strategy.evaluate_event_candidates import discover_candidates

        evidence = {f'card-{i}': {'dies-removal'} for i in range(10)}
        assert discover_candidates({}, 'permanent-dies', 'produced_by', evidence) == []


# ---------------------------------------------------------------------------
# 4. Decomposition-escalation (hermetic — detected before the lake)
# ---------------------------------------------------------------------------


class TestDecompositionEscalation:
    """A concept whose only otag anchor is a coarse role-impure (payoff-family)
    tag escalates to a CardAttribute-authoring ticket — the monarch pattern."""

    def test_coarse_payoff_family_anchor_yields_ticket(self) -> None:
        from pipeline.transforms.strategy.evaluate_event_candidates import (
            ConceptKind,
            ProducerConcept,
            discover_for_concept,
        )

        # The monarch pattern: Scryfall carries ONE `monarch-matters` tag on BOTH
        # makers (producers) and payoffs — a coarse conflated otag. The concept
        # cannot be served by a clean producer otag → escalate.
        concept = ProducerConcept('monarch-synthetic', ('monarch-matters',), ConceptKind.GROWABLE, 'coarse')
        result = discover_for_concept(concept, [], {'c1': {'monarch-matters'}})

        assert result.decomposition_ticket is not None, 'coarse payoff-family anchor must escalate'
        assert result.decomposition_ticket.coarse_otag == 'monarch-matters'
        assert result.candidates == (), 'the coarse otag must NOT be promoted as a producer'

    def test_clean_concept_yields_no_ticket(self) -> None:
        """A clean producer concept (no coarse anchor) emits no decomposition
        ticket — escalation is for the role-impure case only."""
        from pipeline.transforms.strategy.evaluate_event_candidates import (
            ConceptKind,
            ProducerConcept,
            discover_for_concept,
        )

        concept = ProducerConcept('clean-synthetic', ('clone', 'copy-creature'), ConceptKind.GROWABLE, 'clean')
        # empty evidence → no candidates, but also no ticket (clean anchors)
        result = discover_for_concept(concept, ['clone', 'copy-creature'], {})
        assert result.decomposition_ticket is None


# ---------------------------------------------------------------------------
# 5. Shadow-only — no live catalog mutation
# ---------------------------------------------------------------------------


@_skip_no_lake
class TestShadowOnly:
    """Discovery is SHADOW: it proposes to an owner-review artifact and mutates
    no live catalog file."""

    def test_running_shadow_does_not_mutate_seed_map_or_catalog(self) -> None:
        from pipeline.transforms.strategy.derive_card_events import SEED_MAP
        from pipeline.transforms.strategy.evaluate_event_candidates import (
            build_lake_evidence,
            run_concept_shadow,
        )

        seed_map_before = copy.deepcopy(SEED_MAP)
        catalog_before = hashlib.sha256(_CATALOG_PATH.read_bytes()).hexdigest()

        report = run_concept_shadow(build_lake_evidence())
        assert report.growable  # sanity: the shadow actually ran

        assert seed_map_before == SEED_MAP, 'discovery mutated the live SEED_MAP'
        assert hashlib.sha256(_CATALOG_PATH.read_bytes()).hexdigest() == catalog_before, (
            'discovery mutated the raw event_catalog.json'
        )

    def test_shadow_report_skips_fixed_and_grows_b_events(self) -> None:
        from pipeline.transforms.strategy.evaluate_event_candidates import (
            build_lake_evidence,
            run_concept_shadow,
        )

        report = run_concept_shadow(build_lake_evidence())
        for event in FIXED_EVENTS:
            assert event in report.skipped_fixed, f'{event} should be reported skipped'
        # the growable (b) events are present in the growable report
        for event in GROWABLE_B_EVENTS:
            assert event in report.growable, f'{event} should be a growable report row'
