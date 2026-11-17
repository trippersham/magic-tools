"""NAMING layer — thin, derived. Density-rank → label → confidence → combo override.

Reads the :class:`~.strategy_graph.FactsContext` and derives the label with NO modularity
vote: the eligible identity archetypes are ranked by weighted card quantity, the
top is the label, and the #1-vs-#2 margin feeds confidence.

Carried fix **F4** lives here: the win-combo naming override is gated by a
dense-primary guard so a *soft* mana-sink promotion can never displace a dense,
dominant proactive plan (Ozai → burn) while scattered cEDH residues
(Kess/Urza/K'rrik) still flip to combo.
"""

from __future__ import annotations

from pipeline.transforms.strategy.combo import combo_label
from pipeline.transforms.strategy.strategy_graph import FactsContext

#: The combo-override headline label — a SEPARATE axis from ``archetype_identity``
#: (the override sets this as the deck's name while the identity set keeps the
#: pre-combo ranking). One named home for the literal (ADR 0001 §4).
ALT_WINCON_COMBO = 'alt-wincon/combo'


def headline_strategy(named: dict) -> str | None:
    """The single headline strategy label for a resolved deck — DERIVED, not stored.

    The redundant ``named_strategy`` field was removed (ADR 0014 naming-clarity
    pass): it is exactly this derivation. The headline is the combo-override label
    when a winning combo overrode the archetype identity, else the leader of the
    ``archetype_identity`` set, else ``None`` (abstain). This is the ONE named home
    for the derivation (ADR 0001 §4), consumed by ``run.py`` (label tally) and
    ``engine_synthesis`` (role-B deterministic prior).

    Accepts either a :func:`name_deck` return dict or a full :func:`~.resolve.resolve`
    record (both carry ``combo_override_applied`` + ``archetype_identity``).
    """
    if named.get('combo_override_applied'):
        return ALT_WINCON_COMBO
    ai = named.get('archetype_identity') or []
    return ai[0]['strategy'] if ai else None


