import type { PpArt } from './types';

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

  const resp = await fetchImpl(PP_URL, { method: 'POST', body, signal: AbortSignal.timeout(20000) });
  // Guard HTTP errors (e.g. a WAF 403 for a bot-like UA, or a 5xx) so the caller
  // surfaces a retryable error state instead of misreading them as "not on pp".
  if (!resp.ok) throw new Error(`printingproxies search failed: ${resp.status}`);
  const data = await resp.json();

  // pp search is fuzzy (it returns the closest card even when it doesn't carry
  // the queried one), so we MUST match exactly. Normalize curly→straight
  // apostrophes and whitespace. For double-faced cards, pp lists the full
  // "Front // Back" name, so also accept when one name is the other's front face.
  const norm = (s: string) =>
    s.replace(/’/g, "'").replace(/\s+/g, ' ').trim().toLowerCase();
  const qn = norm(name);
  const match = data.cards?.find((c: { name: string }) => {
    const cn = norm(c.name);
    return cn === qn || cn.startsWith(qn + ' //') || qn.startsWith(cn + ' //');
  });

  return match?.id ?? null;
}

/**
 * Get all arts for a PP card_id.
 * Returns PpArt[] sorted by score DESC, with null-score records dropped.
 *
 * The Scryfall UUID is parsed from each official art's image_url (the pp image
 * IS the Scryfall CDN image, so its filename is the card's Scryfall id). We build
 * the export link inline as `scryfall.com/card/<uuid>`, which 301-redirects to the
 * exact printing — so NO Scryfall API call is ever needed (not on load, select,
 * or export). The tile grid renders from pp's own image_url. Zero Scryfall
 * dependency in the whole browse/select/export path.
 *
 * CORS-simple: POST with URLSearchParams, no custom headers.
 */
export async function getArts(
  cardId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<PpArt[]> {
  const body = new URLSearchParams({
    action: 'cp3_card_prints',
    card_id: cardId,
    game: 'mtg',
    lang: 'en',
    quality: '',
  });

  const resp = await fetchImpl(PP_URL, { method: 'POST', body, signal: AbortSignal.timeout(20000) });
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

    // Parse Scryfall UUID from image_url (take last match) — cheap, no network —
    // and build the canonical link inline: scryfall.com/card/<uuid> 301-redirects
    // to the exact printing. No API call needed for the export link.
    if (kind === 'official') {
      const uuidMatches = p.image_url.match(UUID_RE);
      if (uuidMatches && uuidMatches.length > 0) {
        art.scryfallId = uuidMatches[uuidMatches.length - 1];
        art.scryfallUri = `https://scryfall.com/card/${art.scryfallId}`;
      }
    }

    results.push(art);
  }

  // Sort by score descending
  results.sort((a, b) => (b.score ?? 0) - (a.score ?? 0));

  return results;
}
