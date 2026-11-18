"""Tests for the WS3 layer-1 catalog-integrity guards (:mod:`.catalog_integrity`).

Two guards, both validation-only (byte-parity — they READ the catalog, they do
not change engine output):

* **Guard 1** — the dead-ref validator extended to the distill query layer
  (``validate_distill_atoms`` / ``validate_membership_atoms``), made a BLOCKING
  gate, with a non-tautological "a constructed dead ref IS caught" regression.
* **Guard 2** — the role-consistency linter (``validate_role_consistency``):
  payoff-family otag used as a producer seed is BLOCKING; producer/trigger role
  ambiguity is advisory.

The hermetic tests inject their own resolve set / seed map (no lake); the
lake-backed gate tests gate on :data:`LAKE_AVAILABLE`.
"""

from __future__ import annotations

import pytest

from pipeline.transforms.strategy import catalog_integrity as ci
from pipeline.transforms.strategy.tests.conftest import LAKE_AVAILABLE

_skip_no_lake = pytest.mark.skipif(not LAKE_AVAILABLE, reason='lake parquets not available')


# ---------------------------------------------------------------------------
# Atom extraction (shared machinery)
# ---------------------------------------------------------------------------


class TestAtomExtraction:
    def test_otag_atoms_in_order(self) -> None:
        assert ci.otag_atoms('otag:a OR otag:b AND NOT otag:c') == ['a', 'b', 'c']

    def test_query_atoms_covers_all_fields(self) -> None:
        atoms = ci.query_atoms("otag:x OR oracle:'y z' OR type:Vehicle OR keyword:morph OR devotion")
        assert ('otag', 'x') in atoms
        assert ('oracle', 'y z') in atoms
        assert ('type', 'Vehicle') in atoms
        assert ('keyword', 'morph') in atoms
        assert ('devotion', '') in atoms


# ---------------------------------------------------------------------------
# Guard 1 — dead-ref validator (hermetic, injected resolve set)
# ---------------------------------------------------------------------------


class TestDeadRefValidatorRegression:
    """Non-tautological: the validator flags a CONSTRUCTED dead ref and leaves a
    good atom alone — proven against an injected resolve set (no lake)."""

    def test_constructed_dead_ref_is_caught(self) -> None:
        viol = ci.validate_distill_atoms(
            identities=[('fake', 'otag:real-name OR otag:__not_a_real_slug__')],
            edges={'fake': 'fake'},
            resolve=frozenset({'real-name'}),
        )
        assert len(viol) == 1
        assert '__not_a_real_slug__' in viol[0]
        assert 'real-name' not in viol[0]

    def test_all_good_atoms_pass(self) -> None:
        viol = ci.validate_distill_atoms(
            identities=[('fake', 'otag:a OR otag:b AND NOT otag:c')],
            edges={'fake': 'fake'},
            resolve=frozenset({'a', 'b', 'c'}),
        )
        assert viol == []

    def test_non_otag_distill_atom_is_a_dead_atom(self) -> None:
        # A distill pseudo-card carries only an otag, so a field atom never matches.
        viol = ci.validate_distill_atoms(
            identities=[('fake', "oracle:'anything'")],
            edges={'fake': 'fake'},
            resolve=frozenset({'fake'}),
        )
        assert len(viol) == 1
        assert 'non-otag' in viol[0]

    def test_archetype_edge_without_identity_is_dead(self) -> None:
        viol = ci.validate_distill_atoms(
            identities=[('known', 'otag:a')],
            edges={'known': 's', 'orphan-edge': 's'},
            resolve=frozenset({'a'}),
        )
        assert any('orphan-edge' in v and 'dead edge' in v for v in viol)

    def test_membership_atom_dead_ref_is_caught(self) -> None:
        viol = ci.validate_membership_atoms(
            pairs=[('synth-x', "otag:live-slug AND oracle:'text'"), ('synth-y', 'otag:__dead__')],
            resolve=frozenset({'live-slug'}),
        )
        assert len(viol) == 1
        assert '__dead__' in viol[0]


# ---------------------------------------------------------------------------
# Guard 2 — role-consistency linter (hermetic — SEED_MAP + regex only)
# ---------------------------------------------------------------------------


class TestRoleConsistency:
    def test_payoff_family_patterns(self) -> None:
        assert ci.is_payoff_family('synergy-proliferate')
        assert ci.is_payoff_family('monarch-matters')
        assert ci.is_payoff_family('counter-fuel-energy')
        assert not ci.is_payoff_family('land-ramp')
        assert not ci.is_payoff_family('flicker')
        assert not ci.is_payoff_family('mana-producer')

    def test_payoff_as_producer_is_caught_regression(self) -> None:
        """Non-tautological: a constructed payoff-family PRODUCER seed is flagged;
        a legitimate (non-payoff) producer seed in the same event is not."""
        fake = {
            'fake-event': {
                'produced_by': {'seeds': ['synergy-proliferate', 'land-ramp']},
                'triggers': {'seeds': []},
            }
        }
        report = ci.validate_role_consistency(fake)
        assert any('synergy-proliferate' in v for v in report.payoff_producers)
        assert not any('land-ramp' in v for v in report.payoff_producers)
        assert not report.ok  # the inversion blocks

    def test_current_seed_map_has_no_payoff_producer_BLOCKING(self) -> None:
        """BLOCKING: the frozen SEED_MAP seeds no payoff-family otag as a producer
        (the synergy-proliferate inversion class is clean, Tier-A fixed it)."""
        report = ci.validate_role_consistency()
        assert report.payoff_producers == (), (
            'payoff-family otag(s) seeded as producer:\n  ' + '\n  '.join(report.payoff_producers)
        )

    def test_role_ambiguity_is_advisory_only(self) -> None:
        """Advisory: role ambiguity is reported but never blocks (legitimate
        producer/consumer duals like ``flicker`` use both sides)."""
        report = ci.validate_role_consistency()
        assert isinstance(report.role_ambiguous, tuple)
        assert report.ok  # BLOCKING is clean regardless of advisory entries


# ---------------------------------------------------------------------------
# The BLOCKING gate over the REAL frozen catalog + live lake
# ---------------------------------------------------------------------------


@_skip_no_lake
class TestCatalogBlockingGate:
    def test_seed_map_resolves_clean(self) -> None:
        from pipeline.transforms.strategy.derive_card_events import validate_seed_map

        assert validate_seed_map() == []

    def test_membership_query_atoms_resolve_clean(self) -> None:
        assert ci.validate_membership_atoms() == []

    def test_archetype_edge_keys_are_all_identities(self) -> None:
        assert [v for v in ci.validate_distill_atoms() if 'dead edge' in v] == []

    def test_clean_blocking_checks(self) -> None:
        """SEED_MAP + membership + payoff-as-producer are all clean (blocking)."""
        report = ci.validate_catalog()
        assert report.seed_map == ()
        assert report.membership_atoms == ()
        assert report.payoff_producers == ()

    def test_distill_atoms_resolve_clean(self) -> None:
        # R5a (2026-11-17): the 3 long-standing distill dead refs were corrected in a measured
        # re-baseline (counters:otag:proliferate -> live proliferate family; voltron/equipment:
        # otag:auras dropped as redundant with synergy-aura; deathtouch:otag:deathtouch-combat ->
        # otag:gains-deathtouch OR otag:gives-deathtouch). The xfail marker is removed and this
        # guard is now FULLY BLOCKING. See subagents/r5a-latent-bug-hunt/outbox.md.
        assert ci.validate_distill_atoms() == []
