import { rateLimit } from './rate-limit';

const gate = rateLimit(75);

type ClassifyResult =
  | { kind: 'url'; set: string; cn: string }
  | { kind: 'setcn'; set: string; cn: string }
  | { kind: 'text'; text: string };

const SCRYFALL_URL_RE =
  /^https?:\/\/scryfall\.com\/card\/([^/]+)\/([^/]+)/;

const SETCN_RE = /^([a-z0-9]{2,6})\/(\S+)$/i;

export function classifyInput(s: string): ClassifyResult {
  const urlMatch = s.match(SCRYFALL_URL_RE);
  if (urlMatch) {
    return { kind: 'url', set: urlMatch[1], cn: urlMatch[2] };
  }

  const setCnMatch = s.match(SETCN_RE);
  if (setCnMatch) {
    return { kind: 'setcn', set: setCnMatch[1], cn: setCnMatch[2] };
  }

  return { kind: 'text', text: s };
}

type IdentifyResult =
  | { name: string; oracleId: string; repScryfallId?: string }
  | {
      disambiguation: Array<{
        name: string;
        set: string;
        cn: string;
        scryfallId: string;
        image?: string;
      }>;
    };

export async function identify(
  input: string,
  fetchImpl: typeof fetch = fetch,
): Promise<IdentifyResult> {
  const classified = classifyInput(input);

  if (classified.kind === 'url' || classified.kind === 'setcn') {
    await gate();
    const resp = await fetchImpl(
      `https://api.scryfall.com/cards/${classified.set}/${classified.cn}`,
    );
    if (!resp.ok) return { disambiguation: [] };
    const data = await resp.json();
    return {
      name: data.name,
      oracleId: data.oracle_id,
      repScryfallId: data.id,
    };
  }

  // text search
  await gate();
  const resp = await fetchImpl(
    `https://api.scryfall.com/cards/search?q=${encodeURIComponent(classified.text)}&unique=cards`,
  );
  if (!resp.ok) return { disambiguation: [] };
  const data = await resp.json();

  if (data.total_cards === 1) {
    const card = data.data[0];
    return { name: card.name, oracleId: card.oracle_id };
  }

  return {
    disambiguation: data.data.map(
      (c: {
        name: string;
        set: string;
        collector_number: string;
        id: string;
        image_uris?: { small?: string };
      }) => ({
        name: c.name,
        set: c.set,
        cn: c.collector_number,
        scryfallId: c.id,
        image: c.image_uris?.small,
      }),
    ),
  };
}

export async function resolvePrinting(
  scryfallId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<{ scryfallCid: string; scryfallUri: string }> {
  await gate();
  const resp = await fetchImpl(
    `https://api.scryfall.com/cards/${scryfallId}`,
  );
  if (!resp.ok) return { scryfallCid: '', scryfallUri: '' };
  const data = await resp.json();
  return {
    scryfallCid: `${data.set}/${data.collector_number}`,
    scryfallUri: data.scryfall_uri.split('?')[0],
  };
}
