import type { PpArt } from './types';
import { resolvePrinting as defaultResolvePrinting } from './scryfall';

const PP_URL = 'https://printingproxies.com/wp-admin/admin-ajax.php';

const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g;

function isCustom(set: string, setName: string): boolean {
  return (
    set === 'custom' ||
    setName.startsWith('HD -') ||
    setName.startsWith('C -') ||
    setName.startsWith('Custom')
  );
}

function classifyStolenValor(setName: string): PpArt['stolenValor'] {
  // High: World Championship Decks 1997-2004, Pro Tour Collector Set
  if (
    /World Championship Decks (199[7-9]|200[0-4])/.test(setName) ||
    setName === 'Pro Tour Collector Set'
  ) {
    return 'high';
  }

  // Low: Vintage Championship, Legacy Championship, Store Championships,
  // Planeswalker Championship, Judge Gift
  if (
    setName === 'Vintage Championship' ||
    setName === 'Legacy Championship' ||
    setName === 'Store Championships' ||
    setName === 'Planeswalker Championship' ||
    /^Judge Gift/.test(setName)
  ) {
    return 'low';
  }

  return 'none';
}

/**
 * Search for a card by name on printingproxies.
 * Returns the PP card_id for an exact match, or null.
 *
 * CORS-simple: POST with URLSearchParams, no custom headers.
 */
export async function searchCard(
  name: string,
  fetchImpl: typeof fetch = fetch,
): Promise<string | null> {
  const body = new URLSearchParams({
    action: 'cp3_card_search',
    name,
    lang: 'en',
    game: 'mtg',
    quality: '',
  });

  const resp = await fetchImpl(PP_URL, { method: 'POST', body });
  // Guard HTTP errors (e.g. a WAF 403 for a bot-like UA, or a 5xx) so the caller
  // surfaces a retryable error state instead of misreading them as "not on pp".
  if (!resp.ok) throw new Error(`printingproxies search failed: ${resp.status}`);
  const data = await resp.json();

  const normalName = name.replace(/\s+/g, ' ').trim().toLowerCase();
  const match = data.cards?.find(
    (c: { name: string }) =>
      c.name.replace(/\s+/g, ' ').trim().toLowerCase() === normalName,
  );

  return match?.id ?? null;
}

/**
 * Get all arts for a PP card_id.
 * Returns PpArt[] sorted by score DESC, with null-score records dropped.
 *
 * CORS-simple: POST with URLSearchParams, no custom headers.
 */
export async function getArts(
  cardId: string,
  fetchImpl: typeof fetch = fetch,
  deps: {
    resolvePrinting: (
      uuid: string,
      fetchImpl?: typeof fetch,
    ) => Promise<{ scryfallCid: string; scryfallUri: string }>;
  } = { resolvePrinting: defaultResolvePrinting },
): Promise<PpArt[]> {
  const body = new URLSearchParams({
    action: 'cp3_card_prints',
    card_id: cardId,
    game: 'mtg',
    lang: 'en',
    quality: '',
  });

  const resp = await fetchImpl(PP_URL, { method: 'POST', body });
  if (!resp.ok) throw new Error(`printingproxies prints failed: ${resp.status}`);
  const data = await resp.json();

  const prints: Array<{
    id: string;
    set_name: string;
    set: string;
    img_score_result: number | null;
    image_url: string;
    usd_price: string;
  }> = data.prints ?? [];

  const results: PpArt[] = [];

  for (const p of prints) {
    // Drop null-score records
    if (p.img_score_result == null) continue;

    const kind = isCustom(p.set, p.set_name) ? 'custom' : 'official';

    const art: PpArt = {
      ppId: p.id,
      setName: p.set_name,
      score: p.img_score_result,
      imageUrl: p.image_url,
      kind,
      stolenValor: classifyStolenValor(p.set_name),
    };

    // Parse Scryfall UUID from image_url (take last match)
    if (kind === 'official') {
      const uuidMatches = p.image_url.match(UUID_RE);
      if (uuidMatches && uuidMatches.length > 0) {
        const uuid = uuidMatches[uuidMatches.length - 1];
        art.scryfallId = uuid;
        const resolved = await deps.resolvePrinting(uuid);
        art.scryfallCid = resolved.scryfallCid;
        art.scryfallUri = resolved.scryfallUri;
      }
    }

    results.push(art);
  }

  // Sort by score descending
  results.sort((a, b) => (b.score ?? 0) - (a.score ?? 0));

  return results;
}
