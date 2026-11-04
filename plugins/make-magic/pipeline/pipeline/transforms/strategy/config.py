"""Strategy-engine configuration — the validated T5.5 knobs as a frozen dataclass.

Every value defaults to the setting the validated prototype (``facts_naming_t55``)
ran under (``GROUPS=all``, ``EDHREC_LABELS=on``, ``COMBO_OVERRIDE=on`` …), so a
default-constructed :class:`StrategyConfig` reproduces the validated engine.

Two fields are NEW in this consolidation (carried fix **F4** — the Ozai
over-fire): :attr:`combo_primary_share` / :attr:`combo_primary_margin`. They
tighten the win-combo naming override so a *soft* mana-sink promotion can never
displace a dense, dominant primary plan (see :mod:`.naming`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

#: The v4 attribute groups the prototype folds into the bucket set at the
#: validated default. Order is fixed for determinism.
#:
#: Fix **J2/B6**: ``deconf_anthem`` is deliberately NOT in the default set. That
#: group routes the BROAD ``power-matters`` / ``toughness-matters`` slugs into the
#: ``anthem/go-wide`` archetype, the over-fire T2/B6 replaced with the NARROW
#: ``power-matters-self`` payoff (see :attr:`StrategyConfig.anthem_payoff`). The
#: capability is kept available as a non-default knob — it is still a valid key in
#: ``_v4_groups`` and can be re-enabled via
#: ``enabled_groups=DEFAULT_CONFIG.enabled_groups | {'deconf_anthem'}`` — but the
#: default engine feeds ONLY ``power-matters-self`` to the anthem archetype.
ALL_GROUPS: tuple[str, ...] = (
    'new_buckets', 'dual', 'affinity', 'ramp_tutor',
    'dual_land', 'dual_art', 'dual_ench', 'dual_sac',
)
#: The ``GROUPS=refined`` subset (kept for parity with the prototype knob).
REFINED_GROUPS: frozenset[str] = frozenset(
    {'new_buckets', 'affinity', 'dual_land', 'dual_art', 'dual_sac'}
)


@dataclass(frozen=True)
class StrategyConfig:
    """All strategy-engine knobs. Frozen + fully defaulted → deterministic."""

    # ---- bucket / group assembly (vocab-driven) ----------------------------
    enabled_groups: frozenset[str] = frozenset(ALL_GROUPS)
    #: narrow go-tall anthem payoff slug(s) folded into the ``anthem`` bucket
    anthem_payoff: frozenset[str] = frozenset({'power-matters-self'})

    # ---- substantiveness / density knobs -----------------------------------
    ss_rule: str = 'off'          # spellslinger substantiveness: off|density_only|kids2_only|density_or_2kids
    gy_distinct: int = 4
    gy_ratio: float = 0.15
    ench_label: bool = False      # make the enchantments archetype a NAMING candidate
    weak: frozenset[str] = frozenset()
    weak_ratio: float = 0.7

    # ---- confidence thresholds ---------------------------------------------
    conf_high: float = 0.66
    conf_low: float = 0.42

    # ---- T3 typal concentration --------------------------------------------
    typal_dom_share: float = 0.70

    # ---- T4 degree / power readings ----------------------------------------
    over_t: float = 0.5
    ctrl_min_cs: int = 3
    ramp_label: bool = True
    enable_tutor_land_enabler: bool = True

    # ---- T5 combo correction -----------------------------------------------
    combo_override: bool = True
    combo_conf: frozenset[str] = frozenset({'LOW'})
    combo_win_min: int = 2
    combo_mana_sink: bool = True

    # ---- F4 (carried fix): dense-primary guard on the win-combo override ----
    #: The win-combo override is BLOCKED when the pre-combo primary identity
    #: both owns at least this share of the identity mass AND clears
    #: ``combo_primary_margin`` over the #2 identity — i.e. a genuinely dense,
    #: dominant proactive plan (Ozai → burn) is never overridden by a soft
    #: mana-sink promotion, while scattered cEDH residues (Kess/Urza/K'rrik,
    #: share < 0.50) still flip to combo.
    combo_primary_share: float = 0.50
    combo_primary_margin: float = 0.30

    # ---- T5.5 EDHREC label space -------------------------------------------
    edhrec_labels: bool = True

    # ---- commander weighting -----------------------------------------------
    cmd_weight: float = 5.0

    def with_overrides(self, **kw: object) -> StrategyConfig:
        """Return a copy with the given fields replaced (frozen-dataclass copy)."""
        return replace(self, **kw)  # type: ignore[arg-type]


#: Reusable default instance (the validated T5.5 configuration).
DEFAULT_CONFIG: StrategyConfig = StrategyConfig()
