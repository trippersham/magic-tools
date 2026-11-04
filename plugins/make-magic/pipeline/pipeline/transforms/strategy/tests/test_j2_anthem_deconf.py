"""J2/B6 — the broad ``deconf_anthem`` anthem over-fire is OFF by default.

T2/B6 replaced the BROAD ``power-matters`` / ``toughness-matters`` → anthem
routing (the ``deconf_anthem`` group) with the NARROW ``power-matters-self``
payoff. These tests pin that the default engine:

* does NOT enable ``deconf_anthem`` (``arch_anthem_deconf is False``),
* still feeds the NARROW ``power-matters-self`` payoff into the anthem bucket
  (so a genuine go-tall anthem deck is unaffected), and
* keeps ``deconf_anthem`` available as a non-default knob (the capability is
  disabled, not deleted) — re-enabling it restores the broad routing.
"""

from __future__ import annotations

from pipeline.transforms.strategy import load_assets
from pipeline.transforms.strategy.config import DEFAULT_CONFIG

_BROAD = ('power-matters', 'toughness-matters')
_DECONF_ON = DEFAULT_CONFIG.with_overrides(
    enabled_groups=frozenset(DEFAULT_CONFIG.enabled_groups | {'deconf_anthem'})
)


def test_default_config_does_not_enable_deconf_anthem():
    assert 'deconf_anthem' not in DEFAULT_CONFIG.enabled_groups
    assert load_assets(DEFAULT_CONFIG).arch_anthem_deconf is False


def test_narrow_power_matters_self_payoff_is_present():
    # the T2-validated narrow payoff still feeds the anthem archetype
    assert DEFAULT_CONFIG.anthem_payoff == frozenset({'power-matters-self'})
    assert 'power-matters-self' in load_assets(DEFAULT_CONFIG).buckets['anthem']


def test_broad_power_matters_does_not_feed_anthem_by_default():
    assets = load_assets(DEFAULT_CONFIG)
    # broad slugs are no longer folded into the anthem bucket...
    for slug in _BROAD:
        assert slug not in assets.buckets['anthem']
    # ...and NEITHER routes to the anthem archetype (the J2/B6 anti-over-fire fix)
    for slug in _BROAD:
        assert assets.archetype_of_attr(slug) != 'anthem/go-wide'
    # power-matters stays unrouted by default; toughness-matters now has its OWN
    # dedicated archetype (the toughness-matters/defenders vocab add) — NOT anthem,
    # so the Doran/Felothar identity is captured without the broad anthem over-fire.
    assert assets.archetype_of_attr('power-matters') is None
    assert assets.archetype_of_attr('toughness-matters') == 'toughness-matters'


def test_narrow_anthem_node_still_routes():
    # the narrow path (power-matters-self -> 'anthem' bucket node -> archetype)
    # is intact: the 'anthem' node itself still names the anthem archetype
    assert load_assets(DEFAULT_CONFIG).archetype_of_attr('anthem') == 'anthem/go-wide'


def test_deconf_anthem_capability_preserved_as_nondefault_knob():
    # re-enabling the group restores BOTH the broad routing and the broad bucket
    # membership -> the capability was disabled, not deleted
    assets = load_assets(_DECONF_ON)
    assert assets.arch_anthem_deconf is True
    for slug in _BROAD:
        assert assets.archetype_of_attr(slug) == 'anthem/go-wide'
        assert slug in assets.buckets['anthem']
    # the narrow payoff is still present under the re-enabled config too
    assert 'power-matters-self' in assets.buckets['anthem']