def name_deck(ctx: FactsContext) -> dict:
    cfg = ctx.cfg
    strategies = ctx.strategies
    arch_members = ctx.arch_members
    attr_weight = ctx.attr_weight
    nodes = ctx.nodes
    support = ctx.support
    facts_only = ctx.facts_only
    ramp_axis = ctx.ramp_axis

    from pipeline.transforms.strategy.determine_strategy_substantiveness import requirement_for

    def eligible(arch: str, st: dict) -> bool:
        # Identity gating is DATA: the facts-only families are never named, and
        # every other archetype's substantiveness bar is a Requirement selected by
        # data (never by an ``arch == '<name>'`` branch) and dispatched on its kind.
        if arch in facts_only:
            return False
        return requirement_for(arch, cfg).satisfied(st, ctx, arch)

    arch_has_cmd = ctx.arch_has_cmd
    ranked = sorted(
        ((arch, st) for arch, st in strategies.items() if eligible(arch, st)),
        key=lambda kv: (-kv[1]['weighted_quantity'], not arch_has_cmd.get(kv[0], False), kv[0]),
    )
    ranked_all = sorted(
        ((arch, st['weighted_quantity'], st['requires_met']) for arch, st in strategies.items() if st['is_identity']),
        key=lambda kv: (-kv[1], kv[0]),
    )

    name = None
    abstain_reason = None
    margin = None
    tie = False

    if ranked:
        top_arch, top_st = ranked[0]
        wq1 = top_st['weighted_quantity']
        wq2 = ranked[1][1]['weighted_quantity'] if len(ranked) > 1 else 0.0
        name = top_arch
        margin = round(max(0.0, 1.0 - wq2 / wq1) if wq1 else 0.0, 3)
        tie = len(ranked) > 1 and abs(wq1 - wq2) < 1e-9
    else:
        # No substantive archetype ranked. (spellslinger is named via `ranked` when
        # eligible — member_count(3)==requires_met, ADR 0002; its old last-resort
        # fallback here was provably dead and was removed.)
        if ctx.coverage < 0.30:
            abstain_reason = 'low-coverage'
        elif not any(a not in support for a in nodes):
            if ramp_axis['cedh_level']:
                name = 'ramp/big-mana'
                margin = 1.0
            else:
                abstain_reason = 'only-support-axis'
        else:
            abstain_reason = 'no-coherent-engine'

    conflict = None
    if len(ranked) >= 2:
        a0, s0 = ranked[0]
        a1, s1 = ranked[1]
        if s1['weighted_quantity'] >= 0.6 * s0['weighted_quantity']:
            bridge = len(arch_members[a0] & arch_members[a1])
            conflict = {
                'engines': [a0, a1],
                'wq0': s0['weighted_quantity'],
                'wq1': s1['weighted_quantity'],
                'shared_cards': bridge,
                'signal': 'two comparable identities'
                + (' (low margin)' if margin is not None and margin < 0.25 else ''),
            }

    # ---- confidence (density-rank based)
    confidence = conf_score = conf_sig = None
    if name is not None:
        top_st = strategies.get(name) or {'weighted_quantity': 0.0, 'n_member_cards': 0}
        wq1 = max(top_st['weighted_quantity'], 1e-6)
        total_identity = ctx.identity_mass
        marg = margin if margin is not None else 1.0
        share = min(1.0, top_st['weighted_quantity'] / total_identity)
        n_comparable = 1 + sum(1 for _arch, st in ranked[1:] if st['weighted_quantity'] >= 0.5 * wq1)
        comp_term = max(0.0, 1.0 - (n_comparable - 1) / 3.0)
        total_node_mass = sum(attr_weight[a] for a in nodes) or 1e-6
        span = top_st['weighted_quantity'] / total_node_mass
        span_term = min(1.0, span / 0.4)
        coherence = (
            0.55 * marg + 0.20 * comp_term + 0.25 * (0.0 if (conflict and conflict['shared_cards'] <= 1) else 1.0)
        )
        substance = 0.6 * share + 0.4 * span_term
        conf_score = round((coherence * substance) ** 0.5, 3)
        confidence = 'HIGH' if conf_score >= cfg.conf_high else ('LOW' if conf_score < cfg.conf_low else 'MED')
        conf_sig = {
            'margin': marg,
            'identity_share': round(share, 3),
            'span': round(span, 3),
            'n_comparable': n_comparable,
            'coherence': round(coherence, 3),
            'substance': round(substance, 3),
            'named_wq': top_st['weighted_quantity'],
            'total_identity_mass': round(total_identity, 1),
            'tie': tie,
        }

    # ========================================= T5 A3 + F4: WIN-COMBO NAMING OVERRIDE
    pre_combo_name = name
    residue_confidence = confidence
    combo_override_applied = False
    combo_identity = None
    win_combos = ctx.win_combos
    override_eligible = len(win_combos) >= cfg.combo_win_min and ((name is None) or (confidence in cfg.combo_conf))
    # F4 dense-primary guard: block the override when the pre-combo primary is a
    # dense, dominant proactive plan (owns >= combo_primary_share of identity mass
    # AND clears combo_primary_margin over #2). A soft mana-sink promotion cannot
    # override such a plan; scattered cEDH residues (share < 0.50) still flip.
    #
    # SHARE is the primary separator; MARGIN is a secondary guard. Both must hold:
    # a deck that is dense (share >= 0.50) but has a close #2 (margin < 0.30) is
    # dense-but-NOT-dominant and is NOT protected — it will flip to combo. That
    # margin-as-discriminator case (share held constant, only margin moved across
    # the threshold) is pinned by tests/test_f4_margin_gate.py.
    primary_dominates = (
        pre_combo_name is not None
        and conf_sig is not None
        and conf_sig['identity_share'] >= cfg.combo_primary_share
        and conf_sig['margin'] >= cfg.combo_primary_margin
    )
    override_blocked = override_eligible and primary_dominates
    # F5 dense-competing-primary flag: a deck whose pre-combo primary owns a dense
    # share of the identity mass (>= combo_primary_share) but slipped past the F4
    # MARGIN gate (close #2, margin < combo_primary_margin) is dense-but-not-
    # dominant — the flip to combo is acceptable, but the combo label must reflect
    # the real ambiguity of a contested primary with MED confidence, not HIGH. A
    # genuinely weak residue (share < combo_primary_share: Kess/Urza/K'rrik) is not
    # contested and keeps HIGH.
    combo_dense_primary = (
        override_eligible
        and not primary_dominates
        and conf_sig is not None
        and conf_sig['identity_share'] >= cfg.combo_primary_share
    )
    if override_eligible and not primary_dominates:

        def _repkey(c: dict) -> tuple:
            return (len(c['card_names']), 0 if c['win'] else 1, combo_label(c['result']), c['variant_id'])

        rep = sorted(win_combos, key=_repkey)[0]
        combo_identity = f'{combo_label(rep["result"])} combo [{"+".join(sorted(rep["card_names"]))}]'
        name = ALT_WINCON_COMBO
        combo_override_applied = True
        margin = 1.0
        tie = False
        confidence = 'MED' if combo_dense_primary else 'HIGH'
        conf_score = None
        abstain_reason = None

    strat_list = [
        {
            'label': arch,
            'weighted_quantity': st['weighted_quantity'],
            'n_member_cards': st['n_member_cards'],
            'requires_met': st['requires_met'],
            'eligible': eligible(arch, st),
        }
        for arch, st in sorted(strategies.items(), key=lambda kv: (-kv[1]['weighted_quantity'], kv[0]))
        if st['is_identity']
    ][:6]

    # ---- archetype_identity: the ranked identity SET (top-N), the authoritative
    # read that replaces the single-label collapse. Primacy is a per-deck relation,
    # so the set hangs off the deck as a ranked list of strategy refs. Derived from
    # the eligible `ranked` list (the same gate that feeds the headline label):
    # include strategies with wq >= identity_ratio * leader, capped at max_identity.
    # Combo is a SEPARATE axis (`combo_identity`) and does not enter this set.
    #
    # "archetype" is DeckCheck/EDHREC's own word for a ranked set of themes; the
    # name disambiguates this membership set from the separate `engine` layer.
    archetype_identity: list[dict] = []
    identity_gap = None
    if ranked:
        lead_wq = ranked[0][1]['weighted_quantity'] or 1e-6
        im = ctx.identity_mass or 1e-6
        for arch, st in ranked[: cfg.max_identity]:
            wq = st['weighted_quantity']
            if wq < cfg.identity_ratio * lead_wq:
                break
            archetype_identity.append({'strategy': arch, 'weighted_quantity': wq, 'share': round(min(1.0, wq / im), 3)})
        inc = len(archetype_identity)
        if inc < len(ranked):
            # set-boundary cleanliness: the drop from the last included to the
            # first excluded. High gap = clean boundary (confident set); a small
            # gap = the tail bled past the cutoff (fuzzy set).
            last_in = ranked[inc - 1][1]['weighted_quantity'] or 1e-6
            first_out = ranked[inc][1]['weighted_quantity']
            identity_gap = round(max(0.0, 1.0 - first_out / last_in), 3)
        else:
            identity_gap = 1.0
    if not archetype_identity and name is not None and not combo_override_applied:
        # fallback-named deck (spellslinger / ramp only-support) with no `ranked`
        # entry — keep the invariant that a non-combo named deck has a non-empty set.
        wq = (strategies.get(name) or {}).get('weighted_quantity', 0.0)
        archetype_identity = [{'strategy': name, 'weighted_quantity': wq, 'share': 1.0}]
        identity_gap = 1.0

    return {
        'archetype_identity': archetype_identity,
        'archetype_identity_gap': identity_gap,
        'tribe': ctx.tribe_info['top'],
        'tribe_names_strategy': name == 'typal/tribal',
        'tribes_ranked': [(d['tribe'], d['score']) for d in ctx.tribe_info['ranked'][:5]],
        'abstain': name is None,
        'abstain_reason': abstain_reason,
        'naming_margin': margin,
        'naming_tie': tie,
        'demoted_archetypes': ctx.demoted_archetypes,
        'confidence': confidence,
        'confidence_score': conf_score,
        'confidence_signals': conf_sig,
        'ranked_identity': [(a, w, rm) for a, w, rm in ranked_all][:8],
        'strategies': strat_list,
        'conflict': conflict,
        'combo_override_applied': combo_override_applied,
        'combo_identity': combo_identity,
        'pre_combo_name': pre_combo_name,
        'residue_confidence': residue_confidence,
        'combo_override_blocked': override_blocked,
        'combo_override_dense_primary': combo_dense_primary,
    }
