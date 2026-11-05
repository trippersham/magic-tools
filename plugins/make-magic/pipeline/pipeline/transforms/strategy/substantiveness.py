"""Generic substantiveness gating — one algorithm, everything else data.

The identity-gate asks a single question of every archetype: *is this identity
substantive enough to name?* Historically that was ~5 ``if arch == '<name>'``
branches in :func:`~.naming.name_deck`'s ``eligible()``. This module replaces the
name-keyed branches with a closed set of :class:`Requirement` KINDS plus a DATA
map (``SUBSTANTIVENESS``) that assigns each archetype its bar. One evaluator
dispatches on ``req.kind`` — **never on an archetype name**.

The litmus (asserted by :mod:`.tests.test_substantiveness_generic`):

    Add a mechanic -> add a ``SUBSTANTIVENESS`` data row (or rely on the default).
    Change how substance is JUDGED -> add a Requirement kind here, once, shared
    by every archetype thereafter.

The kinds are the two non-arbitrary ways an identity is earned:

* **presence of payoffs** — :data:`member_count` (the default): >= N member cards.
* **degree of a common utility** — :data:`type_density` (a card-TYPE density, e.g.
  spellslinger's instant/sorcery mass), :data:`attr_density` (the mass of an
  attribute FAMILY, e.g. graveyard), :data:`axis_density` (a degree axis read as
  identity, e.g. control's counterspell count).

Byte-preserving 1:1 map of the legacy ``eligible()`` (see the refactor plan):

====================  ===========================================  ==============
archetype             Requirement                                  legacy gate
====================  ===========================================  ==============
spellslinger          ``type_density(I/S, rule=ss_rule)``          ``spell_sub``
graveyard/reanimator  ``attr_density('graveyard', gy_ratio, gy_distinct)``  ``gy_sub``
control/permission    ``axis_density('counterspells', ctrl_min_cs)``  ``cs >= ctrl_min_cs``
EDHREC new labels     ``member_count(3, noncmd=2)``                 EDHREC 2nd gate
all others (~33)      ``member_count(3)``                          ``requires_met``
====================  ===========================================  ==============

The thresholds (``ss_rule`` / ``gy_ratio`` / ``gy_distinct`` / ``ctrl_min_cs``)
are rehomed from free-floating globals into the declared PARAMS of a specific
archetype's Requirement, sourced from the config fields for back-compat.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

from pipeline.transforms.strategy.assets import EDHREC_NEW_LABELS, is_graveyard_attr

if TYPE_CHECKING:
    from pipeline.transforms.strategy.config import StrategyConfig
    from pipeline.transforms.strategy.facts import FactsContext

#: Attribute-FAMILY membership predicates, keyed by family name. An
#: ``attr_density`` Requirement names its family here; the predicate decides which
#: present node attributes belong to it. ``graveyard`` is the single
#: ``is_graveyard_attr`` definition (NOT a parallel copy — it is a broader,
#: startswith-based family than the routing graveyard query, so it is referenced,
#: not re-derived).
FAMILY_PREDICATES: dict[str, Callable[[str], bool]] = {
    'graveyard': is_graveyard_attr,
}


@dataclass(frozen=True)
class Requirement:
    """An archetype's substantiveness bar as data: a kind + its params.

    ``satisfied`` applies the universal payoff gate (``requires_met``) and then the
    kind's extra condition. It dispatches on :attr:`kind` only — zero archetype-name
    branches — so the gate is one algorithm over a closed kind set.
    """

    kind: str
    params: dict[str, Any]

    def satisfied(self, st: dict, ctx: FactsContext, arch: str) -> bool:
        if not st['requires_met']:
            return False
        return _EVALUATORS[self.kind](self.params, st, ctx, arch)


# --- the closed set of kind evaluators (the ONLY substantiveness algorithms) ---
# Each returns the kind's EXTRA condition; the universal ``requires_met`` gate is
# applied by Requirement.satisfied before dispatch.

def _eval_member_count(params: dict, st: dict, ctx: FactsContext, arch: str) -> bool:
    """Presence of payoffs: >= ``min`` members, and (optionally) >= ``noncmd``
    non-commander members. ``noncmd=0`` is the pure ``requires_met`` default."""
    noncmd = params.get('noncmd', 0)
    if noncmd <= 0:
        return True
    mem = ctx.arch_members.get(arch, set())
    if len(mem) < params['min']:
        return False
    nc = sum(1 for i in mem if ctx.cards[i].get('role') != 'commander')
    return nc >= noncmd


def _eval_type_density(params: dict, st: dict, ctx: FactsContext, arch: str) -> bool:
    """Density of a card TYPE (spellslinger instant/sorcery). The facts layer
    measures it from the ``rule`` param (``cfg.ss_rule``) into ``ctx.spell_sub``."""
    return ctx.spell_sub


def _eval_attr_density(params: dict, st: dict, ctx: FactsContext, arch: str) -> bool:
    """Mass of an attribute FAMILY: present-node family breadth >= ``distinct`` OR
    the archetype's member mass >= ``ratio`` of the identity mass."""
    pred = FAMILY_PREDICATES[params['family']]
    fam = [a for a in ctx.nodes if pred(a)]
    if not fam:
        return False
    mass = sum(ctx.card_w[i] for i in ctx.arch_members.get(arch, set()))
    ratio = mass / ctx.identity_mass if ctx.identity_mass else 0.0
    return ratio >= params['ratio'] or len(fam) >= params['distinct']


