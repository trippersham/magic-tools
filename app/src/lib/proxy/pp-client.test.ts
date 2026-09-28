import { describe, it, expect, vi } from 'vitest';
import { searchCard, getArts } from './pp-client';

const PP_URL =
  'https://printingproxies.com/wp-admin/admin-ajax.php';

// ── CORS-simple assertion (REQUIRED) ─────────────────────────────────────────
describe('CORS-simple request shape', () => {
  it('searchCard sends no X-Requested-With and uses URLSearchParams body', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ cards: [] }),
    });

    await searchCard('Underground Sea', mockFetch);

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe(PP_URL);
    expect(init.method).toBe('POST');
    // Body must be URLSearchParams (form-encoded), not JSON
    expect(init.body).toBeInstanceOf(URLSearchParams);
    // No custom headers at all — especially no X-Requested-With
    expect(init.headers).toBeUndefined();
  });

  it('getArts sends no X-Requested-With and uses URLSearchParams body', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ prints: [] }),
    });
    const mockResolve = vi.fn();

    await getArts('123', mockFetch, { resolvePrinting: mockResolve });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe(PP_URL);
    expect(init.method).toBe('POST');
    expect(init.body).toBeInstanceOf(URLSearchParams);
    expect(init.headers).toBeUndefined();
  });
});

// ── HTTP error guard (403/5xx must throw, not be read as "not on pp") ─────────
describe('HTTP error handling', () => {
  it('searchCard throws on a non-ok response (e.g. WAF 403)', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      json: async () => ({}),
    });
    await expect(searchCard('Underground Sea', mockFetch)).rejects.toThrow(/403/);
  });

  it('getArts throws on a non-ok response', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({}),
    });
    await expect(
      getArts('123', mockFetch, { resolvePrinting: vi.fn() }),
    ).rejects.toThrow(/503/);
  });
});

// ── searchCard ────────────────────────────────────────────────────────────────
describe('searchCard', () => {
  it('returns the id of the exact-name match (case-insensitive)', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        cards: [
          { id: '42', name: 'Underground Sea' },
          { id: '99', name: 'Underground River' },
        ],
      }),
    });

    const id = await searchCard('underground sea', mockFetch);
    expect(id).toBe('42');
  });

  it('returns null when no exact match', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        cards: [{ id: '99', name: 'Underground River' }],
      }),
    });

    const id = await searchCard('Underground Sea', mockFetch);
    expect(id).toBeNull();
  });

  it('returns null when cards is empty', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ cards: [] }),
    });

    const id = await searchCard('Nonexistent Card', mockFetch);
    expect(id).toBeNull();
  });

  it('sends correct form params', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ cards: [] }),
    });

    await searchCard('Lightning Bolt', mockFetch);

    const body = mockFetch.mock.calls[0][1].body as URLSearchParams;
    expect(body.get('action')).toBe('cp3_card_search');
    expect(body.get('name')).toBe('Lightning Bolt');
    expect(body.get('lang')).toBe('en');
    expect(body.get('game')).toBe('mtg');
    expect(body.get('quality')).toBe('');
  });
});

