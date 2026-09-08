/** Colored-mana pip counts across mana costs. C = generic-only colorless marker. */
export interface Pips {
  W: number;
  U: number;
  B: number;
  R: number;
  G: number;
  C: number;
}

export interface Stats {
  curve: Record<string, number>; // buckets: '0','1','2','3','4','5','6','7+'
  pips: Pips;
  typeCounts: Record<string, number>;
  landCount: number;
  avgCmc: number;
  total: number;
}

/** Signed field-by-field difference between two Stats (after − before). */
export interface StatsDelta {
  curve: Record<string, number>;
  pips: Pips;
  typeCounts: Record<string, number>;
  landCount: number;
  avgCmc: number;
  total: number;
}

/** Minimal card shape metadata needs. */
export interface MetaCard {
  quantity?: number;
  qty?: number;
  mana_value?: number;
  cmc?: number;
  mana_cost?: string | null;
  type_line: string;
}

export const CURVE_BUCKETS = ['0', '1', '2', '3', '4', '5', '6', '7+'] as const;
const PRIMARY_TYPES = [
  'Creature',
  'Instant',
  'Sorcery',
  'Artifact',
  'Enchantment',
  'Planeswalker',
  'Battle',
  'Land',
] as const;

function qtyOf(c: MetaCard): number {
  return c.qty ?? c.quantity ?? 1;
}

function cmcOf(c: MetaCard): number {
  return c.cmc ?? c.mana_value ?? 0;
}

/** Bucket a mana value into one of the curve buckets. Lands are excluded by caller. */
export function curveBucket(cmc: number): string {
  const n = Math.floor(cmc);
  return n >= 7 ? '7+' : String(n);
}

/**
 * Primary card type — the first matching type in canonical precedence.
 * "Legendary Creature — Kor Scout" → "Creature"; "Artifact — Equipment" → "Artifact".
 */
export function primaryType(typeLine: string): string {
  for (const t of PRIMARY_TYPES) {
    if (typeLine.includes(t)) return t;
  }
  return 'Other';
}

/** Count colored pips in a mana cost string like "{2}{W}{W}". */
export function countPips(manaCost: string | null | undefined): Pips {
  const p: Pips = { W: 0, U: 0, B: 0, R: 0, G: 0, C: 0 };
  if (!manaCost) return p;
  const syms = manaCost.match(/\{[^}]+\}/g) ?? [];
  for (const raw of syms) {
    const s = raw.slice(1, -1); // strip braces
    for (const ch of s) {
      if (ch === 'W' || ch === 'U' || ch === 'B' || ch === 'R' || ch === 'G' || ch === 'C') {
        p[ch] += 1;
      }
    }
  }
  return p;
}

/**
 * Compute aggregate stats over a set of cards, respecting per-card quantity.
 * Curve buckets exclude Lands (curve is a spell-cost distribution). avgCmc is
 * the quantity-weighted mean mana value over non-land cards.
 */
export function stats(cards: MetaCard[]): Stats {
  const curve: Record<string, number> = Object.fromEntries(CURVE_BUCKETS.map((b) => [b, 0]));
  const pips: Pips = { W: 0, U: 0, B: 0, R: 0, G: 0, C: 0 };
  const typeCounts: Record<string, number> = {};
  let landCount = 0;
  let nonLandCmcSum = 0;
  let nonLandQty = 0;
  let total = 0;

  for (const c of cards) {
    const q = qtyOf(c);
    total += q;
    const type = primaryType(c.type_line);
    typeCounts[type] = (typeCounts[type] ?? 0) + q;
    const isLand = /\bLand\b/.test(c.type_line);
    if (isLand) {
      landCount += q;
    } else {
      const cmc = cmcOf(c);
      curve[curveBucket(cmc)] += q;
      nonLandCmcSum += cmc * q;
      nonLandQty += q;
      const cp = countPips(c.mana_cost);
      pips.W += cp.W * q;
      pips.U += cp.U * q;
      pips.B += cp.B * q;
      pips.R += cp.R * q;
      pips.G += cp.G * q;
      pips.C += cp.C * q;
    }
  }

  const avgCmc = nonLandQty === 0 ? 0 : nonLandCmcSum / nonLandQty;
  return { curve, pips, typeCounts, landCount, avgCmc: round2(avgCmc), total };
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

/** Signed difference of every stats field: after − before. */
export function delta(before: Stats, after: Stats): StatsDelta {
  const curve: Record<string, number> = {};
  for (const b of CURVE_BUCKETS) curve[b] = (after.curve[b] ?? 0) - (before.curve[b] ?? 0);

  const pips: Pips = {
    W: after.pips.W - before.pips.W,
    U: after.pips.U - before.pips.U,
    B: after.pips.B - before.pips.B,
    R: after.pips.R - before.pips.R,
    G: after.pips.G - before.pips.G,
    C: after.pips.C - before.pips.C,
  };

  const typeKeys = new Set([...Object.keys(before.typeCounts), ...Object.keys(after.typeCounts)]);
  const typeCounts: Record<string, number> = {};
  for (const k of typeKeys) typeCounts[k] = (after.typeCounts[k] ?? 0) - (before.typeCounts[k] ?? 0);

  return {
    curve,
    pips,
    typeCounts,
    landCount: after.landCount - before.landCount,
    avgCmc: round2(after.avgCmc - before.avgCmc),
    total: after.total - before.total,
  };
}
