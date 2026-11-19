"""Component 3 — the per-deck, per-layer differ + three-class classifier.

Compares two semantic dumps (``semantic_dump.build_dump`` output) and classifies
every divergence into one of three buckets:

* **benign** — ordering / float-format / presentation-only. A fully-normalized
  dump should never produce one; anything that lands here is itself a
  *normalization gap* in the instrument and is listed explicitly as such.
* **substantive** — an engine gained/lost or its mass/producers/consumers
  changed; an attribute appeared/disappeared; a strategy/archetype key changed;
  an eventeffect edge changed; any archetype_identity-layer field flip.
* **card-universe-change** — divergence attributable to the card pool moving
  (``pool`` sets differ for a deck, or a deck appears/disappears). Under a
  tags-only comparison this bucket MUST be empty; a non-empty bucket is an
  isolation breach (a test-integrity failure, not a drift finding), flagged in
  the report text.

``meta`` (``created_at`` / ``env`` / lake shas / composites) is excluded from
comparison — only ``decks`` is compared.

Exit codes: ``0`` identical · ``1`` only benign · ``2`` substantive · ``3``
card-universe-change non-empty (dominant).

CLI::

    uv run python -m pipeline.transforms.strategy.tests.repro.semantic_diff a.json b.json \
        --report out.md [--json out.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pipeline.transforms.strategy.tests.repro.semantic_dump import LAYERS

BENIGN = 'benign'
SUBSTANTIVE = 'substantive'
CARD_UNIVERSE = 'card-universe-change'
CLASSES: tuple[str, ...] = (BENIGN, SUBSTANTIVE, CARD_UNIVERSE)


# --------------------------------------------------------------------------- #
# Canonicalization (for benign / normalization-gap detection)
# --------------------------------------------------------------------------- #


def _round(obj: Any) -> Any:
    """Recursively round floats to 6 dp WITHOUT reordering anything.

    Reordering is left to ``_canon_layer``, which knows which orderings are
    benign (outer collection order) vs meaningful (directed edge pairs, ranked
    identity rows).
    """
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, dict):
        return {k: _round(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(x) for x in obj]
    return obj


def _sorted_outer(rows: list) -> list:
    """Sort a list's OUTER order only (each row's internal order is preserved)."""
    return sorted((_round(r) for r in rows), key=lambda z: json.dumps(z, sort_keys=True, default=str))


def _canon_layer(layer: str, v: Any) -> Any:
    """Layer-aware canonical form: normalize only the benign orderings.

    Benign orderings (safe to sort): the attribute list, each event's
    producer/consumer membership, the outer order of eventeffect edges and the
    card pool. Meaningful orderings left intact: the direction *inside* an
    eventeffect pair, and the RANK order of ``archetype_identity`` /
    ``ranked_identity`` rows — reordering those is a substantive change.
    """
    if layer == 'attributes':
        return sorted(v)
    if layer == 'events':
        return {e: {'producers': sorted(d['producers']), 'consumers': sorted(d['consumers'])} for e, d in v.items()}
    if layer == 'eventeffects':
        return _sorted_outer(v)  # outer order benign; pair direction preserved
    if layer == 'engines':
        return {
            e: {
                **_round(d),
                'producers': sorted(d['producers']),
                'consumers': sorted(d['consumers']),
                'payoff_archetypes': sorted(d['payoff_archetypes']),
            }
            for e, d in v.items()
        }
    if layer == 'pool':
        return _sorted_outer(v)  # outer order benign; [name, oid] positions fixed
    # archetype_identity (and any scalar layer): round floats, never reorder ranked rows.
    return _round(v)


def classify_layer(layer: str, a: Any, b: Any) -> str | None:
    """Classify one layer's divergence for one deck; ``None`` when identical."""
    if a == b:
        return None
    if _canon_layer(layer, a) == _canon_layer(layer, b):
        return BENIGN  # same content, different benign order/format → normalization gap
    return CARD_UNIVERSE if layer == 'pool' else SUBSTANTIVE


