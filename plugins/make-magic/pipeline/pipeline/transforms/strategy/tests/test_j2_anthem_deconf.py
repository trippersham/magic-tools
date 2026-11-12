"""J2/B6 — anthem is NOT fed by the broad power-matters/toughness-matters routing.

The over-fire fix (T2/B6) is now PERMANENT (the ``deconf_anthem`` group that
could re-enable the broad routing was deleted as frozen configuration — ADR
0006). Only the NARROW ``power-matters-self`` payoff feeds the anthem archetype.
These tests pin the (now sole) engine:

* feeds the NARROW ``power-matters-self`` payoff into the anthem bucket
  (a genuine go-tall anthem deck is unaffected),
* does NOT route the broad power-matters/toughness-matters into anthem, and
* the ``anthem`` node itself still names the anthem archetype.
"""

from __future__ import annotations

from pipeline.transforms.strategy import load_assets
from pipeline.transforms.strategy.config import DEFAULT_CONFIG

_BROAD = ('power-matters', 'toughness-matters')


def test_narrow_power_matters_self_payoff_is_present():
    # the T2-validated narrow payoff still feeds the anthem archetype (now via the
    # vocab data fold, not a config knob)
    assert 'power-matters-self' in load_assets(DEFAULT_CONFIG).buckets['anthem']


def test_broad_power_matters_does_not_feed_anthem():
    assets = load_assets(DEFAULT_CONFIG)
    # broad slugs are not folded into the anthem bucket...
    for slug in _BROAD:
        assert slug not in assets.buckets['anthem']
    # ...and NEITHER routes to the anthem archetype (the J2/B6 anti-over-fire fix)
    for slug in _BROAD:
        assert assets.archetype_of_attr(slug) != 'anthem/go-wide'
    # power-matters stays unrouted; toughness-matters has its OWN dedicated archetype
    # (the toughness-matters/defenders vocab add) — NOT anthem, so Doran/Felothar is
    # captured without the broad anthem over-fire.
    assert assets.archetype_of_attr('power-matters') is None
    assert assets.archetype_of_attr('toughness-matters') == 'toughness-matters'


def test_narrow_anthem_node_still_routes():
    # the narrow path (power-matters-self -> 'anthem' bucket node -> archetype) is intact
    assert load_assets(DEFAULT_CONFIG).archetype_of_attr('anthem') == 'anthem/go-wide'
