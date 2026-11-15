#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Measurement harness for role-B synthesis outputs.

Given role-B engine outputs (one Engine per deck), produce a validation table:
- For each deck: resolved Engine vs archetype_identity + B-test known-correct cases
- Classify: RIGHT / defensible / wrong
- Flag: F4 violations (combo hijacking a dominant archetype), empty engines

Usage (from pipeline/):
    set -a; . ../.env; set +a
    uv run python scripts/roleb_measure.py <outputs.jsonl>

The outputs.jsonl file has one JSON object per line:
    {"slug": "...", "engine": {"strategy": "...", "win_condition": "...", "key_pieces": [...], "confidence": "..."}}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ---- Paths ----
PLAN_ROOT = Path.home() / '.claude/plans/trippersham/magic-tools'
WORK_DIR = PLAN_ROOT / '2026-11-11-strategy-abstraction-consolidation' / 'subagents' / '5.5-roleb-work'
INPUTS_DIR = WORK_DIR / 'inputs'

# ---- Known-correct validation anchors from the B-test (a0afd10f) ----
# These are the ground-truth results from the validated 9-deck B-test sample.
# Each entry: slug -> {expected_strategy_contains, expected_verdict, notes}
KNOWN_CORRECT: dict[str, dict] = {
    'gauntlet__urza-lord-high-artificer': {
        'expected_keywords': ['artifact', 'mana', 'combo'],
        'expected_verdict': 'RIGHT',
        'notes': 'B-test: Urza -> "Artifact Mana-Loop Infinite Engine". Combo IS the engine.',
        'must_not_contain': ['control'],  # component demotion: NOT control deck
    },
    'owned__ozai': {
        'expected_keywords': ['burn'],
        'expected_verdict': 'RIGHT',
        'notes': 'B-test F4: Ozai -> burn, NOT the Aggravated Assault combo. F4 applies.',
        'must_not_contain': ['combo'],  # F4: combo must NOT hijack burn
    },
    'gauntlet__tuvasa-the-sunlit': {
        'expected_keywords': ['enchant'],
        'expected_verdict': 'RIGHT',
        'notes': 'B-test: Tuvasa -> enchantress. Component demotion: NOT combat.',
        'must_not_contain': ['combat'],
    },
    'owned__sokka': {
        'expected_keywords': ['spell'],
        'expected_verdict': 'RIGHT',
        'notes': 'B-test: Sokka -> spellslinger. Component demotion: NOT counters.',
        'must_not_contain': ['counter'],
    },
    'gauntlet__baral-chief-of-compliance': {
        'expected_keywords': ['control', 'counter'],
        'expected_verdict': 'RIGHT',
        'notes': 'B-test: Baral -> non-combo control.',
        'must_not_contain': ['combo'],
    },
    'gauntlet__kess-dissident-mage': {
        'expected_keywords': ['combo'],
        'expected_verdict': 'RIGHT',
        'notes': 'B-test: Kess -> combo (scattered cEDH residue, not dense primary).',
    },
    'gauntlet__krrik-son-of-yawgmoth': {
        'expected_keywords': ['combo'],
        'expected_verdict': 'RIGHT',
        'notes': "B-test: K'rrik -> combo (scattered cEDH residue).",
    },
}

# Component demotion validation: these decks must NOT have the incidental
# component as the LEAD word of their strategy (the first word determines
# the primary identity).  "Artifact Mana-Loop Combo" is fine for Urza
# because "Artifact" leads; "Mana Ramp Engine" would fail.
COMPONENT_DEMOTIONS = {
    'gauntlet__urza-lord-high-artificer': {
        'must_not_lead': 'mana',
        'notes': 'Urza is NOT a "mana" deck -- mana is the mechanism, not the identity',
    },
    'gauntlet__tuvasa-the-sunlit': {
        'must_not_lead': 'combat',
        'notes': 'Tuvasa is NOT a "combat" deck -- combat is incidental',
    },
    'owned__sokka': {
        'must_not_lead': 'counter',
        'notes': 'Sokka is NOT a "counters" deck -- counters is minor',
    },
}


def load_input(slug: str) -> dict | None:
    """Load the per-deck synthesis input."""
    path = INPUTS_DIR / f'{slug}.json'
    if path.exists():
        return json.loads(path.read_text())
    return None


