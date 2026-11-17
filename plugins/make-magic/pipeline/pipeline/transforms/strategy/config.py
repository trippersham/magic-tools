"""Strategy-engine configuration — the validated T5.5 knobs as a frozen settings model.

Every value defaults to the setting the validated prototype (``facts_naming_t55``)
ran under (``GROUPS=all``, ``EDHREC_LABELS=on``, ``COMBO_OVERRIDE=on`` …), so a
default-constructed :class:`StrategyConfig` reproduces the validated engine.

:class:`StrategyConfig` is a ``pydantic-settings`` :class:`BaseSettings` model
(matching :mod:`pipeline.config`): ``frozen=True`` so it stays hashable (it is the
``@cache`` key for :func:`~.assets.load_assets` and
:func:`~.substantiveness.build_substantiveness`), and env-overridable only through
the opt-in, namespaced ``MAKEMAGIC_STRATEGY_`` prefix. No ``env_file`` is read, so
a default construction with no such env set reproduces the validated engine byte
for byte.

Two fields are NEW in this consolidation (carried fix **F4** — the Ozai
over-fire): :attr:`combo_primary_share` / :attr:`combo_primary_margin`. They
tighten the win-combo naming override so a *soft* mana-sink promotion can never
displace a dense, dominant primary plan (see :mod:`.naming`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_settings import BaseSettings, SettingsConfigDict

#: The v4 attribute groups the prototype folds into the bucket set at the
#: validated default. Order is fixed for determinism.
#:
#: Fix **J2/B6** (now permanent): the anthem over-fire — the BROAD
#: ``power-matters`` / ``toughness-matters`` slugs routing into ``anthem/go-wide`` —
#: is removed. The ``deconf_anthem`` group that carried the broad routing was
#: DELETED (no routing identity, no ``_v4_groups`` entry), so it can no longer be
#: re-enabled. The anthem archetype is fed ONLY the NARROW ``power-matters-self``
#: payoff, folded into the ``anthem`` bucket by the vocab data itself
#: (``_v4_base_buckets['anthem']`` in ``attribute_catalog.json``). See ADR 0006.
ALL_GROUPS: tuple[str, ...] = (
    'new_buckets',
    'dual',
    'affinity',
    'ramp_tutor',
    'dual_land',
    'dual_art',
    'dual_ench',
    'dual_sac',
)
#: The ``GROUPS=refined`` subset (kept for parity with the prototype knob).
REFINED_GROUPS: frozenset[str] = frozenset({'new_buckets', 'affinity', 'dual_land', 'dual_art', 'dual_sac'})

#: Archetypes reported as FACTS but never NAMING candidates — the ``facts_only`` set
#: as DATA (not an inline literal in the facts branch). The enchantments synergy
#: strategy double-counts synergies the enchantress identity already names, so it is
#: always facts-only (the ``ench_label`` knob was removed — universal-or-gone, ADR 0006).
FACTS_ONLY_WHEN_DISABLED: frozenset[str] = frozenset({'enchantments'})


class StrategyConfig(BaseSettings):
    """All strategy-engine knobs. Frozen + fully defaulted → deterministic + hashable.

    A ``pydantic-settings`` model: ``frozen=True`` keeps it hashable (so it serves
    as the ``@cache`` key for ``load_assets`` / ``build_substantiveness``), and
    every field is overridable only via its ``MAKEMAGIC_STRATEGY_``-prefixed env
    var. No ``env_file`` is read, so an unconfigured construction reproduces the
    validated engine exactly.
    """

    model_config = SettingsConfigDict(frozen=True, env_prefix='MAKEMAGIC_STRATEGY_', extra='ignore')

    if TYPE_CHECKING:
        # pydantic generates __hash__ for frozen=True models at class-creation time;
        # this stub makes the method visible to pyright (which otherwise sees
        # BaseSettings.__hash__ as None).
        def __hash__(self) -> int: ...

    # ---- bucket / group assembly (vocab-driven) ----------------------------
    enabled_groups: frozenset[str] = frozenset(ALL_GROUPS)

    # ---- substantiveness / density knobs -----------------------------------
    gy_distinct: int = 4
    gy_ratio: float = 0.15

    # ---- confidence thresholds ---------------------------------------------
    conf_high: float = 0.66
    conf_low: float = 0.42

    # ---- strategy-identity set (top-N identity; replaces single-label collapse) --
    #: A deck's identity is a ranked SET, not one privileged primary — primacy is a
    #: per-deck *relation* over strategies, not an intrinsic strategy property. The
    #: set includes eligible strategies whose ``weighted_quantity`` is at least
    #: ``identity_ratio`` * the leader's, capped at ``max_identity`` — the measured
    #: "top 2-3 with a steep drop-off". Both are tunable knobs.
    identity_ratio: float = 0.5
    max_identity: int = 3

    # ---- T3 typal concentration --------------------------------------------
    typal_dom_share: float = 0.70

    # ---- T4 degree / power readings ----------------------------------------
    over_t: float = 0.5
    ctrl_min_cs: int = 3

    # ---- T5 combo correction -----------------------------------------------
    combo_conf: frozenset[str] = frozenset({'LOW'})
    combo_win_min: int = 2

    # ---- F4 (carried fix): dense-primary guard on the win-combo override ----
    #: The win-combo override is BLOCKED when the pre-combo primary identity
    #: both owns at least this share of the identity mass AND clears
    #: ``combo_primary_margin`` over the #2 identity — i.e. a genuinely dense,
    #: dominant proactive plan (Ozai → burn) is never overridden by a soft
    #: mana-sink promotion, while scattered cEDH residues (Kess/Urza/K'rrik,
    #: share < 0.50) still flip to combo.
    combo_primary_share: float = 0.50
    combo_primary_margin: float = 0.30

    # ---- commander weighting -----------------------------------------------
    cmd_weight: float = 5.0

    def with_overrides(self, **kw: object) -> StrategyConfig:
        """Return a copy with the given fields replaced (frozen-model copy)."""
        return self.model_copy(update=kw)


#: Reusable default instance (the validated T5.5 configuration).
DEFAULT_CONFIG: StrategyConfig = StrategyConfig()
