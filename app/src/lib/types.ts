// Shared data shapes. Reconstructed from the code that consumes them (index.astro,
// Card.astro, MetadataBand.astro, diff.ts, store.ts) after the original was lost.

export type Membership = 'untouched' | 'drop' | 'add' | 'consideration' | 'dismissed';

/** Raw deck card as emitted by `collection get-deck` → deck.json. */
export interface DeckCard {
  name: string;
  quantity: number;
  mana_value: number;
  mana_cost: string | null;
  type_line: string;
  colors: string[];
  color_identity: string[];
  role?: string | null;
}

/** A card after prep.mjs enrichment (image + price), as stored in enriched.json. */
export interface EnrichedCard {
  name: string;
  requested_name?: string;
  qty: number;
  role?: string | null;
  cmc: number;
  mana_cost: string | null;
  type_line: string;
  colors: string[];
  image_normal: string | null;
  scryfall_uri?: string | null;
  usd: number | null;
  reason: string | null;
  cut: string | null;
}

/** An enriched card tagged with its diff membership for rendering. */
export interface TaggedCard extends EnrichedCard {
  membership: Membership;
}

export interface AddEntry {
  name: string;
  /** Copies to add (v3.1). Optional; defaults to 1. */
  qty?: number;
  cut: string;
  reason: string;
}
export interface DropEntry {
  name: string;
  /** Copies to drop (v3.1). Optional; defaults to 1. Clamped to owned copies. */
  qty?: number;
  reason: string;
}
export interface ConsiderationEntry {
  name: string;
  reason: string;
}

/** The authored v2 changeset (objects with reasons) — changeset.json on disk. */
export interface ChangesetV2 {
  schema?: string;
  adds: AddEntry[];
  drops: DropEntry[];
  considerations: ConsiderationEntry[];
}

/**
 * Name-only changeset that the diff math and store key off. `addQty`/`dropQty`
 * carry per-name copy counts (v3.1); a name absent from the map defaults to 1.
 */
export interface Changeset {
  adds: string[];
  drops: string[];
  considerations: string[];
  addQty?: Record<string, number>;
  dropQty?: Record<string, number>;
}

/** The offline build input — enriched.json. `changeset` is name-only. */
export interface EnrichedData {
  deck: EnrichedCard[];
  adds: EnrichedCard[];
  considerations: EnrichedCard[];
  changeset: Changeset;
}