# --------------------------------------------------------------------------- #
# Detail extraction (before→after, compact but complete)
# --------------------------------------------------------------------------- #


def _dict_detail(a: dict, b: dict) -> dict[str, Any]:
    ak, bk = set(a), set(b)
    changed = {k: {'before': a[k], 'after': b[k]} for k in (ak & bk) if a[k] != b[k]}
    return {
        'added': sorted(bk - ak),
        'removed': sorted(ak - bk),
        'changed': changed,
    }


def _list_detail(a: list, b: list) -> dict[str, Any]:
    # order-insensitive membership delta (JSON-keyed so unhashable rows compare)
    def _key(x: Any) -> str:
        return json.dumps(x, sort_keys=True, default=str)

    am = {_key(x): x for x in a}
    bm = {_key(x): x for x in b}
    return {
        'added': [bm[k] for k in sorted(bm.keys() - am.keys())],
        'removed': [am[k] for k in sorted(am.keys() - bm.keys())],
    }


def layer_detail(layer: str, a: Any, b: Any) -> dict[str, Any]:
    """A compact before→after description of a layer divergence."""
    if isinstance(a, dict) and isinstance(b, dict):
        return _dict_detail(a, b)
    if isinstance(a, list) and isinstance(b, list):
        return _list_detail(a, b)
    return {'before': a, 'after': b}


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #


def _empty_counts() -> dict[str, dict[str, int]]:
    return {layer: dict.fromkeys(CLASSES, 0) for layer in LAYERS}


def diff_dumps(a: dict, b: dict) -> dict:
    """Compare two dumps' ``decks`` sections and bucket every divergence."""
    da: dict[str, dict] = a.get('decks', {})
    db: dict[str, dict] = b.get('decks', {})
    only_a = sorted(set(da) - set(db))
    only_b = sorted(set(db) - set(da))
    common = sorted(set(da) & set(db))

    counts = _empty_counts()
    per_deck: dict[str, dict[str, str]] = {}
    substantive_list: list[dict[str, Any]] = []
    benign_list: list[dict[str, Any]] = []
    card_universe_list: list[dict[str, Any]] = []
    moved: set[str] = set()

    # Decks appearing/disappearing are a card-universe change by definition.
    for key in only_a:
        card_universe_list.append({'deck': key, 'layer': '(deck)', 'detail': {'disappeared': True}})
        moved.add(key)
    for key in only_b:
        card_universe_list.append({'deck': key, 'layer': '(deck)', 'detail': {'appeared': True}})
        moved.add(key)

    for key in common:
        ra, rb = da[key], db[key]
        for layer in LAYERS:
            av, bv = ra.get(layer), rb.get(layer)
            cls = classify_layer(layer, av, bv)
            if cls is None:
                continue
            counts[layer][cls] += 1
            per_deck.setdefault(key, {})[layer] = cls
            moved.add(key)
            row = {'deck': key, 'layer': layer, 'class': cls, 'detail': layer_detail(layer, av, bv)}
            if cls == SUBSTANTIVE:
                substantive_list.append(row)
            elif cls == BENIGN:
                benign_list.append(row)
            else:
                card_universe_list.append(row)

    return {
        'total_decks': len(set(da) | set(db)),
        'n_common': len(common),
        'decks_only_in_a': only_a,
        'decks_only_in_b': only_b,
        'decks_moved': len(moved),
        'counts': counts,
        'per_deck': per_deck,
        'substantive_list': substantive_list,
        'benign_list': benign_list,
        'card_universe_list': card_universe_list,
    }


def exit_code(diff: dict) -> int:
    """0 identical · 1 only benign · 2 substantive · 3 card-universe (dominant)."""
    if diff['card_universe_list']:
        return 3
    if diff['substantive_list']:
        return 2
    if diff['benign_list']:
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def _fmt(val: Any) -> str:
    s = json.dumps(val, sort_keys=True, default=str)
    return s if len(s) <= 300 else s[:297] + '...'


