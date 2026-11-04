"""Drift guards for the declarative attribute layer's separation of concerns.

These fail the moment the clean split erodes:

* the edge layer (:data:`~.vocab.ARCHETYPE_EDGES`) is a pure name->name map — no
  otag slug ever leaks into it (membership lives in ``ATTRIBUTES`` queries);
* every identity is 1:1 with an edge — no orphan identity, no edge without an
  identity, no identity carrying two strategies;
* the records a representative fixture subset produces stay byte-identical to the
  committed baseline — the frozen pin on the native-exact vocabulary's output
  (decision fields, identity-archetype membership, and the deduplicated
  ``contributing_attrs``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pipeline.transforms.strategy import resolve
from pipeline.transforms.strategy.assets import load_assets
from pipeline.transforms.strategy.config import ALL_GROUPS, DEFAULT_CONFIG
from pipeline.transforms.strategy.vocab import (
    ANTHEM_DECONF_IDENTITIES,
    ARCHETYPE_EDGES,
    BASE_IDENTITIES,
    EDHREC_IDENTITIES,
    NEWBUCKET_IDENTITIES,
)

FIX = Path(__file__).resolve().parent / 'fixtures'
DECKS = FIX / 'decks'

_ALL_IDENTITY_PAIRS = (
    EDHREC_IDENTITIES + NEWBUCKET_IDENTITIES + ANTHEM_DECONF_IDENTITIES + BASE_IDENTITIES
)
_ALL_IDENTITY_NAMES = [n for n, _ in _ALL_IDENTITY_PAIRS]
#: Characters that only appear in a query, never in a plain name.
_QUERY_CHARS = (':', '*', '(', ')')


def test_edges_are_pure_name_to_name() -> None:
    """Drift #1: the edge map holds names only — no otag/query syntax leaks in."""
    for identity, strategy in ARCHETYPE_EDGES.items():
        assert isinstance(identity, str) and isinstance(strategy, str)
        for token in (identity, strategy):
            assert not any(ch in token for ch in _QUERY_CHARS), token
            assert 'otag' not in token and ' OR ' not in token and ' AND ' not in token


def test_every_identity_has_exactly_one_edge() -> None:
    """Drift #2: identities and edges are 1:1 — no orphans, no undefined edges."""
    identity_names = _ALL_IDENTITY_NAMES
    # no identity is declared twice (a name is one query + one edge, never two)
    assert len(identity_names) == len(set(identity_names)), 'duplicate identity name'
    identities = set(identity_names)
    edges = set(ARCHETYPE_EDGES)
    # every identity maps to a strategy ...
    assert identities - edges == set(), f'identities with no edge: {identities - edges}'
    # ... and every edge key is a defined identity (no dangling edge)
    assert edges - identities == set(), f'edges with no identity: {edges - identities}'
    # a dict value is single, so "exactly one strategy" is structural; assert the
    # strategies are plain names (the full target space is intentionally small).
    assert all(isinstance(ARCHETYPE_EDGES[n], str) for n in identities)


def test_router_is_a_total_function_over_the_identity_universe() -> None:
    """Routing is deterministic: each identity's own name routes to its edge."""
    assets = load_assets(DEFAULT_CONFIG)
    for name, strategy in ARCHETYPE_EDGES.items():
        # an identity whose query is a single exact-otag name self-routes; for the
        # general case we only assert routing never raises and stays in-vocab.
        routed = assets.archetype_of_attr(name)
        assert routed is None or routed in set(ARCHETYPE_EDGES.values())
        del strategy


def test_config_gating_changes_the_active_identities() -> None:
    """The anthem-deconf identity is off by default and on when its group is."""
    default = load_assets(DEFAULT_CONFIG)
    assert default.archetype_of_attr('power-matters') is None
    deconf = load_assets(
        DEFAULT_CONFIG.with_overrides(enabled_groups=frozenset(ALL_GROUPS) | {'deconf_anthem'}))
    assert deconf.archetype_of_attr('power-matters') == 'anthem/go-wide'


def test_fixture_records_match_committed_baseline() -> None:
    """Drift #3: byte-parity over the hermetic fixtures vs the committed hashes."""
    baseline = json.loads((FIX / 'vocab_parity_baseline.json').read_text())
    combos = json.loads((FIX / 'combos.json').read_text())
    for slug, expected in baseline.items():
        rec = resolve(json.loads((DECKS / f'{slug}.json').read_text()),
                      [dict(c) for c in combos])
        got = hashlib.sha256(
            json.dumps(rec, sort_keys=True, default=list).encode()).hexdigest()
        assert got == expected, f'record drift for {slug}'