def classify_engine(
    slug: str,
    engine: dict,
    deck_input: dict | None,
) -> dict:
    """Classify a single deck's resolved engine.

    Returns a dict with:
      slug, strategy, verdict, flags[], notes
    """
    strategy = (engine.get('strategy') or '').lower()
    win_con = (engine.get('win_condition') or '').lower()
    key_pieces = engine.get('key_pieces', [])
    confidence = engine.get('confidence', 'LOW')

    flags: list[str] = []
    verdict = 'defensible'  # default
    notes: list[str] = []

    # ---- Flag: empty engine ----
    if not strategy or strategy.strip() == '':
        flags.append('EMPTY_ENGINE')
        verdict = 'wrong'
        notes.append('Engine is empty (never-empty violation R4)')

    # ---- Flag: empty key_pieces ----
    if not key_pieces:
        flags.append('NO_KEY_PIECES')
        notes.append('No key pieces identified')

    # ---- Check against known-correct anchors ----
    anchor = KNOWN_CORRECT.get(slug)
    if anchor:
        # Check expected keywords present in strategy
        kws = anchor.get('expected_keywords', [])
        kw_hits = sum(1 for kw in kws if kw in strategy or kw in win_con)
        if kw_hits == len(kws):
            verdict = 'RIGHT'
            notes.append(f'Matches B-test anchor: {anchor["notes"]}')
        elif kw_hits > 0:
            verdict = 'defensible'
            notes.append(f'Partial B-test match ({kw_hits}/{len(kws)} keywords)')
        else:
            verdict = 'wrong'
            notes.append(f'Misses B-test anchor: expected {kws} in strategy')

        # Check must_not_contain (component demotion / F4)
        banned = anchor.get('must_not_contain', [])
        for b in banned:
            if b in strategy:
                flags.append(f'F4_VIOLATION({b})')
                verdict = 'wrong'
                notes.append(f'Contains banned keyword "{b}" (F4/component-demotion)')

    # ---- Check component demotion (lead-word check) ----
    demotion = COMPONENT_DEMOTIONS.get(slug)
    if demotion:
        bad = demotion['must_not_lead']
        # The demotion fails if the banned component is the LEAD word of the
        # strategy (e.g. "Mana Engine" fails for Urza, but "Artifact Mana-Loop
        # Combo" passes because "Artifact" leads).
        lead_word = strategy.split()[0] if strategy.split() else ''
        if lead_word == bad:
            flags.append(f'COMPONENT_NOT_DEMOTED({bad})')
            notes.append(demotion['notes'])

    # ---- F4 check: detect combo-hijack on decks with dominant archetype ----
    # R1 rule: a winning combo aligned to the dominant archetype is the correct
    # "combo IS the engine" case — NOT a violation.  Flag F4 ONLY when:
    #   (a) strategy names combo but no winning combo exists, OR
    #   (b) the naming resolution's F4 gate blocked the combo override
    #       (meaning the combo's archetype is misaligned with the dominant
    #       archetype — a real hijack)
    # A winning combo alongside a dominant archetype with no F4 block is R1:
    # the combo mechanism IS the archetype mechanism (e.g. Korvold aristocrats-
    # combo, Karador graveyard-combo).
    if deck_input:
        ai = deck_input.get('archetype_identity', [])
        combos = deck_input.get('detected_combos', [])
        win_combos = [c for c in combos if c.get('win')]
        naming = deck_input.get('naming_resolution', {})

        if ai and len(ai) >= 1:
            top_share = ai[0].get('share', 0)
            top_strategy = ai[0].get('strategy', '')

            if top_share >= 0.50 and 'combo' in strategy:
                if not win_combos:
                    # (a) Names combo but no winning combo backs it up
                    flags.append('F4_COMBO_HIJACK_SUSPECT')
                    notes.append(
                        f'Combo in strategy but no winning combo; '
                        f'dominant archetype is {top_strategy} '
                        f'(share {top_share})'
                    )
                elif naming.get('combo_override_f4_blocked'):
                    # (b) The naming layer itself detected the combo's
                    # archetype is misaligned — a real hijack
                    flags.append('F4_COMBO_HIJACK_SUSPECT')
                    notes.append(
                        f'Combo in strategy but naming-layer F4 blocked; '
                        f'dominant archetype is {top_strategy} '
                        f'(share {top_share}) -- combo archetype misaligned'
                    )

    # ---- Key pieces in-deck check (best-effort) ----
    if deck_input and key_pieces:
        commanders = [c['name'] for c in deck_input.get('commanders', [])]
        combo_cards = set()
        for c in deck_input.get('detected_combos', []):
            combo_cards.update(c.get('requires', []))
        known_cards = set(commanders) | combo_cards
        # We only have partial card info (commanders + combo pieces)
        # so we just note but don't flag if key_pieces are from unknown sources.
        # key_pieces can come from the elected engine's implicit members
        # which we don't have full card lists for.
        _ = [kp for kp in key_pieces if kp not in known_cards]

    return {
        'slug': slug,
        'strategy': engine.get('strategy', ''),
        'win_condition': engine.get('win_condition', ''),
        'key_pieces': key_pieces,
        'confidence': confidence,
        'verdict': verdict,
        'flags': flags,
        'notes': '; '.join(notes) if notes else '',
        'is_anchor': slug in KNOWN_CORRECT,
    }