def _eval_axis_density(params: dict, st: dict, ctx: FactsContext, arch: str) -> bool:
    """A degree axis read as identity: the axis count >= ``min``. Reuses the one
    ``compute_degree_axes`` reading (``ctx.facts['degree_axes']``) — no parallel code."""
    axis = ctx.facts['degree_axes'][params['axis']]
    return axis['n'] >= params['min']


_EVALUATORS: dict[str, Callable[[dict, dict, Any, str], bool]] = {
    'member_count': _eval_member_count,
    'type_density': _eval_type_density,
    'attr_density': _eval_attr_density,
    'axis_density': _eval_axis_density,
}

#: The default bar: presence of payoffs (== legacy ``requires_met``).
DEFAULT_REQUIREMENT = Requirement('member_count', {'min': 3, 'noncmd': 0})
#: The EDHREC new-label bar: the "second special gate" folded into one row.
EDHREC_REQUIREMENT = Requirement('member_count', {'min': 3, 'noncmd': 2})


@cache
def build_substantiveness(cfg: StrategyConfig) -> dict[str, Requirement]:
    """The ``SUBSTANTIVENESS`` DATA map: archetype -> its declared Requirement.

    Memoized per (frozen) config. Thresholds source from the config fields, so the
    params are co-located and inspectable while staying back-compatible.
    """
    return {
        'spellslinger': Requirement(
            'type_density', {'types': ('instant', 'sorcery'), 'rule': cfg.ss_rule}),
        'graveyard/reanimator': Requirement(
            'attr_density', {'family': 'graveyard', 'ratio': cfg.gy_ratio, 'distinct': cfg.gy_distinct}),
        'control/permission': Requirement(
            'axis_density', {'axis': 'counterspells', 'min': cfg.ctrl_min_cs}),
    }


def requirement_for(arch: str, cfg: StrategyConfig) -> Requirement:
    """Select ``arch``'s Requirement — data lookup, never an archetype-name branch.

    EDHREC new-label identities (only active when ``cfg.edhrec_labels``) take the
    folded ``member_count(3, noncmd=2)`` bar; the per-archetype ``SUBSTANTIVENESS``
    entries take theirs; everything else falls to the ``member_count(3)`` default.
    """
    if cfg.edhrec_labels and arch in EDHREC_NEW_LABELS:
        return EDHREC_REQUIREMENT
    return build_substantiveness(cfg).get(arch, DEFAULT_REQUIREMENT)
