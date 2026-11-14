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

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from pipeline.transforms.strategy import _graph
from pipeline.transforms.strategy.assets import (
    ANSWERS_MAP,
    ANSWERS_MAP_ALL_SLUGS,
    EDHREC_ATTR_SLUGS,
    ENCH_ATTR_SLUGS,
    StrategyAssets,
    is_land,
    load_assets,
)
from pipeline.transforms.strategy.combo import (
    combo_label,
    combo_norm,
    combos_in_deck,
)
from pipeline.transforms.strategy.win_walk import (
    closure_walk,
    resolve_fids_from_feature_names,
    walk_is_win,
    walk_win_kind,
)
from pipeline.transforms.strategy.config import FACTS_ONLY_WHEN_DISABLED, StrategyConfig
from pipeline.transforms.strategy.degree import compute_degree_axes, power_profile
from pipeline.transforms.strategy.query import CardView
from pipeline.transforms.strategy.tribes import compute_tribes, creature_subtypes
from pipeline.transforms.strategy.vocab import synth_attrs_for

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


def node_eligible(support_count: int, is_cmd: bool) -> bool:
    """Substantiveness presence gate: >= 3 payoffs, or >= 2 when a commander carries
    the attr. ONE named implementation (ADR 0004) -- requires_met, lands_signal_present,
    and otag_typal_registered all call it."""
    return support_count >= 3 or (is_cmd and support_count >= 2)


def node_admitted(support_count: int, is_cmd: bool) -> bool:
    """Graph node admission: >= 3 support cards, or a commander-carried attr admitted
    UNCONDITIONALLY. ONE named implementation (ADR 0004) -- the facts `nodes` set and
    engine_report's machine-attr admission both call it. Distinct from node_eligible:
    a commander attr is admitted here at any support count."""
    return support_count >= 3 or is_cmd


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


