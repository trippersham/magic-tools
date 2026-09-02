// Type-based column grouping for the stacks view. Reconstructed to match the
// client-side `typeGroupOf` in scripts/view.ts (which mirrors this function).

import type { TaggedCard } from './types';

export interface Column {
  group: string;
  cards: TaggedCard[];
}

/** Canonical column order. Commander (via role) leads; Equipment split from Artifact. */
const ORDER = [
  'Commander',
  'Creature',
  'Instant',
  'Sorcery',
  'Artifact',
  'Enchantment',
  'Equipment',
  'Planeswalker',
  'Land',
  'Other',
] as const;

export function groupOf(card: { type_line: string; role?: string | null }): string {
  if (card.role === 'commander') return 'Commander';
  const t = card.type_line ?? '';
  if (/\bLand\b/.test(t)) return 'Land';
  if (/\bEquipment\b/.test(t)) return 'Equipment';
  if (/\bCreature\b/.test(t)) return 'Creature';
  if (/\bPlaneswalker\b/.test(t)) return 'Planeswalker';
  if (/\bInstant\b/.test(t)) return 'Instant';
  if (/\bSorcery\b/.test(t)) return 'Sorcery';
  if (/\bArtifact\b/.test(t)) return 'Artifact';
  if (/\bEnchantment\b/.test(t)) return 'Enchantment';
  return 'Other';
}

/** Group cards into ordered columns, sorted by mana value then name within each. */
export function toColumns(cards: TaggedCard[]): Column[] {
  const buckets = new Map<string, TaggedCard[]>();
  for (const c of cards) {
    const g = groupOf(c);
    if (!buckets.has(g)) buckets.set(g, []);
    buckets.get(g)!.push(c);
  }
  const cols: Column[] = [];
  for (const g of ORDER) {
    const cs = buckets.get(g);
    if (!cs || cs.length === 0) continue;
    cs.sort((a, b) => (a.cmc ?? 0) - (b.cmc ?? 0) || a.name.localeCompare(b.name));
    cols.push({ group: g, cards: cs });
  }
  return cols;
}
