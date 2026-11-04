"""T4 degree-axis (membership QUANTITY) + power (efficiency QUALITY) readings.

Two independent readings over the support/interaction suite:

* **Reading 1 — degree axes** (:func:`compute_degree_axes`): per-axis raw
  meta-relative QUANTITY and over/under-served flags, measured against the FROZEN
  precon/cEDH baseline. This is the membership signal that can NAME a deck
  (control when counterspells are over-served, ramp/big-mana as a fallback).
* **Reading 2 — power profile** (:func:`power_profile`): efficiency-weighted
  QUALITY over the interaction suite. It places the deck on the precon→cEDH power
  scale and de-inverts the removal-density illusion. It NEVER changes the label.

Both are deterministic: the baseline is frozen from the gauntlet precon(12) vs
cEDH(23) bundles, not recomputed per run.
"""

from __future__ import annotations

from pipeline.transforms.crosswalk import BUCKET_ROOTS
from pipeline.transforms.strategy.assets import is_land

FROZEN_BASELINE: dict[str, dict[str, float]] = {
    'counterspells': {'precon_cnt': 1.083, 'cedh_cnt': 4.957, 'precon_frac': 0.015, 'cedh_frac': 0.066},
    'removal':       {'precon_cnt': 12.5,  'cedh_cnt': 7.391, 'precon_frac': 0.171, 'cedh_frac': 0.099},
    'ramp':          {'precon_cnt': 11.5,  'cedh_cnt': 17.348, 'precon_frac': 0.159, 'cedh_frac': 0.233},
    'tutor':         {'precon_cnt': 1.75,  'cedh_cnt': 8.043, 'precon_frac': 0.025, 'cedh_frac': 0.109},
    'draw':          {'precon_cnt': 16.0,  'cedh_cnt': 12.739, 'precon_frac': 0.219, 'cedh_frac': 0.17},
    'stax':          {'precon_cnt': 1.833, 'cedh_cnt': 2.348, 'precon_frac': 0.025, 'cedh_frac': 0.031},
    'protection':    {'precon_cnt': 2.417, 'cedh_cnt': 2.174, 'precon_frac': 0.033, 'cedh_frac': 0.029},
}
DEGREE_AXES: dict[str, set[str]] = {
    'counterspells': {'counterspell'},
    'removal': {'removal'},
    'ramp': {'ramp'},
    'tutor': {'tutor'},
    'draw': {'card-advantage'},
    'stax': {'tax', 'cast-tax', 'tax-attack', 'tax-block'},
    # read-only reuse of the committed crosswalk root so the axis can't drift
    # from the canonical protection bucket (import-only; crosswalk is unmodified)
    'protection': set(BUCKET_ROOTS['protection']),
}
_EXILE_SLUGS = {'removal-exile', 'tuck'}
_FREE_SLUGS = {'potentially-free', 'pitch-spell', 'counterspell-free'}
_DRAWBACK = 'drawback'


def compute_degree_axes(cards: list[dict], color_identity: list[str] | None, *, over_t: float) -> dict:
    """Reading 1: per-axis raw QUANTITY + meta-relative over/under flags."""
    nl = [c for c in cards if not is_land(c)]
    n_total = len(nl) or 1
    out: dict[str, dict] = {}
    ci = set(color_identity or [])
    for ax, slugs in DEGREE_AXES.items():
        n = sum(1 for c in nl if set(c.get('otags', [])) & slugs)
        frac = n / n_total
        base = FROZEN_BASELINE[ax]
        pf, cf = base['precon_frac'], base['cedh_frac']
        hi, lo = (cf, pf) if cf >= pf else (pf, cf)
        mid = lo + over_t * (hi - lo)
        inverts = cf < pf
        rec = {
            'n': n, 'frac': round(frac, 4),
            'precon_cnt': base['precon_cnt'], 'cedh_cnt': base['cedh_cnt'],
            'precon_frac': pf, 'cedh_frac': cf,
            'over_served': bool(frac >= mid),
            'under_served': bool(frac <= lo),
            'cedh_level': bool(n >= round(base['cedh_cnt']) or frac >= cf) if not inverts else None,
            'inverts': inverts,
        }
        if ax == 'counterspells':
            rec['color_gated'] = 'U' not in ci
        out[ax] = rec
    return out


def efficiency_weight(c: dict) -> float:
    """Per-card function-scoped efficiency: 1/(MV+1) x instant x exile + free x drawback."""
    mv = float(c.get('cmc') or 0.0)
    w = 1.0 / (mv + 1.0)
    tl = c.get('type_line') or ''
    instant = 'Instant' in tl or ('Flash' in (c.get('keywords') or []))
    if instant:
        w *= 1.3
    ot = set(c.get('otags', []))
    if ot & _EXILE_SLUGS:
        w *= 1.2
    if ot & _FREE_SLUGS:
        w += 0.5
    if _DRAWBACK in ot:
        w *= 0.8
    return w


def power_profile(cards: list[dict]) -> dict:
    """Reading 2: efficiency-weighted QUALITY over the counterspells+removal suite."""
    nl = [c for c in cards if not is_land(c)]
    inter_slugs = DEGREE_AXES['removal'] | DEGREE_AXES['counterspells']
    inter = [c for c in nl if set(c.get('otags', [])) & inter_slugs]
    rem = [c for c in nl if set(c.get('otags', [])) & DEGREE_AXES['removal']]
    if not inter:
        return {'n_interaction': 0, 'mean_efficiency': 0.0, 'total_efficiency': 0.0,
                'removal_count': 0, 'removal_mean_mv': None, 'removal_pct_instant': None,
                'removal_mean_efficiency': None}
    effs = [efficiency_weight(c) for c in inter]
    rem_eff = [efficiency_weight(c) for c in rem]
    rem_mv = [float(c.get('cmc') or 0.0) for c in rem]
    rem_inst = sum(1 for c in rem if 'Instant' in (c.get('type_line') or '')
                   or 'Flash' in (c.get('keywords') or []))
    return {
        'n_interaction': len(inter),
        'mean_efficiency': round(sum(effs) / len(effs), 4),
        'total_efficiency': round(sum(effs), 3),
        'removal_count': len(rem),
        'removal_mean_mv': round(sum(rem_mv) / len(rem_mv), 3) if rem_mv else None,
        'removal_pct_instant': round(rem_inst / len(rem), 3) if rem else None,
        'removal_mean_efficiency': round(sum(rem_eff) / len(rem_eff), 4) if rem_eff else None,
    }
