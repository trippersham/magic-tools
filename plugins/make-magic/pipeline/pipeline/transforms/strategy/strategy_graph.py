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
    StrategyAssets,
    is_land,
    load_assets,
)
from pipeline.transforms.strategy.combo import (
    combo_label,
    combo_norm,
    combos_in_deck,
)
from pipeline.transforms.strategy.combo_win import (
    _RULE_TERMINAL_EVENTS,
    combo_win_verdict,
    resolve_fids_from_feature_names,
    walk_is_win,
    walk_win_kind,
)
from pipeline.transforms.strategy.config import FACTS_ONLY_WHEN_DISABLED, StrategyConfig
from pipeline.transforms.strategy.degree import compute_degree_axes, power_profile
from pipeline.transforms.strategy.derive_card_attributes import derive_card_attributes
from pipeline.transforms.strategy.query import CardView
from pipeline.transforms.strategy.substantiveness import event_admitted
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
    attr_weight: defaultdict[str, float]
    card_w: dict[int, float]
    identity_mass: float
    coverage: float
    demoted_archetypes: list[str]
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
        out[arch] = {'answer_slugs': slugs, 'n_in_deck_answers': len(in_deck), 'in_deck_answer_cards': in_deck}
    return out


def elect_engine_units(
    nodes: set[str],
    event_edges: list[tuple[str, str, str]],
    attr_weight: defaultdict[str, float],
    assets: StrategyAssets,
    cards: list[dict],
    earned_archetypes: set[str] | None = None,
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

    The ranked ``ranked_units`` set (0..n strategies) is chosen by node-weighted
    event-neighborhood **mass** -- ``m(U) = Σ attr_weight`` over a unit's producers U
    consumers (its ``mass``). Units are ordered by mass desc (recursive status
    breaks an EXACT mass tie only); the ranked set keeps EVERY substantive unit
    that is not a member-Jaccard-≥-0.5 near-duplicate of a kept unit — rank-don't-
    cut, no relative-mass threshold (ADR 0022, superseding the 0.6 keep-ratio of
    ADR 0014). ``recursive`` / ``interior_node`` / ``commander_coupled`` /
    ``share`` / ``formation_mode`` / ``marginal`` are reported attributes, never election
    inputs. There is no distinguished single ``lead`` — consumers that need "the
    top candidate" read ``ranked_units[0]`` locally.

    Before the election, EVENT-SUBSTANTIVENESS admission (ADR 0002 third level /
    ADR 0020) filters the candidate pool: a unit whose event carries a
    ``produced_by_commander`` Requirement is an election candidate only when the deck's
    command zone anchors that event's production (``event_admitted``). The gate is
    DATA-keyed (only ``mana-produced`` today) and reads only commander attrs +
    the event's own refs — never mass. The reported ``engines`` list stays
    complete; exclusion applies to election candidacy only.

    ARCHETYPE-SUBSTANTIVENESS admission (ADR 0002 second level / ADR 0021 B6)
    completes the ordering invariant (substantiveness -> centrality -> synthesis)
    at the ARCHETYPE grain. When ``earned_archetypes`` is supplied (the set of this
    deck's archetypes that clear their ``Requirement.satisfied`` bar — the SAME
    one-named determiner the naming layer consults), a candidate is admitted unless
    its SOLE basis is an UNEARNED archetype claim. The owner-approved exemption
    policy KEEPS a unit that (a) has >=1 EARNED ``payoff_archetype``, or (b) is
    STRUCTURALLY anchored (``commander_coupled``, or a non-bipartite anchor mode —
    terminal-producer / exogenous-environmental), or (c) makes NO archetype claim
    (empty ``payoff_archetypes`` — a pure structural event-loop). Only a unit whose
    payoff archetypes are ALL unearned AND that is not structurally anchored is
    gated. GENERIC — the earned verdict is the existing per-archetype data, no new
    threshold/knob. ``earned_archetypes=None`` (the default) disables the grain for
    the structure-only relay-augmentation path (:mod:`.relay`), which is not the P0
    election. The reported ``engines`` list stays complete; exclusion applies to
    election candidacy only.

    Purely additive to the label: reported under ``facts['engines']`` and never
    consulted by the naming layer (the label is unchanged; STRUCTURE-ONLY).
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
        return sorted(xs, key=lambda a: (-attr_weight[a], a))

    def archs(xs):
        return sorted({x for a in xs if (x := assets.archetype_of_attr(a))})

    engines = []
    for ev in sorted(ev_prod):
        P, C = ev_prod[ev], ev_cons[ev]
        relay_producers = by_w(p for p in P if p in relay)
        hub = relay_producers[0] if relay_producers else None
        engines.append(
            {
                'event': ev,
                'interior_node': hub,
                'enabler': hub or (by_w(P)[0] if P else None),
                'kind': 'recursive-engine' if hub else 'static-payoff',
                'producers': by_w(P),
                'consumers': by_w(C),
                'payoff_archetypes': archs(C),
                'mass': round(sum(attr_weight[a] for a in P | C), 1),
            }
        )
    # MASS ELECTION (ADR 0014): node-weighted event-neighborhood mass is the
    # authoritative engine signal. Each unit's mass m(U) = Σ attr_weight over
    # producers ∪ consumers is already computed as e['mass']. Order by mass  # noqa: RUF003
    # desc; recursive status breaks an EXACT mass tie only (a reported attribute,
    # never a primary sort key); event name is the final determinism tiebreak.
    engines.sort(key=lambda e: (-e['mass'], e['kind'] != 'recursive-engine', e['event']))

    # bounded forward chains: feeder_event -> fed_event (fed hub consumes feeder)
    chains = []
    for e in engines:
        if e['interior_node'] is None:
            continue
        for e2 in engines:
            if e2['event'] != e['event'] and e['interior_node'] in e2['consumers']:
                chains.append([e2['event'], e['event']])
    chains.sort()

    # commander attrs (used by commander_coupled below AND the loop probe)
    cmd_idx = [i for i, c in enumerate(cards) if c.get('role') == 'commander']
    cmd_attrs: set[str] = set()
    for i in cmd_idx:
        cmd_attrs |= {t.lower() for t in cards[i].get('otags', [])} & nodes

    def _members(e: dict) -> set[str]:
        return set(e['producers']) | set(e['consumers'])

    def _jaccard(a: set[str], b: set[str]) -> float:
        return len(a & b) / len(a | b) if (a or b) else 0.0

    def _anchor(e: dict) -> str:
        # Generic + structure-derived; NO hardcoded payoff list. The current
        # event_edges are in-deck producer -> in-deck consumer, so every unit is
        # bipartite (anchor mode 1). The terminal-producer (mode 2) and
        # exogenous-environmental (mode 3) anchors populate only once producer-
        # only / consumer-only edges exist (later pipeline steps: mana-wiring,
        # edge-author hydration, exogenous-source modelling). The terminal set is the
        # game-rules-derived reachable-to-game-end set (``_RULE_TERMINAL_EVENTS``),
        # never a frozen payoff literal.
        has_p, has_c = bool(e['producers']), bool(e['consumers'])
        if has_p and has_c:
            return 'bipartite'
        if has_p and e['event'] in _RULE_TERMINAL_EVENTS:
            return 'terminal-producer'
        if has_c and not has_p:
            return 'exogenous-environmental'
        return 'bipartite'

    # EVENT-SUBSTANTIVENESS admission (ADR 0002 third level / ADR 0020): before the
    # mass election, drop any unit whose EVENT fails its ``produced_by_commander`` bar for
    # this deck — the event's engine unit is admitted to ELECTION candidacy only when
    # the command zone anchors its production. GENERIC: keyed on the event's
    # Requirement DATA (``EVENT_SUBSTANTIVENESS``), never an ``if mana`` / deck-name
    # branch; an event with no data row is admitted (today's behavior). The reported
    # ``engines`` list stays COMPLETE — exclusion applies to candidacy only, so
    # diagnostics, chains, hubs and the loop probe are untouched.
    pool = [e for e in engines if event_admitted(e['event'], cmd_attrs, assets)]

    # ARCHETYPE-SUBSTANTIVENESS admission (ADR 0002 second level / ADR 0021 B6):
    # after the EVENT grain, gate a candidate whose SOLE basis is an UNEARNED
    # archetype claim. ``earned_archetypes`` is the deck's set of archetypes that
    # clear ``requirement_for(arch).satisfied`` — the SAME one-named determiner the
    # naming layer uses (ADR 0001/0002), passed in by ``compute_facts`` (it is the
    # only caller that gates; the relay path passes None). Exemptions (keep the
    # unit) are owner-approved and GENERIC — no deck-specific logic, no new
    # threshold: (a) >=1 EARNED payoff_archetype, (b) structurally anchored
    # (commander-coupled OR a non-bipartite anchor mode), (c) no archetype claim.
    def _archetype_admitted(e: dict) -> bool:
        claims = e['payoff_archetypes']
        if not claims:  # (c) pure structural event-loop — no archetype claim
            return True
        if any(a in earned_archetypes for a in claims):  # (a) >=1 earned claim
            return True
        if _members(e) & cmd_attrs:  # (b) structural: commander-coupled
            return True
        # (b) structural: a non-bipartite anchor mode (terminal/exogenous); else the
        # unit's SOLE basis is an unearned archetype claim -> gate.
        return _anchor(e) != 'bipartite'

    if earned_archetypes is not None:
        pool = [e for e in pool if _archetype_admitted(e)]

    # Engine SET (0..n): RANK-DON'T-CUT (ADR 0022, #72).  Keep ALL substantive
    # units ranked by mass desc, with ONLY the Jaccard<0.5 near-duplicate dedup —
    # there is no relative-mass cut.  Units are already substantive by
    # construction (node_admitted + producer-anchor event-admission upstream), so
    # a relative-mass cut among them discarded earned engines on a density-
    # sensitive ratio (the retired 0.6·top keep-ratio of ADR 0014).  Centrality
    # RANKS, it does not CUT; substantiveness (upstream) already decided which
    # units exist.
    #
    # recursive / commander_coupled / share / formation_mode / marginal are REPORTED
    # attributes only, never election inputs.
    elected: list[dict] = []
    if pool:
        total_mass = sum(e['mass'] for e in pool) or 1e-6
        kept_members: list[set[str]] = []
        for e in pool:
            mem = _members(e)
            if any(_jaccard(mem, km) >= 0.5 for km in kept_members):
                continue
            anchor = _anchor(e)
            elected.append(
                {
                    'event': e['event'],
                    'interior_node': e['interior_node'],
                    'mass': e['mass'],
                    'share': round(e['mass'] / total_mass, 3),
                    'formation_mode': anchor,
                    'recursive': e['kind'] == 'recursive-engine',
                    'commander_coupled': bool(mem & cmd_attrs),
                    'marginal': anchor == 'exogenous-environmental',
                    'payoff_archetypes': e['payoff_archetypes'],
                }
            )
            kept_members.append(mem)

    # commander-anchored loop probe
    cmd_loop = None
    if cmd_idx:
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

    if elected:
        determinism = 'mass-elected'
    elif cmd_loop is not None and cmd_loop['closes']:
        determinism = 'loop-commander-anchored'
    else:
        determinism = 'loop-or-flat-no-forward-hub'

    return {
        'relay_hubs': by_w(relay),
        'ranked_units': elected,
        'engines': engines,
        'forward_chains': chains,
        'commander_loop': cmd_loop,
        'determinism': determinism,
        'note': 'a-priori directed-graph engine isolation (per-event convergence + '
        'relay hub + DAG-bounded chains + commander loop probe); the ranked '
        '`elected` set comes from node-weighted event-neighborhood MASS '
        "(rank-don't-cut + Jaccard-0.5 dedup, ADR 0022), not recursive-first; "
        'STRUCTURE-ONLY, never decides the label',
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
        # otag-derived attrs via the shared one-named-rule derivation (also used
        # by Phase-A combo_edges, ADR 0021 B1 / 0001). EDHREC family slugs are
        # surfaced onto the card as themselves (ADR 0006).
        attrs = derive_card_attributes(closure, assets)
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
        verdict = combo_win_verdict(fids=fids, deck_card_names=have_norm)
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
        'creature_type_density': {
            'subtype': dom_sub,
            'n': dom_raw,
            'on': dom_raw >= assets.sub_t,
            'tribes_ranked': [d['tribe'] for d in tribe_ranked[:4]],
        },
    }

    # ---- weighted attribute densities + support cards + commander attrs
    attr_weight: defaultdict[str, float] = defaultdict(float)
    support_cards: dict[str, set[int]] = defaultdict(set)
    cmd_attrs: set[str] = set()
    for i, c in enumerate(cards):
        w = cmd_w if c.get('role') == 'commander' else 1.0
        for a in card_attrs[i]:
            attr_weight[a] += w
            if not is_land(c):
                support_cards[a].add(i)
            if c.get('role') == 'commander':
                cmd_attrs.add(a)

    nodes = {a for a in attr_weight if node_admitted(len(support_cards[a]), a in cmd_attrs)}
    covered = sum(1 for i, c in enumerate(cards) if not is_land(c) and (card_attrs[i] & nodes))
    coverage = covered / n_nonland if n_nonland else 0.0

    attributes_present = {a: round(attr_weight[a], 1) for a in sorted(nodes, key=lambda a: (-attr_weight[a], a))}

    card_w: dict[int, float] = {i: (cmd_w if cards[i].get('role') == 'commander' else 1.0) for i in range(len(cards))}
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
        ({'otag'} if typal_otag_members else set()) | ({'concentration'} if typal_conc_members else set())
    )

    # ---- T2 correction 1: tutor-land ENABLER (conditional lands-matter contribution)
    lands_enabler = {'added': [], 'signal_present': False, 'n_candidates': 0, 'base_wq': 0.0}
    lands = 'lands-matter'
    lands_base_mem = set(arch_members.get(lands, set()))
    base_n = len(lands_base_mem)
    base_has_cmd = any(cards[i].get('role') == 'commander' for i in lands_base_mem)
    lands_signal_present = node_eligible(base_n, base_has_cmd)
    land_tutor_cards = sorted(
        i for i in range(len(cards)) if not is_land(cards[i]) and assets.tutor_land_pred(CardView.from_card(cards[i]))
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
            'contributing_attrs': sorted(arch_attrs[arch], key=lambda a: (-attr_weight[a], a)),
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
            support_quantities[s] = {'weighted_quantity': round(attr_weight[s], 1), 'n_cards': len(support_cards[s])}

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
        comps.append(
            {
                'attrs': sorted(comp, key=lambda a: (-attr_weight[a], a)),
                'events': sorted(evset),
                'chains': sorted(chains),
                'size': len(comp),
                'n_events': len(evset),
                'weight': round(sum(attr_weight[a] for a in comp), 1),
                'archetypes': sorted({x for a in comp if (x := assets.archetype_of_attr(a))}),
            }
        )
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
        engine_communities.append(
            {
                'size': len(cl_nodes),
                'weight': round(sum(attr_weight[a] for a in cl_nodes), 1),
                'archetypes_merged': sorted(archs),
                'top_attrs': sorted(cl_nodes, key=lambda a: (-attr_weight[a], a))[:8],
            }
        )
    engine_communities.sort(key=lambda e: (-e['weight'], -e['size'], ','.join(e['top_attrs'])))

    # ---- T4 degree-axis + power readings
    deck_ci = sorted({x for c in cards for x in (c.get('color_identity') or [])})
    degree_axes = compute_degree_axes(cards, deck_ci, over_t=cfg.over_t)
    power = power_profile(cards)
    prot_axis = degree_axes['protection']
    protection_degree = {
        'n': prot_axis['n'],
        'frac': prot_axis['frac'],
        'over_served': prot_axis['over_served'],
        'under_served': prot_axis['under_served'],
        'reading': (
            'resilience/voltron'
            if prot_axis['over_served']
            else ('susceptible' if prot_axis['under_served'] else 'baseline')
        ),
    }

    # ---- T5 A: core_combo facts + elevation
    def _core_combo(c: dict, kind: str) -> dict:
        pieces = sorted(c['card_names'])
        orphan = sorted(
            {cards[i]['name'] for nn in c['norm_names'] for i in name2idx.get(nn, []) if i not in identity_cards}
        )
        return {
            'variant_id': c['variant_id'],
            'label': combo_label(c['result']),
            'result': c['result'],
            'requires': pieces,
            'n_pieces': len(pieces),
            'win': c['win'],
            'win_kind': kind,
            'elevated_orphan_pieces': orphan,
        }

    core_combos = [_core_combo(c, walk_win_kind(c.get('_walk_verdict', {}))) for c in win_combos] + [
        _core_combo(c, 'non-win') for c in detected if c not in win_combos
    ]
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
            # engine_units is computed AFTER ctx (below): the ARCHETYPE-grain
            # election gate (ADR 0021 B6) consults each archetype's
            # ``Requirement.satisfied`` verdict, which reads ctx.
            'engine_units': None,
            'note': 'engines/communities are STRUCTURE-ONLY; they do not decide the label',
        },
        'edges': {
            'n_cooc': len(cooc_edges),
            'n_event': len(event_edges),
            'top_cooc': sorted(cooc_edges, key=lambda e: (-e[2], e[0], e[1]))[:8],
        },
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

    demoted_archetypes: list[str] = []  # populated post-ctx (satisfied() reads ctx)

    arch_has_cmd = {arch: any(cards[i].get('role') == 'commander' for i in mem) for arch, mem in arch_members.items()}

    ctx = FactsContext(
        deck=deck,
        cards=cards,
        n_nonland=n_nonland,
        assets=assets,
        cfg=cfg,
        facts=facts,
        strategies=strategies,
        facts_only=facts_only,
        arch_members=dict(arch_members),
        arch_has_cmd=arch_has_cmd,
        nodes=nodes,
        support=assets.support,
        attr_weight=attr_weight,
        card_w=card_w,
        identity_mass=identity_mass,
        coverage=coverage,
        demoted_archetypes=demoted_archetypes,
        cs_axis=degree_axes['counterspells'],
        stax_axis=degree_axes['stax'],
        ramp_axis=degree_axes['ramp'],
        protection_degree=protection_degree,
        power=power,
        tribe_info=tribe_info,
        density_signals=density_signals,
        cmd_attrs=cmd_attrs,
        detected=detected,
        win_combos=win_combos,
        predicate_wins=predicate_wins,
        mana_sink_wins=mana_sink_wins,
        mana_sinks_present=mana_sinks_present,
    )

    from pipeline.transforms.strategy.substantiveness import requirement_for

    # ARCHETYPE-grain election gate (ADR 0021 B6): the set of this deck's archetypes
    # that clear their substantiveness bar, via the ONE named determiner
    # (``requirement_for(arch).satisfied``, ADR 0001/0002) — the SAME verdict the
    # naming layer consults. ``satisfied`` reads ctx, so this runs post-ctx; the
    # resulting engine_units is then placed into the (shared) facts dict.
    earned_archetypes = {arch for arch, st in strategies.items() if requirement_for(arch, cfg).satisfied(st, ctx, arch)}
    facts['engines']['engine_units'] = elect_engine_units(
        nodes, event_edges, attr_weight, assets, cards, earned_archetypes
    )

    # demoted_archetypes (ADR 0002, GENERIC -- no archetype-name branch): an archetype
    # that clears the presence bar (`requires_met`) but fails its fuller
    # `Requirement.satisfied()` (a density/axis bar) is present-but-not-substantive, so
    # it is demoted. Driven entirely by the per-archetype Requirement DATA: for default
    # `member_count` archetypes satisfied() == requires_met, so ONLY archetypes with a
    # stricter Requirement (graveyard `attr_density`, control `axis_density`) can appear.
    # Reuses `earned_archetypes` (= the satisfied() set, computed just above) so
    # satisfied() is not re-determined. Replaces the hardcoded graveyard `gy_demoted`
    # branch (and the dead `spell_demoted` flag).
    ctx.demoted_archetypes.extend(
        sorted(arch for arch, st in strategies.items() if st.get('requires_met') and arch not in earned_archetypes)
    )
    return ctx
