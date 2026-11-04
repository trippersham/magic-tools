"""Orchestration: ``resolve(deck)`` = FACTS layer + thin NAMING layer → one record.

This is the public entry point of the strategy engine. It threads an immutable
:class:`~.config.StrategyConfig`-bound :class:`~.assets.StrategyAssets` through
:func:`~.facts.compute_facts` and :func:`~.naming.name_deck`, then assembles the
byte-deterministic per-deck record (the facts graph + the derived label +
confidence + the T4 degree/power surface + the T5 combo surface).
"""

from __future__ import annotations

from pipeline.transforms.strategy.assets import StrategyAssets, load_assets
from pipeline.transforms.strategy.config import StrategyConfig
from pipeline.transforms.strategy.facts import compute_facts
from pipeline.transforms.strategy.naming import name_deck


def resolve(
    deck: dict,
    combos: list[dict] | None = None,
    *,
    cfg: StrategyConfig | None = None,
    assets: StrategyAssets | None = None,
) -> dict:
    """Resolve a deck to its strategy record (facts graph + label + confidence)."""
    if assets is None:
        assets = load_assets(cfg)
    elif cfg is not None and assets.cfg is not cfg:
        raise ValueError('cfg and assets.cfg disagree; pass only one')

    ctx = compute_facts(deck, combos, assets)
    named = name_deck(ctx)
    cards = ctx.cards
    cs_axis, stax_axis, ramp_axis = ctx.cs_axis, ctx.stax_axis, ctx.ramp_axis

    rec = {
        'name': deck.get('name'),
        'bracket': deck.get('bracket'),
        'bundle': deck.get('bundle'),
        'source': deck.get('source'),
        'commanders': [c['name'] for c in cards if c.get('role') == 'commander'],
        'commander_hint': deck.get('commander_hint'),
        'n_cards': len(cards),
        'n_nonland': ctx.n_nonland,
        'n_unresolved': len(deck.get('unresolved', [])),
        'unresolved_rate': round(len(deck.get('unresolved', [])) / max(1, len(cards)), 3),
        'coverage': round(ctx.coverage, 3),
        'density_signals': ctx.density_signals,
        'n_nodes': len(ctx.nodes),
        'cmd_attrs': sorted(ctx.cmd_attrs, key=lambda a: (-ctx.adens[a], a))[:14],
        # ---- degree/power surface (T4)
        'cs_count': cs_axis['n'],
        'cs_over_served': cs_axis['over_served'],
        'cs_color_gated': cs_axis.get('color_gated'),
        'control_cs_gate_met': cs_axis['n'] >= ctx.cfg.ctrl_min_cs,
        'stax_over_served': stax_axis['over_served'],
        'ramp_over_served': ramp_axis['over_served'],
        'ramp_cedh_level': ramp_axis['cedh_level'],
        'protection_reading': ctx.protection_degree['reading'],
        'power_mean_efficiency': ctx.power['mean_efficiency'],
        'removal_mean_efficiency': ctx.power['removal_mean_efficiency'],
        # ---- combo surface (T5)
        'n_detected_combos': len(ctx.detected),
        'n_win_combos': len(ctx.win_combos),
        'n_win_predicate': len(ctx.predicate_wins),
        'n_win_mana_sink': len(ctx.mana_sink_wins),
        'mana_sinks_present': ctx.mana_sinks_present,
        'facts': ctx.facts,
    }
    rec.update(named)
    return rec
