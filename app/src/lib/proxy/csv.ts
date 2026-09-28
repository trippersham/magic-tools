/**
 * CSV import/export for the Proxy Art Picker.
 *
 * Export format (RFC-4180, \r\n line endings):
 *   Card Name,Scryfall link,PP ID,PP confidence,Quantity
 *
 * Buy-row convention: a selection with `ppId === ''` (empty string) represents
 * a card the user wants to buy rather than proxy. In the CSV this produces
 * blank PP ID and PP confidence columns; the Scryfall link comes from the
 * matching PpArt whose ppId is '' (the buy sentinel art).
 */

import type { AppState, PpArt } from './types';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface RestoreRow {
  cardName: string;
  scryfallLink: string;
  ppId: string;
  confidence: number | null;
  quantity: number;
}

export interface ImportReport {
  added: number;
  skipped: string[];
}

type FromCsvResult =
  | { mode: 'restore'; rows: RestoreRow[]; report: ImportReport }
  | { mode: 'seed'; identifiers: string[]; report: ImportReport };

// ---------------------------------------------------------------------------
// RFC-4180 helpers
// ---------------------------------------------------------------------------

/** Quote a field if it contains comma, quote, or newline. */
function quoteField(field: string): string {
  if (/[,"\r\n]/.test(field)) {
    return '"' + field.replace(/"/g, '""') + '"';
  }
  return field;
}

/**
 * Parse a single CSV line honoring RFC-4180 quoted fields.
 * Returns null if the line is incomplete (open quote spanning lines).
 */
function parseCsvRow(line: string): string[] {
  const fields: string[] = [];
  let i = 0;
  while (i <= line.length) {
    if (i === line.length) {
      fields.push('');
      break;
    }
    if (line[i] === '"') {
      // quoted field
      let value = '';
      i++; // skip opening quote
      while (i < line.length) {
        if (line[i] === '"') {
          if (i + 1 < line.length && line[i + 1] === '"') {
            value += '"';
            i += 2;
          } else {
            i++; // skip closing quote
            break;
          }
        } else {
          value += line[i];
          i++;
        }
      }
      fields.push(value);
      // skip comma after quoted field
      if (i < line.length && line[i] === ',') i++;
    } else {
      // unquoted field
      const nextComma = line.indexOf(',', i);
      if (nextComma === -1) {
        fields.push(line.substring(i));
        break;
      } else {
        fields.push(line.substring(i, nextComma));
        i = nextComma + 1;
      }
    }
  }
  return fields;
}

/**
 * Split CSV text into rows, handling quoted fields that contain newlines.
 * Accepts both \r\n and \n.
 */
function splitCsvRows(text: string): string[][] {
  // Normalize line endings for simpler parsing; we'll handle quoted newlines
  const rows: string[][] = [];
  let current = '';
  let inQuotes = false;

  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '"') {
      inQuotes = !inQuotes;
      current += ch;
    } else if (!inQuotes && (ch === '\r' || ch === '\n')) {
      // end of row
      if (ch === '\r' && i + 1 < text.length && text[i + 1] === '\n') {
        i++; // skip \n of \r\n
      }
      if (current.length > 0) {
        rows.push(parseCsvRow(current));
      }
      current = '';
    } else {
      current += ch;
    }
  }
  // trailing content without newline
  if (current.length > 0) {
    rows.push(parseCsvRow(current));
  }
  return rows;
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

const HEADER = ['Card Name', 'Scryfall link', 'PP ID', 'PP confidence', 'Quantity'];

export function toCsv(state: AppState): string {
  const lines: string[] = [HEADER.map(quoteField).join(',')];

  for (const card of state.cards) {
    for (const sel of card.selections) {
      const isBuy = sel.ppId === '';
      let art: PpArt | undefined;

      if (isBuy) {
        // Buy row: find the sentinel art with ppId === ''
        art = card.arts.find((a) => a.ppId === '');
      } else {
        art = card.arts.find((a) => a.ppId === sel.ppId);
      }

      const scryfallLink = art?.scryfallUri ?? '';
      const ppId = sel.ppId;
      const confidence =
        isBuy || art?.score == null
          ? ''
          : (art.score / 100).toFixed(3);
      const quantity = String(sel.quantity);

      const row = [card.name, scryfallLink, ppId, confidence, quantity];
      lines.push(row.map(quoteField).join(','));
    }
  }

  return lines.join('\r\n') + '\r\n';
}

// ---------------------------------------------------------------------------
// Import
// ---------------------------------------------------------------------------

/** Detect whether header indicates restore mode. */
function isRestoreHeader(fields: string[]): boolean {
  const lower = fields.map((f) => f.trim().toLowerCase());
  return lower.includes('pp id') && lower.includes('quantity');
}

/** Find column index by header name (case-insensitive, trimmed). */
function colIndex(header: string[], name: string): number {
  return header.findIndex(
    (h) => h.trim().toLowerCase() === name.toLowerCase(),
  );
}

/**
 * Parse confidence from CSV. Accepts:
 * - '0.748' (decimal, score/100) → 74.8
 * - '74.8%' (legacy percentage) → 74.8
 * - '' or missing → null
 */
function parseConfidence(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === '') return null;

  if (trimmed.endsWith('%')) {
    const n = parseFloat(trimmed.slice(0, -1));
    return isNaN(n) ? null : n;
  }

  const n = parseFloat(trimmed);
  if (isNaN(n)) return null;

  // If the value is <= 1 and doesn't look like a percentage, treat as decimal
  // (score/100 format, e.g. 0.748 → 74.8)
  if (n <= 1) {
    return n * 100;
  }
  // Values > 1 without % — treat as raw score (legacy)
  return n;
}

