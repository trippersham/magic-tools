"""Runtime typal-tribe vote (T3) + the carried facts-sidecar fixes (C3).

The tribe is computed at runtime, COMMANDER-WEIGHTED and MULTI-VALUED, from three
vote sources (creature-subtype counts, oracle-text tribal references, and
``typal-<tribe>`` otags). Two carried C3 fixes are folded in here:

* **C3a — dominant-tribe winner.** Where the raw vote winner diverges from the
  *dominant* tribe (the one that overwhelmingly owns the creature base,
  ``raw ≥ SUB_T`` and ``share ≥ typal_dom_share``), the reported top tribe is the
  dominant one. This is the "Fantastic Four named *Human* while *Hero* dominates"
  nit: the deck's real tribe is the concentrated one, not whichever incidental
  co-printed subtype happened to out-vote it on text.
* **C3b — pseudo-subtype filter.** Non-creature subtypes that leak in from
  dual-subtype type lines (``— Clue Plant``, ``— Equipment …``) are dropped from
  the vote entirely, so ``Clue`` / ``Equipment`` / ``Vehicle`` / ``Treasure`` …
  can never register (or win) as a "tribe".
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from pipeline.transforms.strategy.assets import is_land

#: non-tribe ``typal-*`` control slugs (meta/structural, not a creature subtype)
_TYPAL_NONTRIBE = frozenset({'creature', 'choose', 'each', 'coupling', 'coupling-distinct',
                             'exclusion', 'flagbearer-like', 'glimmer', 'historic'})
_IRREGULAR_PLURAL = {
    'ally': 'allies', 'elf': 'elves', 'dwarf': 'dwarves', 'wolf': 'wolves',
    'thief': 'thieves', 'leaf': 'leaves', 'djinn': 'djinn', 'fish': 'fish',
    'fungus': 'fungi', 'mercenary': 'mercenaries', 'homarid': 'homarids',
}

#: C3b — subtypes that are NOT creature tribes but can appear after the dash on a
#: dual-type permanent (artifact / enchantment / spell subtypes). Filtered out of
#: the tribe vote so they never masquerade as a tribe.
NON_CREATURE_SUBTYPES = frozenset({
    # artifact subtypes
    'Clue', 'Equipment', 'Food', 'Fortification', 'Gold', 'Treasure', 'Vehicle', 'Blood',
    'Powerstone', 'Map', 'Junk', 'Incubator', 'Contraption', 'Attraction', 'Bobblehead',
    # enchantment subtypes
    'Aura', 'Background', 'Cartouche', 'Case', 'Class', 'Curse', 'Role', 'Rune', 'Saga',
    'Shard', 'Shrine', 'Chorus',
    # instant/sorcery (and other non-creature) subtypes
    'Lesson', 'Arcane', 'Adventure', 'Trap', 'Omen',
})


def _plural(t: str) -> str:
    tl = t.lower()
    if tl in _IRREGULAR_PLURAL:
        return _IRREGULAR_PLURAL[tl]
    if tl.endswith('y') and not tl.endswith(('ay', 'ey', 'oy', 'uy', 'iy')):
        return tl[:-1] + 'ies'
    if tl.endswith(('s', 'x', 'z', 'ch', 'sh')):
        return tl + 'es'
    return tl + 's'


def creature_subtypes(card: dict) -> list[str]:
    """Creature subtypes after the dash, with C3b pseudo-subtypes filtered out."""
    tl = card.get('type_line') or ''
    if 'Creature' not in tl or '—' not in tl:
        return []
    right = tl.split('—', 1)[1].split('//')[0]
    return [w for w in right.replace('/', ' ').split()
            if w.isalpha() and w not in NON_CREATURE_SUBTYPES]


def typal_otag_tribe(slug: str) -> str | None:
    """Map ``typal-<tribe>`` → Title-cased single-word subtype, or None for control slugs."""
    if not slug.startswith('typal-'):
        return None
    rest = slug[len('typal-'):]
    if rest in _TYPAL_NONTRIBE or rest.startswith(('non-', 'choose')):
        return None
    word = rest.split('-')[-1]
    return word.capitalize() if word.isalpha() else None


def compute_tribes(cards: list[dict], *, sub_t: int, cmd_weight: float, typal_dom_share: float) -> dict:
    """Commander-weighted, multi-valued, ranked tribe vote. Deterministic."""
    sub_w: defaultdict[str, float] = defaultdict(float)  # commander-weighted subtype membership
    sub_raw: Counter = Counter()                         # raw creature count per subtype (concentration gate)
    otag_w: defaultdict[str, float] = defaultdict(float)
    n_creatures = 0
    for c in cards:
        if is_land(c):
            continue
        w = cmd_weight if c.get('role') == 'commander' else 1.0
        subs = set(creature_subtypes(c))
        if subs:
            n_creatures += 1
        for s in subs:
            sub_w[s] += w
            sub_raw[s] += 1
        for t in c.get('otags', []):
            tribe = typal_otag_tribe(t)
            if tribe is not None:
                otag_w[tribe] += w

    candidates = set(sub_w) | set(otag_w)
    pats = {t: re.compile(rf'\b(?:{re.escape(t.lower())}|{re.escape(_plural(t))})\b')
            for t in candidates}
    text_w: defaultdict[str, float] = defaultdict(float)
    for c in cards:
        if is_land(c):
            continue
        txt = (c.get('oracle_text') or '').lower()
        if not txt:
            continue
        w = cmd_weight if c.get('role') == 'commander' else 1.0
        for t, pat in pats.items():
            if pat.search(txt):
                text_w[t] += w

    ranked = []
    for t in candidates:
        score = sub_w[t] + otag_w[t] + text_w[t]
        share = round(sub_raw[t] / n_creatures, 3) if n_creatures else 0.0
        ranked.append({
            'tribe': t, 'score': round(score, 1),
            'sub_w': round(sub_w[t], 1), 'otag_w': round(otag_w[t], 1),
            'text_w': round(text_w[t], 1), 'raw_creatures': sub_raw[t],
            'share': share,
            'concentrated': sub_raw[t] >= sub_t,
            'dominant': sub_raw[t] >= sub_t and share >= typal_dom_share,
        })
    ranked.sort(key=lambda d: (-d['score'], -d['raw_creatures'], d['tribe']))
    concentrated = sorted(d['tribe'] for d in ranked if d['concentrated'])
    dominant = sorted(d['tribe'] for d in ranked if d['dominant'])

    # C3a — where the raw vote winner diverges from the dominant tribe, the
    # reported top is the dominant one (first dominant in ranked order, so a
    # multi-dominant deck like Sokka keeps its highest-scoring dominant tribe).
    top = ranked[0]['tribe'] if ranked else None
    first_dominant = next((d['tribe'] for d in ranked if d['dominant']), None)
    if first_dominant is not None:
        top = first_dominant

    return {'ranked': ranked, 'concentrated_tribes': concentrated,
            'dominant_tribes': dominant, 'n_creatures': n_creatures, 'top': top}
