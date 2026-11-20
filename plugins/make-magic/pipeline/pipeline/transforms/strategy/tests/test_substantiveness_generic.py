"""Drift guard — the substantiveness LITMUS as code.

The identity-gate (``naming.eligible``) and the degree measurement
(``degree.compute_degree_axes`` / ``degree.power_profile``) must carry **zero
archetype/axis-NAME string literals in control flow**. Adding a mechanic is a
DATA row (a ``SUBSTANTIVENESS`` entry or a ``DEGREE_AXES`` key), never a new
``if arch == '<name>'`` branch. These tests fail the instant a name-keyed branch
reappears in the gate, à la :mod:`.test_distill_separation`.

The drift scan is scoped to the GATE control flow — the function bodies that
decide eligibility/degree — and deliberately NOT to the declarative data maps
(``SUBSTANTIVENESS``/``build_substantiveness``/``DEGREE_AXES``/``AXIS_COLOR_GATE``/
``INTERACTION_AXES``), where archetype/axis names are the data itself.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from pipeline.transforms.strategy import determine_strategy_substantiveness as S
from pipeline.transforms.strategy.config import DEFAULT_CONFIG
from pipeline.transforms.strategy.degree import AXIS_COLOR_GATE, DEGREE_AXES, compute_degree_axes
from pipeline.transforms.strategy.distill_strategies import ARCHETYPE_EDGES
from pipeline.transforms.strategy.strategy_graph import compute_facts

_STRATEGY_DIR = Path(__file__).resolve().parent.parent
NAMING = _STRATEGY_DIR / 'naming.py'
DEGREE = _STRATEGY_DIR / 'degree.py'
FIX = Path(__file__).resolve().parent / 'fixtures'
DECKS = FIX / 'decks'

#: The frozen per-axis record keys (W1/W2: resolve.py reads ``n``/``over_served``/…
#: out of this dict — its shape must never drift).
_AXIS_REC_KEYS = frozenset(
    {
        'n',
        'frac',
        'precon_cnt',
        'cedh_cnt',
        'precon_frac',
        'cedh_frac',
        'over_served',
        'under_served',
        'cedh_level',
        'inverts',
    }
)

#: Every archetype/strategy name the router can emit — the literals that must not
#: appear in the gate's control flow.
ARCH_NAMES = set(ARCHETYPE_EDGES) | set(ARCHETYPE_EDGES.values())
#: Every degree-axis name — likewise barred from degree.py gate control flow.
AXIS_NAMES = set(DEGREE_AXES)


def _func_defs(source: str, name: str) -> list[ast.FunctionDef]:
    tree = ast.parse(source)
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]


def _string_literals(node: ast.AST) -> set[str]:
    """All str-valued constants inside a node (docstring excluded)."""
    out: set[str] = set()
    body = getattr(node, 'body', None)
    doc = None
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        doc = body[0].value
    for c in ast.walk(node):
        if isinstance(c, ast.Constant) and isinstance(c.value, str) and c is not doc:
            out.add(c.value)
    return out


def test_eligible_has_no_archetype_name_literal() -> None:
    """P1 litmus: ``eligible()`` dispatches on a Requirement kind, never an arch name."""
    funcs = _func_defs(NAMING.read_text(), 'eligible')
    assert funcs, 'eligible() not found in naming.py'
    for fn in funcs:
        offenders = _string_literals(fn) & ARCH_NAMES
        assert not offenders, f'archetype-name literal(s) in eligible() control flow: {sorted(offenders)}'


def test_degree_axes_have_no_axis_name_literal() -> None:
    """P4 litmus: the degree measurement gates on data, never an axis name."""
    source = DEGREE.read_text()
    for fname in ('compute_degree_axes', 'power_profile'):
        funcs = _func_defs(source, fname)
        assert funcs, f'{fname}() not found in degree.py'
        for fn in funcs:
            offenders = _string_literals(fn) & AXIS_NAMES
            assert not offenders, f'axis-name literal(s) in {fname}() control flow: {sorted(offenders)}'


def test_degree_axes_dict_shape_is_stable() -> None:
    """W1/W2: ``degree_axes`` keeps its SHAPE — one record per axis with the frozen
    key set, and ``color_gated`` present ONLY on color-gated axes. resolve.py reads
    ``cs_count``/``control_cs_gate_met``/``evasion_*`` out of this dict."""
    deck = json.loads((DECKS / 'gauntlet__baral-chief-of-compliance.json').read_text())
    axes = compute_degree_axes(
        deck['cards'],
        sorted({x for c in deck['cards'] for x in (c.get('color_identity') or [])}),
        over_t=0.5,
    )
    assert set(axes) == set(DEGREE_AXES), 'degree_axes axis set drifted'
    for ax, rec in axes.items():
        extra = {'color_gated'} if ax in AXIS_COLOR_GATE else set()
        assert set(rec) == _AXIS_REC_KEYS | extra, f'axis {ax} record shape drifted: {sorted(rec)}'
        assert isinstance(rec['n'], int)
    # color_gated appears on exactly the color-gated axes, nowhere else
    gated = {ax for ax, rec in axes.items() if 'color_gated' in rec}
    assert gated == set(AXIS_COLOR_GATE), f'color_gated axes drifted: {gated}'


def _probe_ctx():
    """A real FactsContext from a committed fixture deck (kess: cs_n == 11)."""
    combos = json.loads((FIX / 'combos.json').read_text())
    deck = json.loads((DECKS / 'gauntlet__kess-dissident-mage.json').read_text())
    return compute_facts(deck, [dict(c) for c in combos])


def test_unregistered_archetype_falls_to_default_bar() -> None:
    """A brand-new archetype with NO data row is gated by the generic default
    (``member_count(3)`` == requires_met) — zero code, zero data needed."""
    assert '__probe__' not in S.build_substantiveness(DEFAULT_CONFIG)
    assert S.requirement_for('__probe__', DEFAULT_CONFIG) is S.DEFAULT_REQUIREMENT
    ctx = _probe_ctx()
    assert S.requirement_for('__probe__', DEFAULT_CONFIG).satisfied({'requires_met': True}, ctx, '__probe__')
    assert not S.requirement_for('__probe__', DEFAULT_CONFIG).satisfied({'requires_met': False}, ctx, '__probe__')


def test_add_archetype_is_a_data_row_only(monkeypatch) -> None:
    """The LITMUS as an executable test: a throwaway archetype becomes fully gated
    by ADDING ONE ``SUBSTANTIVENESS`` data row — no new evaluator, no new branch.

    The generic kind-gate then evaluates it correctly for its declared kind. A
    config overlay (the monkeypatched map) keeps the default engine / oracle clean.
    """
    ctx = _probe_ctx()
    cs_n = ctx.facts['degree_axes']['counterspells']['n']  # 11 for kess

    # --- the ONLY change: a data row declaring the probe's bar (an axis_density
    # kind, reusing the existing counterspells degree reading).
    probe_req = S.Requirement('axis_density', {'axis': 'counterspells', 'min': cs_n})
    base = dict(S.build_substantiveness(DEFAULT_CONFIG))
    base['__probe__'] = probe_req
    monkeypatch.setattr(S, 'build_substantiveness', lambda cfg: base)

    # data lookup (not a name branch) routes the probe to its declared Requirement
    assert S.requirement_for('__probe__', DEFAULT_CONFIG) is probe_req

    # the generic gate evaluates the declared kind: cs_n >= min passes at the count,
    # fails one above it — the param drives the decision, no probe-specific code.
    st = {'requires_met': True}
    assert probe_req.satisfied(st, ctx, '__probe__')
    tighter = S.Requirement('axis_density', {'axis': 'counterspells', 'min': cs_n + 1})
    assert not tighter.satisfied(st, ctx, '__probe__')

    # a different declared kind (presence-of-payoffs) is likewise handled from data:
    # the probe has no routed members, so the noncmd gate fails, the default passes.
    assert not S.Requirement('member_count', {'min': 3, 'noncmd': 2}).satisfied(st, ctx, '__probe__')
    assert S.Requirement('member_count', {'min': 3, 'noncmd': 0}).satisfied(st, ctx, '__probe__')


def test_default_substantiveness_map_is_unpolluted() -> None:
    """The oracle engine carries exactly the two declared special bars — the probe
    machinery never leaks into the default map. Spellslinger is NOT special-cased:
    it falls to the generic ``member_count(3)`` default (the vestigial density gate
    was removed; the over-fire now lives in vocab routing)."""
    assert set(S.build_substantiveness(DEFAULT_CONFIG)) == {'graveyard/reanimator', 'control/permission'}
    assert S.requirement_for('spellslinger', DEFAULT_CONFIG) is S.DEFAULT_REQUIREMENT