def render_report(diff: dict) -> str:
    code = exit_code(diff)
    verdict = {0: 'IDENTICAL', 1: 'BENIGN-ONLY', 2: 'SUBSTANTIVE', 3: 'CARD-UNIVERSE-CHANGE'}[code]
    lines: list[str] = []
    lines.append('# Semantic dump diff')
    lines.append('')
    lines.append(f'**Verdict:** {verdict} (exit {code})')
    lines.append('')
    lines.append(f'- decks moved: **{diff["decks_moved"]} / {diff["total_decks"]}** ({diff["n_common"]} common)')
    lines.append(
        f'- totals: {len(diff["substantive_list"])} substantive · '
        f'{len(diff["benign_list"])} benign · {len(diff["card_universe_list"])} card-universe'
    )
    lines.append('')

    # Summary table: layer x class.
    lines.append('## Counts by layer x class')
    lines.append('')
    lines.append('| layer | benign | substantive | card-universe |')
    lines.append('| --- | --- | --- | --- |')
    counts = diff['counts']
    for layer in LAYERS:
        c = counts[layer]
        lines.append(f'| {layer} | {c[BENIGN]} | {c[SUBSTANTIVE]} | {c[CARD_UNIVERSE]} |')
    lines.append('')

    # Isolation / normalization notes.
    if diff['card_universe_list']:
        lines.append('## Card-universe changes (ISOLATION BREACH under tags-only)')
        lines.append('')
        lines.append(
            '> A non-empty card-universe bucket means the card pool moved: under a '
            'tags-only comparison this is a test-integrity failure (the pinned card '
            'bulk leaked / isolation broke), **not** a drift finding.'
        )
        lines.append('')
        if diff['decks_only_in_a']:
            lines.append(f'- decks only in A (disappeared): {diff["decks_only_in_a"]}')
        if diff['decks_only_in_b']:
            lines.append(f'- decks only in B (appeared): {diff["decks_only_in_b"]}')
        for row in diff['card_universe_list']:
            lines.append(f'- `{row["deck"]}` [{row["layer"]}] {_fmt(row["detail"])}')
        lines.append('')

    if diff['benign_list']:
        lines.append('## Benign divergences (NORMALIZATION GAPS — fix the instrument)')
        lines.append('')
        lines.append(
            '> These differ only by ordering or float-format and should have been '
            'normalized away. Each one is a gap in the dump normalizer, not drift.'
        )
        lines.append('')
        for row in diff['benign_list']:
            lines.append(f'- `{row["deck"]}` [{row["layer"]}] {_fmt(row["detail"])}')
        lines.append('')

    lines.append('## Substantive divergences')
    lines.append('')
    if not diff['substantive_list']:
        lines.append('_none_')
    else:
        lines.append('| deck | layer | before -> after |')
        lines.append('| --- | --- | --- |')
        for row in diff['substantive_list']:
            lines.append(f'| `{row["deck"]}` | {row["layer"]} | {_fmt(row["detail"])} |')
    lines.append('')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Classifying differ for two semantic dumps.')
    ap.add_argument('a', type=Path, help='dump A (e.g. pinned)')
    ap.add_argument('b', type=Path, help='dump B (e.g. fresh)')
    ap.add_argument('--report', type=Path, required=True, help='write the markdown report here')
    ap.add_argument('--json', type=Path, default=None, help='also write the raw diff JSON here')
    args = ap.parse_args(argv)

    a = json.loads(args.a.read_text())
    b = json.loads(args.b.read_text())
    diff = diff_dumps(a, b)
    args.report.write_text(render_report(diff))
    if args.json is not None:
        args.json.write_text(json.dumps(diff, sort_keys=True, indent=2, default=list))
    code = exit_code(diff)
    print(
        f'exit {code}: {diff["decks_moved"]}/{diff["total_decks"]} decks moved · '
        f'{len(diff["substantive_list"])} substantive · {len(diff["benign_list"])} benign · '
        f'{len(diff["card_universe_list"])} card-universe -> {args.report}'
    )
    return code


if __name__ == '__main__':
    raise SystemExit(main())
