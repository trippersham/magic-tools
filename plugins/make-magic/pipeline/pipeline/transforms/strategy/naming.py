"""NAMING layer — thin, derived. Density-rank → label → confidence → combo override.

Reads the :class:`~.facts.FactsContext` and derives the label with NO modularity
vote: the eligible identity archetypes are ranked by weighted card quantity, the
top is the label, and the #1-vs-#2 margin feeds confidence.

Carried fix **F4** lives here: the win-combo naming override is gated by a
dense-primary guard so a *soft* mana-sink promotion can never displace a dense,
dominant proactive plan (Ozai → burn) while scattered cEDH residues
(Kess/Urza/K'rrik) still flip to combo.
"""

from __future__ import annotations

from pipeline.transforms.strategy.combo import combo_label
from pipeline.transforms.strategy.facts import FactsContext


def name_deck(ctx: FactsContext) -> dict:
    cfg = ctx.cfg
    strategies = ctx.strategies
    arch_members = ctx.arch_members
    cards = ctx.cards
    adens = ctx.adens
    nodes = ctx.nodes
    support = ctx.support
    facts_only = ctx.facts_only
    cs_axis = ctx.cs_axis
    ramp_axis = ctx.ramp_axis

    from pipeline.transforms.strategy.assets import EDHREC_NEW_LABELS

    def eligible(arch: str, st: dict) -> bool:
        if arch in facts_only:
            return False
        if not st['requires_met']:
            return False
        if arch == 'spellslinger' and not ctx.spell_sub:
            return False
        if arch == 'graveyard/reanimator' and not ctx.gy_sub:
            return False
        if arch == 'control/permission' and cs_axis['n'] < cfg.ctrl_min_cs:
            return False
        if cfg.edhrec_labels and arch in EDHREC_NEW_LABELS:
            mem = arch_members.get(arch, set())
            noncmd = sum(1 for i in mem if cards[i].get('role') != 'commander')
            if len(mem) < 3 or noncmd < 2:
                return False
        return True

    arch_has_cmd = ctx.arch_has_cmd
    ranked = sorted(
        ((arch, st) for arch, st in strategies.items() if eligible(arch, st)),
        key=lambda kv: (-kv[1]['weighted_quantity'], not arch_has_cmd.get(kv[0], False), kv[0]),
    )
    if ranked and ranked[0][0] in cfg.weak:
        promo = next(((a, s) for a, s in ranked
                      if a not in cfg.weak
                      and s['weighted_quantity'] >= cfg.weak_ratio * ranked[0][1]['weighted_quantity']),
                     None)
        if promo is not None:
            ranked = [promo] + [x for x in ranked if x[0] != promo[0]]
    ranked_all = sorted(
        ((arch, st['weighted_quantity'], st['requires_met']) for arch, st in strategies.items()
         if st['is_identity']),
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
        # DEFER #6: spellslinger last-resort fallback. Inert under DEFAULT_CONFIG
        # (ss_rule='off' keeps spellslinger eligible so it is named via `ranked`
        # above and never reaches here); only reachable under a non-default
        # ss_rule that demoted spellslinger, where it would re-name the demoted
        # deck spellslinger. Behavior intentionally left as-is.
        if ('spellslinger' in strategies and strategies['spellslinger']['requires_met']
                and not any(st['requires_met'] and arch not in facts_only and arch != 'spellslinger'
                            for arch, st in strategies.items())):
            name = 'spellslinger'
            margin = 1.0
        elif ctx.coverage < 0.30:
            abstain_reason = 'low-coverage'
        elif not any(a not in support for a in nodes):
            if cfg.ramp_label and ramp_axis['cedh_level']:
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
            conflict = {'engines': [a0, a1],
                        'wq0': s0['weighted_quantity'], 'wq1': s1['weighted_quantity'],
                        'shared_cards': bridge,
                        'signal': 'two comparable identities'
                                  + (' (low margin)' if margin is not None and margin < 0.25 else '')}

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
        total_node_mass = sum(adens[a] for a in nodes) or 1e-6
        span = top_st['weighted_quantity'] / total_node_mass
        span_term = min(1.0, span / 0.4)
        coherence = 0.55 * marg + 0.20 * comp_term + 0.25 * (0.0 if (conflict and conflict['shared_cards'] <= 1)
                                                             else 1.0)
        substance = 0.6 * share + 0.4 * span_term
        conf_score = round((coherence * substance) ** 0.5, 3)
        confidence = 'HIGH' if conf_score >= cfg.conf_high else ('LOW' if conf_score < cfg.conf_low else 'MED')
        conf_sig = {'margin': marg, 'identity_share': round(share, 3), 'span': round(span, 3),
                    'n_comparable': n_comparable, 'coherence': round(coherence, 3),
                    'substance': round(substance, 3), 'named_wq': top_st['weighted_quantity'],
                    'total_identity_mass': round(total_identity, 1), 'tie': tie}

    # ========================================= T5 A3 + F4: WIN-COMBO NAMING OVERRIDE
    pre_combo_name = name
    residue_confidence = confidence
    combo_override_applied = False
    combo_identity = None
    win_combos = ctx.win_combos
    override_eligible = (
        cfg.combo_override
        and len(win_combos) >= cfg.combo_win_min
        and ((name is None) or (confidence in cfg.combo_conf))
    )
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
    f4_blocked = override_eligible and primary_dominates
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
        combo_identity = f"{combo_label(rep['result'])} combo [{'+'.join(sorted(rep['card_names']))}]"
        name = 'alt-wincon/combo'
        combo_override_applied = True
        margin = 1.0
        tie = False
        confidence = 'MED' if combo_dense_primary else 'HIGH'
        conf_score = None
        abstain_reason = None

    strat_list = [{'label': arch, 'weighted_quantity': st['weighted_quantity'],
                   'n_member_cards': st['n_member_cards'], 'requires_met': st['requires_met'],
                   'eligible': eligible(arch, st)}
                  for arch, st in sorted(strategies.items(), key=lambda kv: (-kv[1]['weighted_quantity'], kv[0]))
                  if st['is_identity']][:6]

    return {
        'named_strategy': name,
        'tribe': ctx.tribe_info['top'],
        'tribe_names_strategy': name == 'typal/tribal',
        'tribes_ranked': [(d['tribe'], d['score']) for d in ctx.tribe_info['ranked'][:5]],
        'abstain': name is None,
        'abstain_reason': abstain_reason,
        'naming_margin': margin,
        'naming_tie': tie,
        'demotions': ctx.demotions,
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
        'combo_override_f4_blocked': f4_blocked,
        'combo_override_dense_primary': combo_dense_primary,
    }