def _isolate_engines(
    nodes: set[str],
    event_edges: list[tuple[str, str, str]],
    adens: defaultdict[str, float],
    assets: StrategyAssets,
    cards: list[dict],
) -> dict:
    """STRUCTURE-ONLY a-priori engine isolation over the DIRECTED event graph.

    Where ``convergent_event_components`` fuses everything weakly-connected into
    one blob (so a land-recursion engine and a sacrifice engine merge through the
    shared ``sac`` relay), this isolates engines WITHOUT transitive chaining:

    * per-event convergence — each event with producer-nodes AND consumer-nodes is
      one atomic bipartite engine (producers -> event -> consumers); no fusion.
    * hub classification — the engine HUB is the producer that is ALSO a *relay*
      (consumes an upstream event + mints this one): the attribute the deck's own
      machinery feeds. A relay hub => ``recursive-engine``; an event minted only by
      pure sources (``ramp``, ``flicker``) => ``static-payoff``.
    * bounded forward chains — the ONLY legal fusion: engine Y feeds engine X when
      X's relay hub consumes Y's event. DAG-bounded (the catalog is a strict
      forward DAG: 0 SCCs), so this never grows into the weak-component blob.
    * commander-anchored loop probe — a value LOOP cannot appear as an SCC in a
      forward DAG, but the commander recurs every turn; seed forward reachability
      at what the commander produces and test whether it reaches back to what the
      commander consumes. Non-empty => a commander-anchored cycle the DAG can't hold.

    Purely additive: reported under ``facts['engines']`` and never consulted by the
    naming layer (the label is unchanged).
    """
    out_adj: dict[str, set[str]] = defaultdict(set)
    in_adj: dict[str, set[str]] = defaultdict(set)
    ev_prod: dict[str, set[str]] = defaultdict(set)
    ev_cons: dict[str, set[str]] = defaultdict(set)
    for p, c_, ev in event_edges:
        out_adj[p].add(c_)
        in_adj[c_].add(p)
        ev_prod[ev].add(p)
        ev_cons[ev].add(c_)
    producers = {n for n in nodes if out_adj.get(n)}
    consumers = {n for n in nodes if in_adj.get(n)}
    relay = producers & consumers

    def by_w(xs):
        return sorted(xs, key=lambda a: (-adens[a], a))

    def archs(xs):
        return sorted({x for a in xs if (x := assets.archetype_of_attr(a))})

    engines = []
    for ev in sorted(ev_prod):
        P, C = ev_prod[ev], ev_cons[ev]
        relay_producers = by_w(p for p in P if p in relay)
        hub = relay_producers[0] if relay_producers else None
        engines.append({
            'event': ev,
            'hub': hub,
            'enabler': hub or (by_w(P)[0] if P else None),
            'kind': 'recursive-engine' if hub else 'static-payoff',
            'producers': by_w(P),
            'consumers': by_w(C),
            'payoff_archetypes': archs(C),
            'weight': round(sum(adens[a] for a in P | C), 1),
        })
    engines.sort(key=lambda e: (e['kind'] != 'recursive-engine',
                                -len(e['payoff_archetypes']), -e['weight'], e['event']))

    # bounded forward chains: feeder_event -> fed_event (fed hub consumes feeder)
    chains = []
    for e in engines:
        if e['hub'] is None:
            continue
        for e2 in engines:
            if e2['event'] != e['event'] and e['hub'] in e2['consumers']:
                chains.append([e2['event'], e['event']])
    chains.sort()

    lead = next((e for e in engines if e['kind'] == 'recursive-engine'), None)

    # commander-anchored loop probe
    cmd_loop = None
    cmd_idx = [i for i, c in enumerate(cards) if c.get('role') == 'commander']
    if cmd_idx:
        cmd_attrs: set[str] = set()
        for i in cmd_idx:
            cmd_attrs |= {t.lower() for t in cards[i].get('otags', [])} & nodes
        cmd_prod = {a for a in cmd_attrs if a in producers}
        cmd_cons = {a for a in cmd_attrs if a in consumers}
        seen = set(cmd_prod)
        stack = list(cmd_prod)
        while stack:
            u = stack.pop()
            for v in out_adj.get(u, ()):
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        back = seen & cmd_cons
        cmd_loop = {
            'commander': sorted({cards[i]['name'] for i in cmd_idx}),
            'produces': by_w(cmd_prod),
            'consumes': by_w(cmd_cons),
            'reaches_back_to': by_w(back),
            'closes': bool(back),
        }

    if lead is not None:
        determinism = 'forward-engine-isolated'
    elif cmd_loop is not None and cmd_loop['closes']:
        determinism = 'loop-commander-anchored'
    else:
        determinism = 'loop-or-flat-no-forward-hub'

    return {
        'relay_hubs': by_w(relay),
        'lead_engine': (None if lead is None else
                        {'hub': lead['hub'], 'event': lead['event'],
                         'payoff_archetypes': lead['payoff_archetypes']}),
        'engines': engines,
        'forward_chains': chains,
        'commander_loop': cmd_loop,
        'determinism': determinism,
        'note': 'a-priori directed-graph engine isolation (per-event convergence + '
                'relay hub + DAG-bounded chains + commander loop probe); STRUCTURE-ONLY, '
                'never decides the label',
    }


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
        attrs |= closure & ENCH_ATTR_SLUGS
        # EDHREC otag membership surfacing: put the EDHREC family slugs onto the
        # card as themselves so the EDHREC identities can route them. (Always on;
        # the edhrec_labels knob was removed -- ADR 0006.)
        attrs |= closure & EDHREC_ATTR_SLUGS
        # Synth attributes (vehicle/energy/extra-turn/devotion from card fields,
        # morph from keywords) come from the compiled vocab predicates — one
        # resolution path, no hand-coded field tests.
        attrs |= synth_attrs_for(c, assets.synth_predicates)
        card_attrs[i] = attrs

    # -------------------------------------------------- combo detection (per deck)
    # Win classification via the closure-mechanism walk (ADR 0016).
    # The walk is deck-aware: a combo's win-ness depends on the deck's card
    # composition (e.g. infinite mana + a mana_sink card = win).
    combos = combos or []
    have_oid: set[str] = {str(c['oracle_id']) for c in cards if c.get('oracle_id')}
    have_norm: set[str] = {combo_norm(c.get('name', '')) for c in cards}
    detected = combos_in_deck(have_oid, have_norm, combos) if combos else []

    # Run the closure walk for each detected combo to determine win-ness
    for c in detected:
        feature_names = [f.strip() for f in c['result'].split(';') if f.strip()]
        fids = resolve_fids_from_feature_names(feature_names)
        verdict = closure_walk(fids=fids, deck_card_names=have_norm)
        c['win'] = walk_is_win(verdict)
        c['_walk_verdict'] = verdict

    win_combos = [c for c in detected if c['win']]
    # All wins come from the walk — no separate predicate/mana-sink categories
    predicate_wins = win_combos
    mana_sink_wins: list[dict] = []
    mana_sinks_present: list[str] = []

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
        'enchantment_density': {'n': ench_n, 'on': ench_n >= assets.ench_t},
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

    nodes = {a for a in adens if node_admitted(len(support_cards[a]), a in cmd_attrs)}
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
    otag_typal_registered = node_eligible(_n_ot, _ot_has_cmd)
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
    lands = 'lands-matter'
    lands_base_mem = set(arch_members.get(lands, set()))
    base_n = len(lands_base_mem)
    base_has_cmd = any(cards[i].get('role') == 'commander' for i in lands_base_mem)
    lands_signal_present = node_eligible(base_n, base_has_cmd)
    land_tutor_cards = sorted(
        i for i in range(len(cards))
        if not is_land(cards[i]) and assets.tutor_land_pred(CardView.from_card(cards[i]))
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
    facts_only: set[str] = set(FACTS_ONLY_WHEN_DISABLED)
    identity_cards: set[int] = set()
    for arch, mem in arch_members.items():
        if arch in facts_only:
            continue
        identity_cards |= mem
    identity_mass = sum(card_w[i] for i in identity_cards) or 1e-6

    def requires_met(mem: set[int]) -> bool:
        n = len(mem)
        has_cmd = any(cards[i].get('role') == 'commander' for i in mem)
        return node_eligible(n, has_cmd)

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
            for c_ in refs_to_nodes(ev['triggers']):
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
        [_core_combo(c, walk_win_kind(c.get('_walk_verdict', {}))) for c in win_combos]
        + [_core_combo(c, 'non-win') for c in detected if c not in win_combos]
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
            'engine_isolation': _isolate_engines(nodes, event_edges, adens, assets, cards),
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

    arch_has_cmd = {arch: any(cards[i].get('role') == 'commander' for i in mem)
                    for arch, mem in arch_members.items()}

    ctx = FactsContext(
        deck=deck, cards=cards, n_nonland=n_nonland, assets=assets, cfg=cfg,
        facts=facts, strategies=strategies, facts_only=facts_only,
        arch_members=dict(arch_members), arch_has_cmd=arch_has_cmd, nodes=nodes,
        support=assets.support, adens=adens, card_w=card_w, identity_mass=identity_mass,
        coverage=coverage, demotions=demotions,
        cs_axis=degree_axes['counterspells'], stax_axis=degree_axes['stax'],
        ramp_axis=degree_axes['ramp'], protection_degree=protection_degree, power=power,
        tribe_info=tribe_info, density_signals=density_signals, cmd_attrs=cmd_attrs,
        detected=detected, win_combos=win_combos, predicate_wins=predicate_wins,
        mana_sink_wins=mana_sink_wins, mana_sinks_present=mana_sinks_present,
    )

    # gy_demoted: the graveyard identity clears requires_met but NOT the shared graveyard
    # attr_density Requirement (one evaluator, ADR 0002 -- no parallel copy). Equivalent to
    # the old `requires_met and not gy_sub`: satisfied() == requires_met and density, so
    # `requires_met and not satisfied()` == `requires_met and not density`. Evaluated
    # post-ctx because the evaluator reads ctx.
    from pipeline.transforms.strategy.substantiveness import requirement_for
    gy_st = strategies.get('graveyard/reanimator')
    if gy_st and gy_st['requires_met'] and not requirement_for(
            'graveyard/reanimator', cfg).satisfied(gy_st, ctx, 'graveyard/reanimator'):
        demotions['gy_demoted'] = True
    return ctx
