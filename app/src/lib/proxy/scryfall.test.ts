import { describe, it, expect, vi } from 'vitest';
import { classifyInput, identify, resolvePrinting } from './scryfall';

describe('classifyInput', () => {
  it('classifies a full Scryfall URL', () => {
    const r = classifyInput('https://scryfall.com/card/vma/323/underground-sea');
    expect(r).toEqual({ kind: 'url', set: 'vma', cn: '323' });
  });

  it('classifies a Scryfall URL without trailing slug', () => {
    const r = classifyInput('https://scryfall.com/card/3ed/290');
    expect(r).toEqual({ kind: 'url', set: '3ed', cn: '290' });
  });

  it('classifies a short set/collector string', () => {
    const r = classifyInput('vma/323');
    expect(r).toEqual({ kind: 'setcn', set: 'vma', cn: '323' });
  });

  it('classifies a set/collector with star suffix', () => {
    const r = classifyInput('frc/10');
    expect(r).toEqual({ kind: 'setcn', set: 'frc', cn: '10' });
  });

  it('classifies free text', () => {
    const r = classifyInput('underground sea');
    expect(r).toEqual({ kind: 'text', text: 'underground sea' });
  });

  it('classifies a single word as text', () => {
    const r = classifyInput('Counterspell');
    expect(r).toEqual({ kind: 'text', text: 'Counterspell' });
  });
});

describe('identify', () => {
  it('identifies a card by URL (set/cn)', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        name: 'Underground Sea',
        oracle_id: 'abc-123',
        id: 'sf-uuid-1',
      }),
    });

    const result = await identify(
      'https://scryfall.com/card/vma/323/underground-sea',
      mockFetch,
    );

    expect(mockFetch).toHaveBeenCalledWith(
      'https://api.scryfall.com/cards/vma/323',
    );
    expect(result).toEqual({
      name: 'Underground Sea',
      oracleId: 'abc-123',
      repScryfallId: 'sf-uuid-1',
    });
  });

  it('identifies a card by short set/cn', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        name: 'Timetwister',
        oracle_id: 'ora-tw',
        id: 'sf-tw-1',
      }),
    });

    const result = await identify('vma/3', mockFetch);

    expect(mockFetch).toHaveBeenCalledWith(
      'https://api.scryfall.com/cards/vma/3',
    );
    expect(result).toEqual({
      name: 'Timetwister',
      oracleId: 'ora-tw',
      repScryfallId: 'sf-tw-1',
    });
  });

  it('identifies a single text match', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        total_cards: 1,
        data: [
          {
            name: 'Underground Sea',
            oracle_id: 'abc-123',
            id: 'sf-uuid-1',
          },
        ],
      }),
    });

    const result = await identify('Underground Sea', mockFetch);

    expect(mockFetch).toHaveBeenCalledWith(
      'https://api.scryfall.com/cards/search?q=Underground%20Sea&unique=cards',
    );
    expect(result).toEqual({
      name: 'Underground Sea',
      oracleId: 'abc-123',
    });
  });

  it('returns disambiguation for multiple text matches', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        total_cards: 2,
        data: [
          {
            name: 'Niv-Mizzet, Parun',
            oracle_id: 'ora-nm1',
            id: 'sf-nm1',
            set: 'grn',
            collector_number: '192',
            image_uris: { small: 'https://img/nm1.jpg' },
          },
          {
            name: 'Niv-Mizzet, Dracogenius',
            oracle_id: 'ora-nm2',
            id: 'sf-nm2',
            set: 'rtr',
            collector_number: '183',
            image_uris: { small: 'https://img/nm2.jpg' },
          },
        ],
      }),
    });

    const result = await identify('niv-mizzet', mockFetch);

    expect(result).toEqual({
      disambiguation: [
        {
          name: 'Niv-Mizzet, Parun',
          set: 'grn',
          cn: '192',
          scryfallId: 'sf-nm1',
          image: 'https://img/nm1.jpg',
        },
        {
          name: 'Niv-Mizzet, Dracogenius',
          set: 'rtr',
          cn: '183',
          scryfallId: 'sf-nm2',
          image: 'https://img/nm2.jpg',
        },
      ],
    });
  });

  it('returns empty disambiguation on 404', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ object: 'error', code: 'not_found' }),
    });

    const result = await identify('xyzzy nonsense card', mockFetch);
    expect(result).toEqual({ disambiguation: [] });
  });
});

describe('resolvePrinting', () => {
  it('returns safe empty values on non-ok response', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ object: 'error', code: 'not_found' }),
    });

    const result = await resolvePrinting('bad-uuid', mockFetch);
    expect(result).toEqual({ scryfallCid: '', scryfallUri: '' });
  });

  it('resolves a Scryfall UUID to cid and uri', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        set: 'vma',
        collector_number: '323',
        scryfall_uri:
          'https://scryfall.com/card/vma/323/underground-sea?utm_source=api',
      }),
    });

    const result = await resolvePrinting('sf-uuid-1', mockFetch);

    expect(mockFetch).toHaveBeenCalledWith(
      'https://api.scryfall.com/cards/sf-uuid-1',
    );
    expect(result).toEqual({
      scryfallCid: 'vma/323',
      scryfallUri: 'https://scryfall.com/card/vma/323/underground-sea',
    });
  });
});
