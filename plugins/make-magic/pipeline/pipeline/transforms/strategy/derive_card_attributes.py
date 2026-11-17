# Phase-A card-intrinsic attribute derivation
# ---------------------------------------------------------------------------
# The otag->CardAttribute derivation shared by the FACTS layer
# (:func:`.strategy_graph.compute_facts`, per deck) and the Phase-A combo-edge
# authoring (:mod:`.combo_edges`, ADR 0021 B1).  Extracted to its own module so
# the two reads of a card's attributes resolve through one named rule and never
# drift (ADR 0001 one-named-rule).
from __future__ import annotations

from pipeline.transforms.strategy.assets import (
    EDHREC_ATTR_SLUGS,
    ENCH_ATTR_SLUGS,
    StrategyAssets,
)
from pipeline.transforms.strategy.query import CardView


def derive_card_attributes(closure: set[str], assets: StrategyAssets) -> set[str]:
    """Union the otag-derived CardAttributes for one card's otag closure.

    This is the otag->attribute derivation shared by :func:`.strategy_graph.compute_facts`
    (per deck) and the Phase-A combo-edge authoring (:mod:`.combo_edges`,
    ADR 0021 B1), so the two reads of a card's attributes never drift
    (ADR 0001 one-named-rule).  Synth attributes (card-field predicates) are
    NOT included here -- :func:`.strategy_graph.compute_facts` unions those separately
    from the full card via :func:`.vocab.synth_attrs_for`.
    """
    # One matcher (ADR 0009): a CardAttribute -- bucket or payoff -- is carried iff
    # its compiled ``card_attributes`` query matches the card's otag closure. The
    # closure is the ancestor-closed otag set, so a view over just those otags
    # resolves every ``otag:`` query; this replaced the separate bucket
    # (OR-of-slug) and payoff (slug-intersection) tests with identical membership.
    # The event-anchor / enchantment / EDHREC lines stay direct closure
    # intersections (already clean ``otag:name`` membership, out of scope).
    view = CardView(otags=frozenset(closure), keywords=frozenset(), type_line='', oracle_text_lower='', mana_cost='')
    attrs = {name for name, pred in assets.card_attr_preds if pred(view)}
    attrs |= closure & assets.event_anchors
    attrs |= closure & ENCH_ATTR_SLUGS
    attrs |= closure & EDHREC_ATTR_SLUGS
    return attrs
