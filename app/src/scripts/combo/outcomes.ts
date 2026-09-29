// Pure outcome-bucketing for combo `produces` features.
//
// The make-magic `deck-combos` CLI emits granular, human-authored feature strings
// per combo (each `{name, status, win}`). For the combo graph we collapse those
// into a small, stable set of canonical OUTCOME TYPES so the UI can render a
// handful of outcome nodes/icons rather than dozens of free-text results.
//
// The mapping is intentionally rule-based and extensible: add a case to RULES (or
// a new entry to OUTCOME_TYPES) and every consumer picks it up. Anything that
// matches no rule lands in the safe `other` bucket rather than throwing.
//
// This module is pure (no I/O) so it is reused by both the offline prep step
// (scripts/prep-combos.mjs, via the compiled/ts-run form) and by tests.

/** A single granular feature as emitted by the CLI's `produces[]`. */
export interface ProducesFeature {
  name: string;
  status: string;
  win: boolean;
}

/** A canonical outcome type. `win` marks a bucket that ends the game. */
export interface OutcomeType {
  id: string;
  label: string;
  icon: string;
  win: boolean;
}

/**
 * The canonical outcome-type registry. Order here is the canonical display order
 * used when multiple non-win types tie. `win` is a static property of the bucket
 * (drain is the only game-ending bucket in this domain).
 *
 * Icons/labels are provisional per the plan but finalized here.
 */
export const OUTCOME_TYPES: Record<string, OutcomeType> = {
  drain: { id: 'drain', label: 'Infinite Drain / Opponent Loses', icon: '🩸', win: true },
  draw: { id: 'draw', label: 'Card Draw', icon: '📜', win: false },
  mana: { id: 'mana', label: 'Mana', icon: '💎', win: false },
  tokens: { id: 'tokens', label: 'Tokens / Counters', icon: '🪙', win: false },
  damage: { id: 'damage', label: 'Damage', icon: '🔥', win: false },
  lifegain: { id: 'lifegain', label: 'Lifegain', icon: '❤️', win: false },
  other: { id: 'other', label: 'Other', icon: '✨', win: false },
};

/**
 * Ordered rules: the first matching rule wins for a given feature. Ordering
 * encodes precedence:
 *   1. A WIN feature about an opponent losing (loses the game / lifeloss / drain /
 *      damage-to-opponent) is `drain` — checked first so win-damage/win-lifeloss
 *      never fall through to the non-win `damage`/`lifegain` buckets.
 *   2. card draw / draw triggers -> draw
 *   3. mana -> mana
 *   4. tokens / +1/+1 counters / storm count -> tokens
 *   5. remaining (non-win) damage -> damage
 *   6. remaining lifegain -> lifegain
 * Anything else -> other.
 */
const RULES: { id: string; test: (f: ProducesFeature) => boolean }[] = [
  {
    id: 'drain',
    test: (f) =>
      f.win === true &&
      /(loses? the game|lifeloss|life loss|lose(?:s)? life|drain|damage)/i.test(f.name),
  },
  { id: 'draw', test: (f) => /(card draw|draw trigger)/i.test(f.name) },
  { id: 'mana', test: (f) => /\bmana\b/i.test(f.name) },
  { id: 'tokens', test: (f) => /(token|\+1\/\+1|counter|storm count)/i.test(f.name) },
  { id: 'damage', test: (f) => /damage/i.test(f.name) },
  { id: 'lifegain', test: (f) => /(lifegain|life gain)/i.test(f.name) },
];

/** Map a single feature to its canonical outcome-type id. */
export function bucketFeature(feature: ProducesFeature): string {
  for (const rule of RULES) {
    if (rule.test(feature)) return rule.id;
  }
  return 'other';
}

/**
 * Map a combo's `produces[]` to its deduped canonical outcome types.
 *
 * Order is stable and deterministic: types are collected in first-appearance
 * order, deduped, then partitioned so `win` types come first (they take
 * precedence for display), preserving relative order within each group.
 */
export function outcomeTypesFor(produces: readonly ProducesFeature[]): OutcomeType[] {
  const seen = new Set<string>();
  const ordered: OutcomeType[] = [];
  for (const feature of produces) {
    const id = bucketFeature(feature);
    if (seen.has(id)) continue;
    seen.add(id);
    ordered.push(OUTCOME_TYPES[id]);
  }
  const wins = ordered.filter((t) => t.win);
  const rest = ordered.filter((t) => !t.win);
  return [...wins, ...rest];
}

/** Convenience: just the deduped, ordered outcome-type ids for a combo. */
export function outcomeTypeIdsFor(produces: readonly ProducesFeature[]): string[] {
  return outcomeTypesFor(produces).map((t) => t.id);
}

/** Combo -> outcome types helper (reads `combo.produces`). */
export function outcomeTypesForCombo(combo: { produces: readonly ProducesFeature[] }): OutcomeType[] {
  return outcomeTypesFor(combo.produces);
}