// ── getArts ───────────────────────────────────────────────────────────────────
describe('getArts', () => {
  const basePrint = {
    id: 'pp-1',
    set_name: 'Revised Edition',
    set: '3ed',
    img_score_result: 74.8,
    image_url:
      'https://cards.scryfall.io/large/front/a/b/ab12cd34-5678-90ab-cdef-1234567890ab.jpg',
    usd_price: '1.50',
  };

  it('returns official arts sorted by score desc', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        prints: [
          { ...basePrint, id: 'pp-low', img_score_result: 50 },
          { ...basePrint, id: 'pp-high', img_score_result: 90 },
          { ...basePrint, id: 'pp-mid', img_score_result: 70 },
        ],
      }),
    });
    const mockResolve = vi.fn().mockResolvedValue({
      scryfallCid: '3ed/290',
      scryfallUri: 'https://scryfall.com/card/3ed/290/underground-sea',
    });

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(arts.map((a) => a.ppId)).toEqual(['pp-high', 'pp-mid', 'pp-low']);
    expect(arts.map((a) => a.score)).toEqual([90, 70, 50]);
  });

  it('drops records with null score', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        prints: [
          { ...basePrint, id: 'pp-ok', img_score_result: 60 },
          { ...basePrint, id: 'pp-null', img_score_result: null },
        ],
      }),
    });
    const mockResolve = vi.fn().mockResolvedValue({
      scryfallCid: '3ed/290',
      scryfallUri: 'https://scryfall.com/card/3ed/290/underground-sea',
    });

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(arts).toHaveLength(1);
    expect(arts[0].ppId).toBe('pp-ok');
  });

  it('classifies custom arts by set="custom"', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        prints: [
          {
            ...basePrint,
            id: 'pp-custom',
            set: 'custom',
            set_name: 'Custom Arts',
            img_score_result: 80,
            image_url: 'https://example.com/custom-art.png',
          },
        ],
      }),
    });
    const mockResolve = vi.fn();

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(arts[0].kind).toBe('custom');
    // Custom arts should NOT call resolvePrinting
    expect(mockResolve).not.toHaveBeenCalled();
  });

  it('classifies "HD -" prefix as custom', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        prints: [
          {
            ...basePrint,
            id: 'pp-hd',
            set: 'hd',
            set_name: 'HD - Revised Edition',
            img_score_result: 85,
          },
        ],
      }),
    });
    const mockResolve = vi.fn();

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(arts[0].kind).toBe('custom');
  });

  it('classifies "C -" prefix as custom', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        prints: [
          {
            ...basePrint,
            id: 'pp-c',
            set: 'c',
            set_name: 'C - Community Art',
            img_score_result: 70,
          },
        ],
      }),
    });
    const mockResolve = vi.fn();

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(arts[0].kind).toBe('custom');
  });

  it('classifies "Custom" prefix as custom', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        prints: [
          {
            ...basePrint,
            id: 'pp-cust',
            set: 'cst',
            set_name: 'Custom Full Art',
            img_score_result: 60,
          },
        ],
      }),
    });
    const mockResolve = vi.fn();

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(arts[0].kind).toBe('custom');
  });

  it('parses Scryfall UUID from image_url and calls resolvePrinting for official arts', async () => {
    const uuid = 'ab12cd34-5678-90ab-cdef-1234567890ab';
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        prints: [
          {
            ...basePrint,
            image_url: `https://cards.scryfall.io/large/front/a/b/${uuid}.jpg`,
          },
        ],
      }),
    });
    const mockResolve = vi.fn().mockResolvedValue({
      scryfallCid: '3ed/290',
      scryfallUri: 'https://scryfall.com/card/3ed/290/underground-sea',
    });

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(mockResolve).toHaveBeenCalledWith(uuid);
    expect(arts[0].scryfallId).toBe(uuid);
    expect(arts[0].scryfallCid).toBe('3ed/290');
    expect(arts[0].scryfallUri).toBe(
      'https://scryfall.com/card/3ed/290/underground-sea',
    );
  });

  it('handles official art with no UUID in image_url', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        prints: [
          {
            ...basePrint,
            image_url: 'https://example.com/no-uuid-here.png',
          },
        ],
      }),
    });
    const mockResolve = vi.fn();

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(mockResolve).not.toHaveBeenCalled();
    expect(arts[0].scryfallId).toBeUndefined();
  });

  // ── stolenValor tagging ──────────────────────────────────────────────────
  it('tags World Championship Decks 1997-2004 as stolenValor high', async () => {
    const prints = [
      { ...basePrint, id: 'wc97', set_name: 'World Championship Decks 1997' },
      { ...basePrint, id: 'wc04', set_name: 'World Championship Decks 2004' },
    ];
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ prints }),
    });
    const mockResolve = vi.fn().mockResolvedValue({
      scryfallCid: '3ed/290',
      scryfallUri: 'https://scryfall.com/card/3ed/290/x',
    });

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(arts.every((a) => a.stolenValor === 'high')).toBe(true);
  });

  it('tags Pro Tour Collector Set as stolenValor high', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        prints: [
          { ...basePrint, id: 'ptc', set_name: 'Pro Tour Collector Set' },
        ],
      }),
    });
    const mockResolve = vi.fn().mockResolvedValue({
      scryfallCid: '3ed/290',
      scryfallUri: 'https://scryfall.com/card/3ed/290/x',
    });

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(arts[0].stolenValor).toBe('high');
  });

  it('tags Judge Gift as stolenValor low', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        prints: [
          { ...basePrint, id: 'jgp', set_name: 'Judge Gift Cards 2014' },
        ],
      }),
    });
    const mockResolve = vi.fn().mockResolvedValue({
      scryfallCid: '3ed/290',
      scryfallUri: 'https://scryfall.com/card/3ed/290/x',
    });

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(arts[0].stolenValor).toBe('low');
  });

  it('tags Vintage Championship as stolenValor low', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        prints: [
          { ...basePrint, id: 'ovnt', set_name: 'Vintage Championship' },
        ],
      }),
    });
    const mockResolve = vi.fn().mockResolvedValue({
      scryfallCid: '3ed/290',
      scryfallUri: 'https://scryfall.com/card/3ed/290/x',
    });

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(arts[0].stolenValor).toBe('low');
  });

  it('tags Legacy Championship as stolenValor low', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        prints: [
          { ...basePrint, id: 'olgc', set_name: 'Legacy Championship' },
        ],
      }),
    });
    const mockResolve = vi.fn().mockResolvedValue({
      scryfallCid: '3ed/290',
      scryfallUri: 'https://scryfall.com/card/3ed/290/x',
    });

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(arts[0].stolenValor).toBe('low');
  });

  it('tags Store Championships as stolenValor low', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        prints: [
          { ...basePrint, id: 'sch', set_name: 'Store Championships' },
        ],
      }),
    });
    const mockResolve = vi.fn().mockResolvedValue({
      scryfallCid: '3ed/290',
      scryfallUri: 'https://scryfall.com/card/3ed/290/x',
    });

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(arts[0].stolenValor).toBe('low');
  });

  it('tags Planeswalker Championship as stolenValor low', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        prints: [
          { ...basePrint, id: 'pwcs', set_name: 'Planeswalker Championship' },
        ],
      }),
    });
    const mockResolve = vi.fn().mockResolvedValue({
      scryfallCid: '3ed/290',
      scryfallUri: 'https://scryfall.com/card/3ed/290/x',
    });

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(arts[0].stolenValor).toBe('low');
  });

  it('tags normal sets as stolenValor none', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        prints: [basePrint],
      }),
    });
    const mockResolve = vi.fn().mockResolvedValue({
      scryfallCid: '3ed/290',
      scryfallUri: 'https://scryfall.com/card/3ed/290/x',
    });

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(arts[0].stolenValor).toBe('none');
  });

  it('returns empty array for empty prints', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ prints: [] }),
    });
    const mockResolve = vi.fn();

    const arts = await getArts('42', mockFetch, {
      resolvePrinting: mockResolve,
    });

    expect(arts).toEqual([]);
  });
});
