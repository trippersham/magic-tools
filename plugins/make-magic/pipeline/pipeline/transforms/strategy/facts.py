"""FACTS layer — the deterministic per-deck strategy graph.

Set-membership + counting only; does NOT depend on clustering. Produces the
inspectable ``facts`` object (attributes present + weighted counts, every
archetype strategy that fires + its weighted card quantity / requires_met /
member cards, convergent engines, support/degree quantities, answered_by facts,
and core-combo facts) plus a :class:`FactsContext` carrying the intermediate
state the thin NAMING layer (:mod:`.naming`) reads.

The modularity communities and convergent event components are STRUCTURE-ONLY:
they are reported as ``facts["engines"]`` and never decide the label.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from pipeline.transforms.strategy import _graph
from pipeline.transforms.strategy.assets import (
    ANSWERS_MAP,
    ANSWERS_MAP_ALL_SLUGS,
    StrategyAssets,
    is_graveyard_attr,
    is_land,
    load_assets,
)
from pipeline.transforms.strategy.combo import (
    LETHAL_MANA_SINK_NAMES,
    combo_label,
    combo_norm,
    combos_in_deck,
)
from pipeline.transforms.strategy.config import StrategyConfig
from pipeline.transforms.strategy.degree import compute_degree_axes, power_profile
from pipeline.transforms.strategy.tribes import compute_tribes, creature_subtypes

TYPAL = 'typal/tribal'


@dataclass
class FactsContext:
    """Intermediate state shared from the facts layer to the naming layer."""

    deck: dict
    cards: list[dict]
    n_nonland: int
    assets: StrategyAssets
    cfg: StrategyConfig
    facts: dict
    strategies: dict[str, dict]
    facts_only: set[str]
    arch_members: dict[str, set[int]]
    arch_has_cmd: dict[str, bool]
    nodes: set[str]
    support: frozenset[str]
    adens: defaultdict[str, float]
    card_w: dict[int, float]
    identity_mass: float
    coverage: float
    spell_sub: bool
    gy_sub: bool
    demotions: dict[str, bool]
    cs_axis: dict
    stax_axis: dict
    ramp_axis: dict
    protection_degree: dict
    power: dict
    tribe_info: dict
    density_signals: dict
    cmd_attrs: set[str]
    detected: list[dict]
    win_combos: list[dict]
    predicate_wins: list[dict]
    mana_sink_wins: list[dict]
    mana_sinks_present: list[str] = field(default_factory=list)


def _spell_substantive(
    node_attrs: set[str], spell_on: bool, cfg: StrategyConfig, cast_children: frozenset[str]
) -> bool:
    kids = sum(1 for a in node_attrs if a in cast_children)
    if cfg.ss_rule == 'off':
        return True
    if cfg.ss_rule == 'density_only':
        return spell_on
    if cfg.ss_rule == 'kids2_only':
        return kids >= 2
    return spell_on or kids >= 2


def _gy_substantive(node_attrs: set[str], gy_member_mass: float, identity_mass: float, cfg: StrategyConfig) -> bool:
    gy = [a for a in node_attrs if is_graveyard_attr(a)]
    if not gy:
        return False
    ratio = gy_member_mass / identity_mass if identity_mass else 0.0
    return ratio >= cfg.gy_ratio or len(gy) >= cfg.gy_distinct


def _compute_answered_by(cards: list[dict], strategies: dict[str, dict]) -> dict:
    by_slug: dict[str, list[str]] = defaultdict(list)
    for c in cards:
        if is_land(c):
            continue
        ot = set(c.get('otags', []))
        for a in ANSWERS_MAP_ALL_SLUGS & ot:
            by_slug[a].append(c['name'])
    out: dict[str, dict] = {}
    for arch, st in strategies.items():
        slugs = ANSWERS_MAP.get(arch)
        if not slugs or not st.get('requires_met'):
            continue
        in_deck = sorted({n for s in slugs for n in by_slug.get(s, [])})
        out[arch] = {'answer_slugs': slugs, 'n_in_deck_answers': len(in_deck),
                     'in_deck_answer_cards': in_deck}
    return out


def compute_facts(
    deck: dict,
    combos: list[dict] | None = None,
    assets: StrategyAssets | None = None,
) -> FactsContext:
    """Compute the deterministic facts object + the naming-layer context."""
    assets = assets or load_assets()
    cfg = assets.cfg
    cmd_w = cfg.cmd_weight
    buckets = assets.buckets
    payoff_slugs = assets.payoff_slugs
    event_anchors = assets.event_anchors
    ench_attr_re = assets.ench_attr_re

    if 'cards' not in deck:
        raise ValueError("deck missing 'cards'")
    cards = deck['cards']
    nonland = [c for c in cards if not is_land(c)]
    n_nonland = len(nonland)

    card_attrs: dict[int, set[str]] = {}
    card_closures: dict[int, set[str]] = {}
    for i, c in enumerate(cards):
        closure = {t.lower() for t in c.get('otags', [])}
        card_closures[i] = closure
        attrs = {b for b, slugs in buckets.items() if closure & set(slugs)}
        attrs |= closure & payoff_slugs
        attrs |= closure & event_anchors
        attrs |= {t for t in closure if ench_attr_re.search(t)}
        if cfg.edhrec_labels:
            from pipeline.transforms.strategy.assets import EDHREC_ATTR_SLUGS

            attrs |= closure & EDHREC_ATTR_SLUGS
            tl = c.get('type_line') or ''
            ot = (c.get('oracle_text') or '').lower()
            mc = c.get('mana_cost') or ''
            if 'Vehicle' in tl:
                attrs.add('vehicle-type')
            if ('energy counter' in ot) or ('{e}' in ot):
                attrs.add('energy-text')
            if 'extra turn' in ot:
                attrs.add('extra-turn-text')
            if 'devotion' in ot:
                pips: Counter = Counter(re.findall(r'\{([WUBRG])\}', mc))
                if (pips and max(pips.values()) >= 2) or 'devotion' in ot:
                    attrs.add('devotion-signal')
        card_attrs[i] = attrs

    # -------------------------------------------------- combo detection (per deck)
    combos = combos or []
    have_oid: set[str] = {str(c['oracle_id']) for c in cards if c.get('oracle_id')}
    have_norm: set[str] = {combo_norm(c.get('name', '')) for c in cards}
    detected = combos_in_deck(have_oid, have_norm, combos) if combos else []
    mana_sinks_present = sorted(n for n in LETHAL_MANA_SINK_NAMES if combo_norm(n) in have_norm)
    predicate_wins = [c for c in detected if c['win']]
    mana_only = [c for c in detected if not c['win'] and c['mana']]
    mana_sink_wins = mana_only if (cfg.combo_mana_sink and mana_sinks_present) else []
    win_combos = predicate_wins + mana_sink_wins

    combo_piece_idx: set[int] = set()
    name2idx: dict[str, list[int]] = defaultdict(list)
    for i, c in enumerate(cards):
        name2idx[combo_norm(c.get('name', ''))].append(i)
    for c in detected:
        for nn in c['norm_names']:
            for i in name2idx.get(nn, []):
                combo_piece_idx.add(i)

    spell_n = sum(1 for c in cards if not is_land(c) and assets.spell_re.search(c.get('type_line') or ''))
    art_n = sum(1 for c in cards if not is_land(c) and assets.art_re.search(c.get('type_line') or ''))
    ench_n = sum(1 for c in cards if not is_land(c) and assets.ench_re.search(c.get('type_line') or ''))

    tribe_info = compute_tribes(cards, sub_t=assets.sub_t, cmd_weight=cmd_w, typal_dom_share=cfg.typal_dom_share)
    tribe_ranked = tribe_info['ranked']
    dom_sub = tribe_info['top']
    dom_raw = next((d['raw_creatures'] for d in tribe_ranked if d['tribe'] == dom_sub), 0) if tribe_ranked else 0
    density_signals = {
        'spell_density': {'n': spell_n, 'on': spell_n >= assets.spell_t},
        'artifact_density': {'n': art_n, 'on': art_n >= assets.art_t},
        'enchantment_density': {'n': ench_n, 'on': ench_n >= 12},
        'creature_type_density': {'subtype': dom_sub, 'n': dom_raw, 'on': dom_raw >= assets.sub_t,
                                  'tribes_ranked': [d['tribe'] for d in tribe_ranked[:4]]},
    }

    # ---- weighted attribute densities + support cards + commander attrs
    adens: defaultdict[str, float] = defaultdict(float)
    support_cards: dict[str, set[int]] = defaultdict(set)
    cmd_attrs: set[str] = set()
    for i, c in enumerate(cards):
        w = cmd_w if c.get('role') == 'commander' else 1.0
        for a in card_attrs[i]:
            adens[a] += w
            if not is_land(c):
                support_cards[a].add(i)
            if c.get('role') == 'commander':
                cmd_attrs.add(a)

    nodes = {a for a in adens if len(support_cards[a]) >= 3 or a in cmd_attrs}
    covered = sum(1 for i, c in enumerate(cards) if not is_land(c) and (card_attrs[i] & nodes))
    coverage = covered / n_nonland if n_nonland else 0.0

    attributes_present = {a: round(adens[a], 1) for a in sorted(nodes, key=lambda a: (-adens[a], a))}

    card_w: dict[int, float] = {i: (cmd_w if cards[i].get('role') == 'commander' else 1.0)
                                for i in range(len(cards))}
    arch_members: dict[str, set[int]] = defaultdict(set)
    arch_attrs: dict[str, set[str]] = defaultdict(set)
    for i in range(len(cards)):
        if is_land(cards[i]):
            continue
        for a in card_attrs[i]:
            if a not in nodes:
                continue
            arch = assets.archetype_of_attr(a)
            if arch is None:
                continue
            arch_members[arch].add(i)
            arch_attrs[arch].add(a)

    # ---- T3 typal concentration membership (otag-less dominant-tribe fallback)
    typal_otag_members = set(arch_members.get(TYPAL, set()))
    dominant = set(tribe_info['dominant_tribes'])
    _n_ot = len(typal_otag_members)
    _ot_has_cmd = any(cards[i].get('role') == 'commander' for i in typal_otag_members)
    otag_typal_registered = _n_ot >= 3 or (_ot_has_cmd and _n_ot >= 2)
    typal_conc_members: set[int] = set()
    if dominant and not otag_typal_registered:
        for i in range(len(cards)):
            if is_land(cards[i]):
                continue
            if set(creature_subtypes(cards[i])) & dominant:
                typal_conc_members.add(i)
        if typal_conc_members:
            arch_members[TYPAL] |= typal_conc_members
            arch_attrs[TYPAL].add('subtype-concentration[' + ','.join(sorted(dominant)) + ']')
    typal_fired_by = sorted(
        ({'otag'} if typal_otag_members else set())
        | ({'concentration'} if typal_conc_members else set())
    )

    # ---- T2 correction 1: tutor-land ENABLER (conditional lands-matter contribution)
    lands_enabler = {'added': [], 'signal_present': False, 'n_candidates': 0, 'base_wq': 0.0}
    if cfg.enable_tutor_land_enabler:
        lands = 'lands-matter'
        lands_base_mem = set(arch_members.get(lands, set()))
        base_n = len(lands_base_mem)
        base_has_cmd = any(cards[i].get('role') == 'commander' for i in lands_base_mem)
        lands_signal_present = base_n >= 3 or (base_has_cmd and base_n >= 2)
        land_tutor_cards = sorted(
            i for i in range(len(cards))
            if not is_land(cards[i]) and (card_closures[i] & assets.tutor_land_slugs)
        )
        lands_enabler['signal_present'] = lands_signal_present
        lands_enabler['n_candidates'] = len(land_tutor_cards)
        lands_enabler['base_wq'] = round(sum(card_w[i] for i in lands_base_mem), 1)
        if lands_signal_present:
            for i in land_tutor_cards:
                if i not in arch_members[lands]:
                    arch_members[lands].add(i)
                    lands_enabler['added'].append(cards[i]['name'])
            if lands_enabler['added']:
                arch_attrs[lands].add('tutor-land[enabler]')

    # identity mass (each identity card counted once; for share-based guards)
    facts_only: set[str] = set() if cfg.ench_label else {'enchantments'}
    identity_cards: set[int] = set()
    for arch, mem in arch_members.items():
        if arch in facts_only:
            continue
        identity_cards |= mem
    identity_mass = sum(card_w[i] for i in identity_cards) or 1e-6

    gy_member_mass = sum(card_w[i] for i in arch_members.get('graveyard/reanimator', set()))
    spell_sub = _spell_substantive(nodes, density_signals['spell_density']['on'], cfg, assets.cast_children)
    gy_sub = _gy_substantive(nodes, gy_member_mass, identity_mass, cfg)

    def requires_met(mem: set[int]) -> bool:
        n = len(mem)
        has_cmd = any(cards[i].get('role') == 'commander' for i in mem)
        return n >= 3 or (has_cmd and n >= 2)

    strategies: dict[str, dict] = {}
    for arch, mem in arch_members.items():
        wq = sum(card_w[i] for i in mem)
        strategies[arch] = {
            'weighted_quantity': round(wq, 1),
            'n_member_cards': len(mem),
            'requires_met': requires_met(mem),
            'contributing_attrs': sorted(arch_attrs[arch], key=lambda a: (-adens[a], a)),
            'member_cards': sorted({cards[i]['name'] for i in mem}),
            'is_identity': arch not in facts_only,
        }
    if TYPAL in strategies:
        strategies[TYPAL]['tribe'] = tribe_info['top']
        strategies[TYPAL]['tribes_ranked'] = tribe_ranked[:6]
        strategies[TYPAL]['fired_by'] = typal_fired_by
        strategies[TYPAL]['dominant_tribes'] = sorted(dominant)

    support_quantities = {}
    for s in sorted(assets.support):
        if s in nodes:
            support_quantities[s] = {'weighted_quantity': round(adens[s], 1), 'n_cards': len(support_cards[s])}

    # ---- EDGES + ENGINES (structure-only)
    pair_co: Counter = Counter()
    nl_ids = [i for i, c in enumerate(cards) if not is_land(c)]
    for i in nl_ids:
        present = [a for a in card_attrs[i] if a in nodes]
        for a, b in combinations(sorted(present), 2):
            pair_co[(a, b)] += 1
    cooc_edges = []
    for (a, b), nab in pair_co.items():
        if nab < 3:
            continue
        na, nb = len(support_cards[a]), len(support_cards[b])
        if na == 0 or nb == 0:
            continue
        lift = nab * n_nonland / (na * nb)
        if lift > 1.2:
            cooc_edges.append((a, b, round(lift, 2), nab))

    def refs_to_nodes(refs: list[str]) -> list[str]:
        return [r for r in refs if r in nodes]

    event_edges = []
    for evname, ev in assets.events.items():
        for p in refs_to_nodes(ev['produced_by']):
            for c_ in refs_to_nodes(ev['triggered_by']):
                if p != c_:
                    event_edges.append((p, c_, evname))

    weighted = [(a, b, lift) for a, b, lift, _nab in cooc_edges]
    weighted += [(p, c_, 1.5) for p, c_, _ev in event_edges]
    communities = _graph.greedy_modularity_communities(nodes, weighted) or [[n] for n in sorted(nodes)]

    comps_raw = _graph.weakly_connected_components((p, c_) for p, c_, _ev in event_edges)
    # one directed edge per (p, c_) ordered pair, LAST event wins — matches the
    # networkx DiGraph the prototype built (add_edge overwrites the edge attr), so
    # the structure-only engine facts stay byte-identical to the validated output.
    pair_event: dict[tuple[str, str], str] = {}
    for p, c_, ev in event_edges:
        pair_event[(p, c_)] = ev
    out_adj: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for (p, c_), ev in pair_event.items():
        out_adj[p].append((c_, ev))
    comps = []
    for comp in comps_raw:
        evset: set[str] = set()
        chains: list[str] = []
        for u in comp:
            for v, ev in out_adj.get(u, []):
                if v in comp:
                    evset.add(ev)
                    chains.append(f'{u} --{ev}--> {v}')
        comps.append({
            'attrs': sorted(comp, key=lambda a: (-adens[a], a)),
            'events': sorted(evset),
            'chains': sorted(chains),
            'size': len(comp),
            'n_events': len(evset),
            'weight': round(sum(adens[a] for a in comp), 1),
            'archetypes': sorted({x for a in comp if (x := assets.archetype_of_attr(a))}),
        })
    comps.sort(key=lambda c: (-c['n_events'], -c['size'], -c['weight'], ','.join(c['attrs'])))

    engine_communities = []
    for cl in communities:
        cl_nodes = [a for a in cl if a in nodes]
        if len(cl_nodes) < 3:
            continue
        archs: Counter = Counter()
        for a in cl_nodes:
            x = assets.archetype_of_attr(a)
            if x:
                archs[x] += 1
        engine_communities.append({
            'size': len(cl_nodes),
            'weight': round(sum(adens[a] for a in cl_nodes), 1),
            'archetypes_merged': sorted(archs),
            'top_attrs': sorted(cl_nodes, key=lambda a: (-adens[a], a))[:8],
        })
    engine_communities.sort(key=lambda e: (-e['weight'], -e['size'], ','.join(e['top_attrs'])))

    # ---- T4 degree-axis + power readings
    deck_ci = sorted({x for c in cards for x in (c.get('color_identity') or [])})
    degree_axes = compute_degree_axes(cards, deck_ci, over_t=cfg.over_t)
    power = power_profile(cards)
    prot_axis = degree_axes['protection']
    protection_degree = {
        'n': prot_axis['n'], 'frac': prot_axis['frac'],
        'over_served': prot_axis['over_served'], 'under_served': prot_axis['under_served'],
        'reading': ('resilience/voltron' if prot_axis['over_served']
                    else ('susceptible' if prot_axis['under_served'] else 'baseline')),
    }

    # ---- T5 A: core_combo facts + elevation
    def _core_combo(c: dict, kind: str) -> dict:
        pieces = sorted(c['card_names'])
        orphan = sorted({cards[i]['name'] for nn in c['norm_names']
                         for i in name2idx.get(nn, []) if i not in identity_cards})
        return {'variant_id': c['variant_id'], 'label': combo_label(c['result']),
                'result': c['result'], 'requires': pieces, 'n_pieces': len(pieces),
                'win': c['win'], 'win_kind': kind, 'elevated_orphan_pieces': orphan}

    core_combos = (
        [_core_combo(c, 'predicate') for c in predicate_wins]
        + [_core_combo(c, 'mana-sink') for c in mana_sink_wins]
        + [_core_combo(c, 'non-win') for c in detected if c not in predicate_wins and c not in mana_sink_wins]
    )
    core_combos.sort(key=lambda d: (not d['win'], d['win_kind'], d['label'], d['variant_id']))
    elevated_cards = sorted({cards[i]['name'] for i in combo_piece_idx if i not in identity_cards})
    answered_by = _compute_answered_by(cards, strategies)

    facts = {
        'attributes_present': attributes_present,
        'density_signals': density_signals,
        'strategies': strategies,
        'core_combos': core_combos,
        'combo_elevation': {
            'n_combo_pieces': len(combo_piece_idx),
            'n_elevated_orphans': len(elevated_cards),
            'elevated_cards': elevated_cards,
            'note': 'combo pieces never density-drop; elevated_cards had NO identity archetype '
                    '(orphan noise) but are surfaced because they complete a detected combo',
        },
        'answered_by': answered_by,
        'support_quantities': support_quantities,
        'degree_axes': degree_axes,
        'power_profile': power,
        'protection_degree': protection_degree,
        'engines': {
            'convergent_event_components': comps[:4],
            'modularity_communities': engine_communities[:5],
            'note': 'engines/communities are STRUCTURE-ONLY; they do not decide the label',
        },
        'edges': {'n_cooc': len(cooc_edges), 'n_event': len(event_edges),
                  'top_cooc': sorted(cooc_edges, key=lambda e: (-e[2], e[0], e[1]))[:8]},
        'lands_enabler': {
            'signal_present': lands_enabler['signal_present'],
            'n_land_tutors': lands_enabler['n_candidates'],
            'base_lands_wq': lands_enabler['base_wq'],
            'enabler_added': sorted(lands_enabler['added']),
        },
        'tribes': {
            'top': tribe_info['top'],
            'ranked': tribe_ranked[:8],
            'concentrated_tribes': tribe_info['concentrated_tribes'],
            'dominant_tribes': sorted(dominant),
            'typal_fired_by': typal_fired_by,
            'concentration_applied': bool(typal_conc_members),
            'otag_typal_registered': otag_typal_registered,
            'note': 'tribe = 3-source (subtype+oracle-text+typal-otag) commander-weighted vote; '
                    'typal archetype fires on subtype concentration independent of typal-* otags',
        },
        'coverage': round(coverage, 3),
        'n_nodes': len(nodes),
    }

    demotions = {'spell_demoted': False, 'gy_demoted': False}
    if 'spellslinger' in strategies and strategies['spellslinger']['requires_met'] and not spell_sub:
        demotions['spell_demoted'] = True
    if 'graveyard/reanimator' in strategies and strategies['graveyard/reanimator']['requires_met'] and not gy_sub:
        demotions['gy_demoted'] = True

    arch_has_cmd = {arch: any(cards[i].get('role') == 'commander' for i in mem)
                    for arch, mem in arch_members.items()}

    return FactsContext(
        deck=deck, cards=cards, n_nonland=n_nonland, assets=assets, cfg=cfg,
        facts=facts, strategies=strategies, facts_only=facts_only,
        arch_members=dict(arch_members), arch_has_cmd=arch_has_cmd, nodes=nodes,
        support=assets.support, adens=adens, card_w=card_w, identity_mass=identity_mass,
        coverage=coverage, spell_sub=spell_sub, gy_sub=gy_sub, demotions=demotions,
        cs_axis=degree_axes['counterspells'], stax_axis=degree_axes['stax'],
        ramp_axis=degree_axes['ramp'], protection_degree=protection_degree, power=power,
        tribe_info=tribe_info, density_signals=density_signals, cmd_attrs=cmd_attrs,
        detected=detected, win_combos=win_combos, predicate_wins=predicate_wins,
        mana_sink_wins=mana_sink_wins, mana_sinks_present=mana_sinks_present,
    )