/** Find a seed-column index: tries Card Name, card, name, or falls back to 0. */
function findSeedColumn(header: string[]): number {
  for (const name of ['card name', 'card', 'name']) {
    const idx = colIndex(header, name);
    if (idx >= 0) return idx;
  }
  return 0;
}

export function fromCsv(text: string): FromCsvResult {
  const allRows = splitCsvRows(text);
  if (allRows.length === 0) {
    return { mode: 'seed', identifiers: [], report: { added: 0, skipped: [] } };
  }

  const headerRow = allRows[0];

  if (isRestoreHeader(headerRow)) {
    // Restore mode
    const iCardName = colIndex(headerRow, 'card name');
    const iScryfallLink = colIndex(headerRow, 'scryfall link');
    const iPpId = colIndex(headerRow, 'pp id');
    const iConfidence = colIndex(headerRow, 'pp confidence');
    const iQuantity = colIndex(headerRow, 'quantity');

    const rows: RestoreRow[] = [];
    const skipped: string[] = [];

    for (let r = 1; r < allRows.length; r++) {
      const fields = allRows[r];
      const cardName = (fields[iCardName] ?? '').trim();
      if (!cardName) {
        skipped.push(`Row ${r + 1}: blank card name`);
        continue;
      }
      const scryfallLink = (fields[iScryfallLink] ?? '').trim();
      const ppId = (fields[iPpId] ?? '').trim();
      const confidence = parseConfidence(fields[iConfidence] ?? '');
      const rawQty = parseInt(fields[iQuantity] ?? '1', 10);
      const quantity = isNaN(rawQty) || rawQty < 1 ? 1 : rawQty;

      rows.push({ cardName, scryfallLink, ppId, confidence, quantity });
    }

    return { mode: 'restore', rows, report: { added: rows.length, skipped } };
  }

  // Seed mode
  const seedCol = findSeedColumn(headerRow);
  const identifiers: string[] = [];
  const skipped: string[] = [];

  // Check if the first row looks like a header or data
  const firstVal = (headerRow[seedCol] ?? '').trim();
  const looksLikeHeader = ['card name', 'card', 'name'].includes(
    firstVal.toLowerCase(),
  );

  const startRow = looksLikeHeader ? 1 : 0;
  for (let r = startRow; r < allRows.length; r++) {
    const fields = allRows[r];
    const val = (fields[seedCol] ?? '').trim();
    if (val) {
      identifiers.push(val);
    } else {
      skipped.push(`Row ${r + 1}: blank`);
    }
  }

  return { mode: 'seed', identifiers, report: { added: identifiers.length, skipped } };
}