def format_table(results: list[dict]) -> str:
    """Format a human-readable validation table."""
    lines: list[str] = []
    lines.append('=' * 120)
    lines.append('ROLE-B SYNTHESIS VALIDATION TABLE')
    lines.append('=' * 120)
    lines.append('')

    # Summary
    n = len(results)
    right = sum(1 for r in results if r['verdict'] == 'RIGHT')
    defensible = sum(1 for r in results if r['verdict'] == 'defensible')
    wrong = sum(1 for r in results if r['verdict'] == 'wrong')
    f4_flags = sum(1 for r in results if any('F4' in f for f in r['flags']))
    empty = sum(1 for r in results if 'EMPTY_ENGINE' in r['flags'])

    lines.append(f'SUMMARY: {n} decks | {right} RIGHT | {defensible} defensible | {wrong} wrong')
    lines.append(f'FLAGS: {f4_flags} F4 violations | {empty} empty engines')
    lines.append('')

    # Anchor results first
    anchor_results = [r for r in results if r['is_anchor']]
    non_anchor = [r for r in results if not r['is_anchor']]

    if anchor_results:
        lines.append('-' * 80)
        lines.append('KNOWN-CORRECT ANCHORS (B-test validated)')
        lines.append('-' * 80)
        for r in anchor_results:
            emoji = {'RIGHT': '[OK]', 'defensible': '[~~]', 'wrong': '[!!]'}[r['verdict']]
            lines.append(f'  {emoji} {r["slug"]}')
            lines.append(f'      Strategy: {r["strategy"]}')
            lines.append(f'      Verdict:  {r["verdict"]}  Confidence: {r["confidence"]}')
            if r['flags']:
                lines.append(f'      Flags:    {", ".join(r["flags"])}')
            if r['notes']:
                lines.append(f'      Notes:    {r["notes"]}')
            lines.append('')

    if non_anchor:
        lines.append('-' * 80)
        lines.append('OTHER DECKS')
        lines.append('-' * 80)
        for r in non_anchor:
            emoji = {'RIGHT': '[OK]', 'defensible': '[~~]', 'wrong': '[!!]'}[r['verdict']]
            lines.append(f'  {emoji} {r["slug"]}')
            lines.append(f'      Strategy: {r["strategy"]}')
            lines.append(f'      Verdict:  {r["verdict"]}  Confidence: {r["confidence"]}')
            if r['flags']:
                lines.append(f'      Flags:    {", ".join(r["flags"])}')
            lines.append('')

    # Component demotion check
    lines.append('-' * 80)
    lines.append('COMPONENT DEMOTION CHECK (4 expected)')
    lines.append('-' * 80)
    for slug, info in COMPONENT_DEMOTIONS.items():
        matching = [r for r in results if r['slug'] == slug]
        if matching:
            r = matching[0]
            bad = info['must_not_lead']
            lead = r['strategy'].lower().split()[0] if r['strategy'].split() else ''
            passed = lead != bad
            tag = 'PASS' if passed else 'FAIL'
            lines.append(f'  [{tag}] {slug}: "{bad}" does not lead strategy "{r["strategy"]}"')
            lines.append(f'         {info["notes"]}')
        else:
            lines.append(f'  [MISS] {slug}: not in outputs')
    lines.append('')

    return '\n'.join(lines)


def main() -> None:
    if len(sys.argv) < 2:
        print(f'Usage: {sys.argv[0]} <outputs.jsonl>', file=sys.stderr)
        print('', file=sys.stderr)
        print('The outputs.jsonl file should have one JSON object per line:', file=sys.stderr)
        print(
            '  {"slug": "...", "engine": {"strategy": "...", "win_condition": "...", '
            '"key_pieces": [...], "confidence": "..."}}',
            file=sys.stderr,
        )
        sys.exit(1)

    outputs_path = Path(sys.argv[1])
    if not outputs_path.exists():
        print(f'Error: {outputs_path} does not exist', file=sys.stderr)
        sys.exit(1)

    # Parse outputs
    outputs: list[dict] = []
    for line in outputs_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            outputs.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f'Warning: skipping malformed line: {e}', file=sys.stderr)

    # Classify each
    results: list[dict] = []
    for out in outputs:
        slug = out.get('slug', '')
        engine = out.get('engine', {})
        deck_input = load_input(slug)
        result = classify_engine(slug, engine, deck_input)
        results.append(result)

    # Sort: anchors first, then alphabetically
    results.sort(key=lambda r: (not r['is_anchor'], r['slug']))

    # Output
    print(format_table(results))

    # Also write machine-readable results
    results_path = outputs_path.parent / (outputs_path.stem + '_results.json')
    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + '\n')
    print(f'\nMachine-readable results: {results_path}')

    # Exit code: 0 if all anchors RIGHT, 1 otherwise
    anchor_wrong = [r for r in results if r['is_anchor'] and r['verdict'] == 'wrong']
    if anchor_wrong:
        print(f'\n** {len(anchor_wrong)} anchor(s) WRONG -- synthesis needs tuning **')
        sys.exit(1)


if __name__ == '__main__':
    main()
