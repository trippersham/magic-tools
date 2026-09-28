/** Proxy Art Picker — data model (design §4). */

export interface PpArt {
  ppId: string;
  setName: string;
  score: number | null; // 0–100 per-art quality score
  imageUrl: string;
  scryfallId?: string;
  scryfallCid?: string; // "set/collector"
  scryfallUri?: string;
  kind: 'official' | 'custom';
  stolenValor: 'none' | 'low' | 'high';
}

export interface Selection {
  ppId: string;
  quantity: number;
}

export interface CardEntry {
  localId: string;
  name: string;
  oracleId: string;
  repScryfallId?: string;
  arts: PpArt[];
  artsStatus: 'unloaded' | 'loading' | 'loaded' | 'error' | 'not-on-pp';
  selections: Selection[];
  collapsed: boolean;
}

export interface AppState {
  schemaVersion: 1;
  cards: CardEntry[];
  lastExportSignature: string | null;
}
