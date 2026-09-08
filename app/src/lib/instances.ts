// Pure instance-expansion / stacking helper (v3.2). Given a name's netted
// quantities (untouched U, dropped R, added A), decide how its copies render:
//
//   • Expanded (unstacked): U neutral tiles + R red tiles + A green tiles.
//   • Stacked: ONE tile with a quantity/delta badge and a border kind.

export type TileKind = 'neutral' | 'drop' | 'add';
export type BadgeKind = 'unchanged' | 'add' | 'drop' | 'mixed';

export interface InstanceCounts {
  untouchedQty: number;
  droppedQty: number;
  addedQty: number;
}

/** Ordered list of tiles for the expanded (unstacked) rendering. */
export function expandTiles(c: InstanceCounts): TileKind[] {
  const tiles: TileKind[] = [];
  for (let i = 0; i < c.untouchedQty; i++) tiles.push('neutral');
  for (let i = 0; i < c.droppedQty; i++) tiles.push('drop');
  for (let i = 0; i < c.addedQty; i++) tiles.push('add');
  return tiles;
}

export interface StackBadge {
  /** The label to show, e.g. "×15", "×4 (+1)", "×3 → ×2 (−1)". */
  text: string;
  /** Border/emphasis kind for the single stacked tile. */
  kind: BadgeKind;
}

/**
 * The badge for the single stacked tile. N = current copies (deckQty = U + R);
 * A added, R dropped. Proposed copies = N − R + A.
 *
 *   unchanged (R=0,A=0) → "×N"
 *   pure add  (A>0,R=0) → "×N (+A)"        green
 *   pure drop (R>0,A=0) → "×N → ×(N−R) (−R)" red
 *   mixed     (R>0,A>0) → "×N → ×(N−R+A)"  amber
 */
export function stackBadge(c: InstanceCounts): StackBadge {
  const n = c.untouchedQty + c.droppedQty; // current copies in the deck
  const r = c.droppedQty;
  const a = c.addedQty;
  if (r === 0 && a === 0) return { text: `×${n}`, kind: 'unchanged' };
  if (a > 0 && r === 0) return { text: `×${n} (+${a})`, kind: 'add' };
  if (r > 0 && a === 0) return { text: `×${n} → ×${n - r} (−${r})`, kind: 'drop' };
  return { text: `×${n} → ×${n - r + a}`, kind: 'mixed' };
}
