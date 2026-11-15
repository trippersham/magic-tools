"""ADR 0012 invariant: Event-ontology catalog shape.

An Event is a node from the game's BOUNDED TRIGGER TAXONOMY.
Reachability is PURE TOPOLOGY (derived from edges, never an authored ``role``).
``polarity``/``scope`` are reserved for ``game-end`` only.

These tests assert the settled shape so a future regression is caught.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_CATALOG_PATH = Path(__file__).resolve().parent.parent / 'data' / 'event_catalog_v4.json'


@pytest.fixture()
def event_catalog() -> dict:
    with open(_CATALOG_PATH) as f:
        return json.load(f)


@pytest.fixture()
def events(event_catalog: dict) -> dict[str, dict]:
    return event_catalog['events']


# ────────────────────────────────────────────────────────────────────────────
# 1. No ``role`` or ``terminal`` field on any event
# ────────────────────────────────────────────────────────────────────────────


class TestNoAuthoredRole:
    """Sink-ness is pure topology — no event carries an authored ``role``."""

    def test_no_event_has_role_field(self, events: dict[str, dict]) -> None:
        """No event node carries a ``role`` field; reachability is derived from edges."""
        for name, ev in events.items():
            assert 'role' not in ev, (
                f'event {name!r} carries a "role" field — reachability must be pure topology (ADR 0012)'
            )

    def test_no_event_has_terminal_field(self, events: dict[str, dict]) -> None:
        """No event carries a ``terminal`` field; sink-ness is a graph property."""
        for name, ev in events.items():
            assert 'terminal' not in ev, (
                f'event {name!r} carries a "terminal" field — '
                f'terminal status is a graph property, not an annotation (ADR 0012)'
            )


# ────────────────────────────────────────────────────────────────────────────
# 2. ``polarity`` / ``scope`` only on ``game-end`` (conditional)
# ────────────────────────────────────────────────────────────────────────────


class TestPolarityScopeRestriction:
    """polarity{win|draw|loss} and scope{table|target|self} are reserved for game-end."""

    def test_no_non_game_end_event_has_polarity(self, events: dict[str, dict]) -> None:
        """Only ``game-end`` may carry a ``polarity`` field."""
        for name, ev in events.items():
            if name == 'game-end':
                continue  # game-end is allowed polarity
            assert 'polarity' not in ev, (
                f'event {name!r} carries "polarity" — only game-end may have polarity (ADR 0012)'
            )

    def test_no_non_game_end_event_has_scope(self, events: dict[str, dict]) -> None:
        """Only ``game-end`` may carry a ``scope`` field."""
        for name, ev in events.items():
            if name == 'game-end':
                continue  # game-end is allowed scope
            assert 'scope' not in ev, f'event {name!r} carries "scope" — only game-end may have scope (ADR 0012)'


# ────────────────────────────────────────────────────────────────────────────
# 3. Allowed event-node fields (structural shape)
# ────────────────────────────────────────────────────────────────────────────

# The canonical field set for a non-terminal event node.
_ALLOWED_FIELDS = frozenset({'rule', 'produced_by', 'triggers', 'abbrev'})
# game-end extends the allowed set with polarity/scope.
_GAME_END_EXTRA = frozenset({'polarity', 'scope'})


class TestEventNodeFieldSet:
    """Every event node uses only the allowed field vocabulary."""

    def test_every_event_uses_only_allowed_fields(self, events: dict[str, dict]) -> None:
        """No event carries a field outside the allowed vocabulary."""
        for name, ev in events.items():
            allowed = _ALLOWED_FIELDS | (_GAME_END_EXTRA if name == 'game-end' else frozenset())
            extra = set(ev.keys()) - allowed
            assert not extra, (
                f'event {name!r} carries unexpected field(s) {extra} — allowed: {sorted(allowed)} (ADR 0012)'
            )

    def test_every_event_has_required_fields(self, events: dict[str, dict]) -> None:
        """Every event carries rule + produced_by + triggers."""
        required = {'rule', 'produced_by', 'triggers'}
        for name, ev in events.items():
            missing = required - set(ev.keys())
            assert not missing, f'event {name!r} is missing required field(s) {missing}'


# ────────────────────────────────────────────────────────────────────────────
# 4. Catalog is non-empty and game-end is not yet present (conditional guard)
# ────────────────────────────────────────────────────────────────────────────


class TestCatalogBaseline:
    """Baseline shape checks — fail loudly if the catalog drifts unexpectedly."""

    def test_catalog_is_nonempty(self, events: dict[str, dict]) -> None:
        assert len(events) > 0, 'event catalog is empty'

    def test_game_end_present_with_polarity_and_scope(self, events: dict[str, dict]) -> None:
        """game-end is wired in Phase 2B step 5a; it carries polarity and scope (ADR 0012)."""
        assert 'game-end' in events, 'game-end must be present in the event catalog'
        assert 'polarity' in events['game-end'], 'game-end is present but missing polarity'
        assert 'scope' in events['game-end'], 'game-end is present but missing scope'


# ────────────────────────────────────────────────────────────────────────────
# 5. Phase 2B step 5a events: mana-produced (now LIVE), game-end + library-depleted (INERT)
# ────────────────────────────────────────────────────────────────────────────


class TestPhase2BInertEvents:
    """game-end and library-depleted remain INERT (empty edges).

    mana-produced was promoted to LIVE wiring (broad mana sources/sinks) and is
    tested separately below.
    """

    _STILL_INERT = ('game-end', 'library-depleted')
    _ALL_PHASE2B = ('mana-produced', 'game-end', 'library-depleted')

    def test_new_events_present(self, events: dict[str, dict]) -> None:
        for slug in self._ALL_PHASE2B:
            assert slug in events, f'{slug!r} must be in the event catalog'

    def test_new_events_have_required_fields(self, events: dict[str, dict]) -> None:
        for slug in self._ALL_PHASE2B:
            ev = events[slug]
            assert 'rule' in ev, f'{slug}: missing rule'
            assert 'produced_by' in ev, f'{slug}: missing produced_by'
            assert 'triggers' in ev, f'{slug}: missing triggers'

    def test_inert_events_are_inert(self, events: dict[str, dict]) -> None:
        """game-end and library-depleted remain inert (empty edges)."""
        for slug in self._STILL_INERT:
            ev = events[slug]
            assert ev['produced_by'] == [], f'{slug}: produced_by should be empty (inert)'
            assert ev['triggers'] == [], f'{slug}: triggers should be empty (inert)'

    def test_mana_produced_is_live(self, events: dict[str, dict]) -> None:
        """mana-produced is LIVE with the corrected, taxonomy-derived wiring.

        ``produced_by`` is the closure-deduplicated producer set (ADR 0020):
        the umbrella ``ramp`` and the ``mana-producer``-redundant ``mana-dork`` /
        ``mana-rock`` children are OUT; ``ramp`` is carried only anchor-side in the
        ``producer_anchor`` gate, never in the mass-producing ``produced_by``.
        """
        ev = events['mana-produced']
        assert len(ev['produced_by']) > 0, 'mana-produced: produced_by should be non-empty (live)'
        assert len(ev['triggers']) > 0, 'mana-produced: triggers should be non-empty (live)'
        # The corrected produced_by set (exact): closure-deduplicated producers.
        assert set(ev['produced_by']) == {'adds-multiple-mana', 'mana-producer', 'repeatable-treasures', 'ritual'}
        # The umbrella + closure-redundant children are OUT of produced_by.
        assert 'ramp' not in ev['produced_by']
        assert 'mana-dork' not in ev['produced_by']
        assert 'mana-rock' not in ev['produced_by']
        # Consumer side unchanged.
        assert 'bottomless-mana-sink' in ev['triggers']

    def test_game_end_has_polarity_and_scope(self, events: dict[str, dict]) -> None:
        """game-end is the ONLY event with polarity + scope fields."""
        ge = events['game-end']
        assert ge['polarity'] == ['win', 'draw', 'loss']
        assert ge['scope'] == ['table', 'target', 'self']

    def test_mana_produced_and_library_depleted_no_polarity_scope(self, events: dict[str, dict]) -> None:
        for slug in ('mana-produced', 'library-depleted'):
            ev = events[slug]
            assert 'polarity' not in ev, f'{slug} must NOT have polarity'
            assert 'scope' not in ev, f'{slug} must NOT have scope'
