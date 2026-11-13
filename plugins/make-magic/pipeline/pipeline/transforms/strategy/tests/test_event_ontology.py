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
                f'event {name!r} carries a "role" field — '
                f'reachability must be pure topology (ADR 0012)'
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
                f'event {name!r} carries "polarity" — '
                f'only game-end may have polarity (ADR 0012)'
            )

    def test_no_non_game_end_event_has_scope(self, events: dict[str, dict]) -> None:
        """Only ``game-end`` may carry a ``scope`` field."""
        for name, ev in events.items():
            if name == 'game-end':
                continue  # game-end is allowed scope
            assert 'scope' not in ev, (
                f'event {name!r} carries "scope" — '
                f'only game-end may have scope (ADR 0012)'
            )


# ────────────────────────────────────────────────────────────────────────────
# 3. Allowed event-node fields (structural shape)
# ────────────────────────────────────────────────────────────────────────────

# The canonical field set for a non-terminal event node.
_ALLOWED_FIELDS = frozenset({'rule', 'produced_by', 'triggered_by', 'abbrev'})
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
                f'event {name!r} carries unexpected field(s) {extra} — '
                f'allowed: {sorted(allowed)} (ADR 0012)'
            )

    def test_every_event_has_required_fields(self, events: dict[str, dict]) -> None:
        """Every event carries rule + produced_by + triggered_by."""
        required = {'rule', 'produced_by', 'triggered_by'}
        for name, ev in events.items():
            missing = required - set(ev.keys())
            assert not missing, (
                f'event {name!r} is missing required field(s) {missing}'
            )


# ────────────────────────────────────────────────────────────────────────────
# 4. Catalog is non-empty and game-end is not yet present (conditional guard)
# ────────────────────────────────────────────────────────────────────────────

class TestCatalogBaseline:
    """Baseline shape checks — fail loudly if the catalog drifts unexpectedly."""

    def test_catalog_is_nonempty(self, events: dict[str, dict]) -> None:
        assert len(events) > 0, 'event catalog is empty'

    def test_game_end_not_yet_present(self, events: dict[str, dict]) -> None:
        """game-end is wired in Phase 2B with the fid adapter; it should not appear yet.

        When game-end is added, this test should be replaced with a positive
        assertion that game-end carries polarity and scope.
        """
        if 'game-end' in events:
            # If game-end lands, enforce that it carries polarity + scope
            assert 'polarity' in events['game-end'], (
                'game-end is present but missing polarity'
            )
            assert 'scope' in events['game-end'], (
                'game-end is present but missing scope'
            )
        # Otherwise: game-end is not yet in the catalog — expected for Phase 2A.
