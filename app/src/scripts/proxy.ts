/**
 * Proxy Art Picker -- client glue (Phase 4).
 *
 * Module-singleton state; every mutation calls persist() + render() +
 * dispatches `proxy-changed`. Matches the repo's store.ts / main.ts idiom.
 */

import type { AppState, CardEntry, PpArt } from '../lib/proxy/types';
import {
  emptyState,
  addCard,
  removeCard,
  toggleSelection,
  setQuantity,
  setTargetQty,
  setArts,
  importSeed,
  applyRestore,
  signature,
  isDirty,
  sortScryfallLast,
} from '../lib/proxy/state';
import { identify, getPrintings } from '../lib/proxy/scryfall';
import { searchCard, getArts } from '../lib/proxy/pp-client';
import { toCsv, fromCsv } from '../lib/proxy/csv';

// ---------------------------------------------------------------------------
// State + persistence
// ---------------------------------------------------------------------------

const STORAGE_KEY = 'mtg-proxy-picker:v1';

/**
 * Generate a unique local id. `crypto.randomUUID()` only exists in a secure
 * context (HTTPS or localhost); when the app is served over plain http on a
 * LAN/Tailscale IP it is undefined and would throw, so fall back to a manual
 * generator. (Caught by real-browser testing over http://100.x.)
 */
function newLocalId(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
  } catch {
    /* fall through */
  }
  return 'id-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
}

let state: AppState = emptyState();

function loadState(): AppState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return emptyState();
    const parsed = JSON.parse(raw) as AppState;
    if (parsed && parsed.schemaVersion === 1 && Array.isArray(parsed.cards)) {
      return parsed;
    }
    return emptyState();
  } catch {
    return emptyState();
  }
}

let persistTimer: ReturnType<typeof setTimeout> | null = null;
function persist(): void {
  if (persistTimer) clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch {
      /* storage full / unavailable */
    }
  }, 200);
}

function mutate(next: AppState): void {
  state = next;
  persist();
  render();
  document.dispatchEvent(new CustomEvent('proxy-changed'));
}

function mutateCard(next: AppState, localId: string): void {
  state = next;
  persist();
  // When filtering to "only remaining", a newly-chosen card must drop out of the
  // view — a targeted row re-render would leave it visible, so re-render the list.
  if (hideChosen) {
    render();
  } else {
    renderRow(localId);
    renderHeader();
  }
  document.dispatchEvent(new CustomEvent('proxy-changed'));
}

/** When true, the list shows only cards that still have no art selected. */
let hideChosen = false;

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------

const $ = <T extends HTMLElement>(sel: string) => document.querySelector<T>(sel);
const cardListEl = () => $<HTMLDivElement>('#card-list')!;
const counterEl = () => $<HTMLSpanElement>('#counter')!;
const dirtyDotEl = () => $<HTMLSpanElement>('#dirty-dot')!;
const importModal = () => $<HTMLDialogElement>('#import-modal')!;
const exitModal = () => $<HTMLDialogElement>('#exit-modal')!;

// ---------------------------------------------------------------------------
// Header / counter
// ---------------------------------------------------------------------------

function renderHeader(): void {
  const nCards = state.cards.length;
  const nArts = state.cards.reduce(
    (sum, c) => sum + c.selections.reduce((s, sel) => s + sel.quantity, 0),
    0,
  );
  const nUnchosen = state.cards.filter((c) => c.selections.length === 0).length;
  counterEl().textContent =
    `${nCards} card${nCards !== 1 ? 's' : ''} · ` +
    `${nArts} art${nArts !== 1 ? 's' : ''} selected · ` +
    `${nUnchosen} not chosen`;
  const dirty = isDirty(state, state.lastExportSignature);
  dirtyDotEl().classList.toggle('hidden', !dirty);
}

// ---------------------------------------------------------------------------
// Selection summary (for collapsed row)
// ---------------------------------------------------------------------------

function selectionSummary(card: CardEntry): string {
  if (card.selections.length === 0) return '';
  return card.selections
    .map((sel) => {
      if (sel.ppId === '') return `Buy ×${sel.quantity}`;
      const art = card.arts.find((a) => a.ppId === sel.ppId);
      const label = art && art.setName ? art.setName : sel.ppId;
      return `${label} ×${sel.quantity}`;
    })
    .join(', ');
}

// ---------------------------------------------------------------------------
// Art tile rendering
// ---------------------------------------------------------------------------

function renderArtTile(art: PpArt, card: CardEntry): string {
  const sel = card.selections.find((s) => s.ppId === art.ppId);
  const isSelected = !!sel;
  const isSeed =
    card.repScryfallId && art.scryfallId && art.scryfallId === card.repScryfallId;
  const scoreText = art.score != null ? `${art.score.toFixed(1)}%` : '';

  let valorBadge = '';
  if (art.stolenValor === 'low') {
    valorBadge =
      '<span class="inline-block text-xs font-semibold px-1.5 py-0.5 rounded bg-amber-900/60 text-amber-300">caution</span>';
  } else if (art.stolenValor === 'high') {
    valorBadge =
      '<span class="inline-block text-xs font-semibold px-1.5 py-0.5 rounded bg-red-900/60 text-red-300">avoid</span>';
  }

  const ringClass = isSelected
    ? 'ring-2 ring-emerald-400 ring-offset-2 ring-offset-zinc-900'
    : isSeed
      ? 'ring-2 ring-blue-400 ring-offset-2 ring-offset-zinc-900'
      : '';

  const qty = sel ? sel.quantity : 1;
  const isScryfall = art.ppId.startsWith('sf:');

  // Escape ppId for HTML attribute safety (some ids may have special chars)
  const safePpId = art.ppId.replace(/"/g, '&quot;');
  const idLine = isScryfall
    ? '<span class="text-blue-400">via Scryfall (not on printingproxies)</span>'
    : safePpId;

  return `
    <div class="relative rounded-lg overflow-hidden cursor-pointer bg-zinc-800 ${ringClass}
                ${art.kind === 'custom' ? 'pp-custom-art' : 'pp-official-art'}"
         data-pp-id="${safePpId}" data-local-id="${card.localId}">
      <img src="${art.imageUrl}" alt="${art.setName}"
           class="w-full h-auto rounded-t-lg" loading="lazy" />
      <div class="p-2 text-xs space-y-1">
        <div class="flex items-center justify-between gap-1">
          <span class="truncate font-medium">${art.setName}</span>
          ${scoreText ? `<span class="text-zinc-400 shrink-0">${scoreText}</span>` : ''}
        </div>
        <div class="text-zinc-500 font-mono text-[10px] truncate">${idLine}</div>
        ${valorBadge}
        ${isSeed ? '<div class="text-[10px] text-blue-400 font-medium">your seed art</div>' : ''}
        ${
          isSelected
            ? `<div class="flex items-center gap-2 mt-1">
                <span class="text-emerald-400 text-xs font-semibold">&#10003;</span>
                <button data-qty-dec data-pp-id="${safePpId}" data-local-id="${card.localId}"
                        class="w-5 h-5 rounded bg-zinc-700 hover:bg-zinc-600 text-xs font-bold leading-none">&minus;</button>
                <span class="text-xs font-mono w-4 text-center">${qty}</span>
                <button data-qty-inc data-pp-id="${safePpId}" data-local-id="${card.localId}"
                        class="w-5 h-5 rounded bg-zinc-700 hover:bg-zinc-600 text-xs font-bold leading-none">+</button>
              </div>`
            : ''
        }
      </div>
    </div>`;
}

// ---------------------------------------------------------------------------
// Row rendering
// ---------------------------------------------------------------------------

function renderCardRow(card: CardEntry): string {
  const summary = selectionSummary(card);
  const noSelection = card.selections.length === 0;
  const mutedClass = noSelection ? 'opacity-50' : '';
  const artsCount =
    card.artsStatus === 'loaded' || card.artsStatus === 'not-on-pp'
      ? String(card.arts.filter((a) => a.ppId !== '').length)
      : '—';

  const chevron = card.collapsed ? '▶' : '▼';
  // "need N" target control + selected-vs-target status
  const selectedTotal = card.selections.reduce((n, s) => n + s.quantity, 0);
  const met = selectedTotal === card.targetQty;
  const status = card.selections.length
    ? met
      ? '<span class="text-emerald-400" title="target met">&#10003;</span>'
      : `<span class="text-amber-400 font-mono" title="selected ${selectedTotal} of ${card.targetQty}">${selectedTotal}/${card.targetQty}</span>`
    : '';
  const targetControl = `
      <span class="flex items-center gap-1 shrink-0 text-xs text-zinc-400" title="Copies needed (across decks)">
        <span class="text-zinc-500">need</span>
        <button data-target-dec="${card.localId}" class="w-5 h-5 rounded bg-zinc-700 hover:bg-zinc-600 font-bold leading-none">&minus;</button>
        <span class="font-mono w-4 text-center ${card.targetQty > 1 ? 'text-amber-300' : 'text-zinc-200'}">${card.targetQty}</span>
        <button data-target-inc="${card.localId}" class="w-5 h-5 rounded bg-zinc-700 hover:bg-zinc-600 font-bold leading-none">+</button>
        ${status}
      </span>`;
  const collapsed = `
    <div class="flex items-center gap-3 px-4 py-3 bg-zinc-800 rounded-lg border border-zinc-700 ${mutedClass}"
         data-row="${card.localId}">
      <button data-toggle="${card.localId}"
              class="text-zinc-400 hover:text-zinc-200 text-sm shrink-0 w-5 text-center">${chevron}</button>
      <span class="font-medium flex-1 truncate">${card.name}</span>
      ${targetControl}
      <span class="text-xs bg-zinc-700 px-2 py-0.5 rounded text-zinc-300 shrink-0">${artsCount} art${artsCount !== '1' ? 's' : ''}</span>
      <span class="text-xs text-zinc-400 truncate max-w-[220px] ${noSelection ? 'italic' : ''}">
        ${summary || 'not chosen yet'}
      </span>
      <button data-remove="${card.localId}"
              class="text-zinc-500 hover:text-red-400 text-sm shrink-0" title="Remove">&times;</button>
    </div>`;

  if (card.collapsed) {
    return `<div class="mb-2" id="row-${card.localId}">${collapsed}</div>`;
  }

  // Expanded section
  let expandedContent = '';

  if (card.artsStatus === 'loading') {
    expandedContent =
      '<div class="text-sm text-zinc-400 py-4 text-center">Loading arts&hellip;</div>';
  } else if (card.artsStatus === 'error') {
    expandedContent = `
      <div class="text-sm text-red-400 py-4 text-center">
        Failed to load arts.
        <button data-retry="${card.localId}" class="underline hover:text-red-300 ml-1">Retry</button>
      </div>`;
  } else if (card.artsStatus === 'not-on-pp') {
    const buySel = card.selections.find((s) => s.ppId === '');
    const buyQty = buySel ? buySel.quantity : 1;
    expandedContent = `
      <div class="py-4 px-2 text-sm text-zinc-400">
        <p class="mb-2">Not found on printingproxies.com</p>
        <div class="flex items-center gap-3">
          <button data-buy-toggle="${card.localId}"
                  class="rounded px-3 py-1.5 text-sm font-medium ${buySel ? 'bg-emerald-700 hover:bg-emerald-600 text-emerald-100' : 'bg-zinc-700 hover:bg-zinc-600 text-zinc-200'}">
            ${buySel ? 'Remove buy' : 'Add as buy'}
          </button>
          ${
            buySel
              ? `<div class="flex items-center gap-2">
                  <button data-qty-dec data-pp-id="" data-local-id="${card.localId}"
                          class="w-5 h-5 rounded bg-zinc-700 hover:bg-zinc-600 text-xs font-bold leading-none">&minus;</button>
                  <span class="text-xs font-mono w-4 text-center">${buyQty}</span>
                  <button data-qty-inc data-pp-id="" data-local-id="${card.localId}"
                          class="w-5 h-5 rounded bg-zinc-700 hover:bg-zinc-600 text-xs font-bold leading-none">+</button>
                </div>`
              : ''
          }
        </div>
      </div>`;
  } else if (card.artsStatus === 'loaded') {
    const officialArts = card.arts.filter(
      (a) => a.kind === 'official' && a.ppId !== '',
    );
    const customArts = card.arts.filter(
      (a) => a.kind === 'custom' && a.ppId !== '',
    );

    const officialGrid = officialArts.map((a) => renderArtTile(a, card)).join('');

    const customGrid =
      customArts.length > 0
        ? `<div class="pp-custom-section hidden mt-4 pt-3 border-t border-zinc-700"
               data-custom-section="${card.localId}">
             <p class="text-xs text-zinc-500 mb-2 uppercase tracking-wide">Community / Custom</p>
             <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
               ${customArts.map((a) => renderArtTile(a, card)).join('')}
             </div>
           </div>`
        : '';

    expandedContent = `
      <div class="py-3 px-2">
        ${
          customArts.length > 0
            ? `<label class="flex items-center gap-2 text-xs text-zinc-400 mb-3 cursor-pointer">
                <input type="checkbox" data-show-custom="${card.localId}" class="accent-emerald-500" />
                Show community/custom arts (${customArts.length})
              </label>`
            : ''
        }
        <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
          ${officialGrid}
        </div>
        ${customGrid}
      </div>`;
  } else {
    expandedContent =
      '<div class="text-sm text-zinc-400 py-4 text-center">Expand to load arts</div>';
  }

  return `
    <div class="mb-2" id="row-${card.localId}">
      ${collapsed}
      <div class="mt-1 bg-zinc-800/50 rounded-lg border border-zinc-700/50 overflow-hidden">
        ${expandedContent}
      </div>
    </div>`;
}

function renderRow(localId: string): void {
  const card = state.cards.find((c) => c.localId === localId);
  const el = document.getElementById(`row-${localId}`);
  if (!card || !el) {
    render();
    return;
  }
  const tmp = document.createElement('div');
  tmp.innerHTML = renderCardRow(card);
  const newEl = tmp.firstElementChild as HTMLElement;
  el.replaceWith(newEl);
}

function render(): void {
  const list = cardListEl();
  // Scryfall-only picks (no orderable pp art) sink to the bottom of the list.
  const ordered = sortScryfallLast(state.cards);
  const visible = hideChosen
    ? ordered.filter((c) => c.selections.length === 0)
    : ordered;
  if (state.cards.length === 0) {
    list.innerHTML = `
      <div class="text-center py-16 text-zinc-500">
        <p class="text-lg mb-1">No cards yet</p>
        <p class="text-sm">Search for a card above or import a list</p>
      </div>`;
  } else if (visible.length === 0) {
    list.innerHTML = `
      <div class="text-center py-16 text-zinc-500">
        <p class="text-lg mb-1">🎉 Every card has a selection</p>
        <p class="text-sm">Uncheck “Only remaining” to review them, or Export.</p>
      </div>`;
  } else {
    list.innerHTML = visible.map(renderCardRow).join('');
  }
  renderHeader();
}

// ---------------------------------------------------------------------------
// Card expand + lazy art fetch
// ---------------------------------------------------------------------------

/** Lazy-load a card's PP art grid if it hasn't been fetched yet. Shared by
 *  single-row expand and Expand-all. No-op if already loaded/loading. */
async function loadArtsIfNeeded(localId: string): Promise<void> {
  const card = state.cards.find((c) => c.localId === localId);
  if (!card || card.artsStatus !== 'unloaded') return;

  // Keep the existing (e.g. restored) arts under the spinner rather than wiping
  // to [] — so a mid-load export still finds the selected art's known link.
  state = setArts(state, localId, card.arts, 'loading');
  persist();
  renderRow(localId);
  renderHeader();

  try {
    // Try printingproxies first.
    const ppCardId = await searchCard(card.name);
    let arts: PpArt[] = [];
    if (ppCardId !== null) {
      const ppArts = await getArts(ppCardId);
      // Preserve any Scryfall link/cid we already knew for a ppId (e.g. synthesized
      // from a restored CSV) — fresh pp arts don't carry it. Read from `card.arts`
      // captured at entry (state.arts was just set to the same list).
      const prevById = new Map(card.arts.map((a) => [a.ppId, a]));
      arts = ppArts.map((a) => {
        const old = prevById.get(a.ppId);
        return old?.scryfallUri && !a.scryfallUri
          ? { ...a, scryfallCid: old.scryfallCid, scryfallUri: old.scryfallUri, scryfallId: a.scryfallId ?? old.scryfallId }
          : a;
      });
    }

    // Fall back to Scryfall whenever pp yields no usable arts — either no name
    // match (flavor-name reprints / newer UB cards) OR pp has the card but no
    // official prints. These get scryfall-sourced ppIds ('sf:<uuid>') that export
    // with a Scryfall link + blank PP ID (proxy from the Scryfall art, not pp).
    if (arts.length === 0) {
      const prints = await getPrintings(card.name);
      if (prints.length === 0) {
        mutateCard(setArts(state, localId, card.arts, 'not-on-pp'), localId);
        return;
      }
      arts = prints.map((pr) => ({
        ppId: `sf:${pr.scryfallId}`,
        setName: pr.setName,
        score: null,
        imageUrl: pr.imageUrl,
        kind: 'official' as const,
        stolenValor: 'none' as const,
        scryfallId: pr.scryfallId,
        scryfallUri: pr.scryfallUri,
      }));
    }

    // Flip to 'loaded' and show the art grid NOW — do NOT block the drawer on a
    // Scryfall call. (Previously an `await identify()` oracleId backfill ran here
    // before loaded; since Scryfall shares one rate-limit gate, a throttled/hung
    // request wedged the gate and stranded drawers in "loading" forever.)
    mutateCard(setArts(state, localId, arts, 'loaded'), localId);

    // Backfill oracleId in the background (fire-and-forget; never gates the UI).
    const c = state.cards.find((x) => x.localId === localId);
    if (c && c.oracleId === '') {
      void identify(c.name)
        .then((idResult) => {
          if ('name' in idResult && idResult.oracleId) {
            state = {
              ...state,
              cards: state.cards.map((x) =>
                x.localId === localId && x.oracleId === '' ? { ...x, oracleId: idResult.oracleId } : x,
              ),
            };
            persist();
          }
        })
        .catch(() => {
          /* backfill is best-effort */
        });
    }
  } catch {
    mutateCard(setArts(state, localId, [], 'error'), localId);
  }
}

async function expandCard(localId: string): Promise<void> {
  const card = state.cards.find((c) => c.localId === localId);
  if (!card) return;

  const willExpand = card.collapsed;
  state = {
    ...state,
    cards: state.cards.map((c) =>
      c.localId === localId ? { ...c, collapsed: !c.collapsed } : c,
    ),
  };
  persist();
  renderRow(localId);
  renderHeader();
  document.dispatchEvent(new CustomEvent('proxy-changed'));

  if (willExpand) await loadArtsIfNeeded(localId);
}

/** Expand every row and lazy-load any unloaded art grids with bounded
 *  concurrency. getArts no longer does ~40 Scryfall resolves per card (those are
 *  deferred to selection), so a small pool of parallel pp fetches is safe and
 *  well under the WAF's burst threshold. */
async function expandAll(): Promise<void> {
  state = { ...state, cards: state.cards.map((c) => ({ ...c, collapsed: false })) };
  persist();
  render();
  const ids = state.cards.map((c) => c.localId);
  const CONCURRENCY = 5;
  let next = 0;
  const worker = async (): Promise<void> => {
    while (next < ids.length) {
      await loadArtsIfNeeded(ids[next++]);
    }
  };
  await Promise.all(
    Array.from({ length: Math.min(CONCURRENCY, ids.length) }, worker),
  );
}

function collapseAll(): void {
  state = { ...state, cards: state.cards.map((c) => ({ ...c, collapsed: true })) };
  persist();
  render();
}

// ---------------------------------------------------------------------------
// Disambiguation UI
// ---------------------------------------------------------------------------

let disambiguationEl: HTMLDivElement | null = null;

function clearDisambiguation(): void {
  if (disambiguationEl) {
    disambiguationEl.remove();
    disambiguationEl = null;
  }
}

function showDisambiguation(
  items: Array<{
    name: string;
    set: string;
    cn: string;
    scryfallId: string;
    image?: string;
  }>,
): void {
  clearDisambiguation();
  if (items.length === 0) {
    showNotice('No cards matched that search.');
    return;
  }

  disambiguationEl = document.createElement('div');
  disambiguationEl.className = 'max-w-5xl mx-auto px-4 mb-4';
  disambiguationEl.innerHTML = `
    <div class="bg-zinc-800 border border-zinc-700 rounded-lg p-3">
      <div class="flex items-center justify-between mb-2">
        <span class="text-sm text-zinc-400">Multiple matches &mdash; pick one:</span>
        <button data-dismiss-disambig class="text-zinc-500 hover:text-zinc-300 text-sm">&times;</button>
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-2 max-h-64 overflow-y-auto">
        ${items
          .map(
            (item) => `
          <button data-pick-card='${JSON.stringify(item).replace(/'/g, '&#39;')}'
                  class="flex items-center gap-2 p-2 rounded hover:bg-zinc-700 text-left text-sm">
            ${item.image ? `<img src="${item.image}" class="w-8 h-11 rounded object-cover shrink-0" />` : ''}
            <div class="min-w-0">
              <div class="font-medium truncate">${item.name}</div>
              <div class="text-zinc-500 text-xs">${item.set.toUpperCase()} #${item.cn}</div>
            </div>
          </button>`,
          )
          .join('')}
      </div>
    </div>`;

  // Bind clicks on the picker itself — it lives in <main>, OUTSIDE #card-list,
  // so the #card-list delegated handler never sees these clicks.
  disambiguationEl.addEventListener('click', (e) => {
    const target = e.target as HTMLElement;
    if (target.closest('[data-dismiss-disambig]')) {
      clearDisambiguation();
      return;
    }
    const pick = target.closest<HTMLElement>('[data-pick-card]');
    if (pick) {
      void addCardFromDisambiguation(JSON.parse(pick.dataset.pickCard!));
    }
  });

  const main = document.querySelector('main');
  main?.prepend(disambiguationEl);
}

// ---------------------------------------------------------------------------
// Notices
// ---------------------------------------------------------------------------

function showNotice(msg: string, duration = 3000): void {
  const el = document.createElement('div');
  el.className = 'max-w-5xl mx-auto px-4 mb-3';
  el.innerHTML = `<div class="bg-zinc-800 border border-zinc-700 rounded-lg px-4 py-2 text-sm text-zinc-300">${msg}</div>`;
  const main = document.querySelector('main');
  main?.prepend(el);
  setTimeout(() => el.remove(), duration);
}

// ---------------------------------------------------------------------------
// Search / Add card
// ---------------------------------------------------------------------------

async function addCardFromInput(): Promise<void> {
  const input = $<HTMLInputElement>('#search-input')!;
  const val = input.value.trim();
  if (!val) return;

  const btn = $<HTMLButtonElement>('#btn-add')!;
  btn.disabled = true;
  btn.textContent = '...';

  try {
    const result = await identify(val);

    if ('disambiguation' in result) {
      showDisambiguation(result.disambiguation);
    } else {
      clearDisambiguation();
      const entry: CardEntry = {
        localId: newLocalId(),
        name: result.name,
        oracleId: result.oracleId,
        repScryfallId: result.repScryfallId,
        arts: [],
        artsStatus: 'unloaded',
        selections: [],
        collapsed: true,
        targetQty: 1,
      };
      mutate(addCard(state, entry));
      input.value = '';
    }
  } catch (err) {
    showNotice(
      `Search failed: ${err instanceof Error ? err.message : 'unknown error'}`,
    );
  } finally {
    btn.disabled = false;
    btn.textContent = 'Add';
  }
}

async function addCardFromDisambiguation(item: {
  name: string;
  set: string;
  cn: string;
  scryfallId: string;
}): Promise<void> {
  clearDisambiguation();

  try {
    const result = await identify(`${item.set}/${item.cn}`);
    if ('name' in result) {
      const entry: CardEntry = {
        localId: newLocalId(),
        name: result.name,
        oracleId: result.oracleId,
        repScryfallId: result.repScryfallId || item.scryfallId,
        arts: [],
        artsStatus: 'unloaded',
        selections: [],
        collapsed: true,
        targetQty: 1,
      };
      mutate(addCard(state, entry));
      $<HTMLInputElement>('#search-input')!.value = '';
    }
  } catch (err) {
    showNotice(
      `Failed to add card: ${err instanceof Error ? err.message : 'unknown'}`,
    );
  }
}

// ---------------------------------------------------------------------------
// Buy toggle for not-on-PP cards
// ---------------------------------------------------------------------------

function handleBuyToggle(localId: string): void {
  const card = state.cards.find((c) => c.localId === localId);
  if (!card) return;

  const existing = card.selections.find((s) => s.ppId === '');
  if (existing) {
    mutateCard(toggleSelection(state, localId, ''), localId);
  } else {
    let next = state;
    // Add sentinel PpArt if absent
    if (!card.arts.some((a) => a.ppId === '')) {
      // Buy link built inline from the identified printing's UUID (no fetch);
      // scryfall.com/card/<uuid> redirects to the exact printing.
      const scryfallUri = card.repScryfallId
        ? `https://scryfall.com/card/${card.repScryfallId}`
        : '';
      const sentinel: PpArt = {
        ppId: '',
        setName: 'Buy',
        score: null,
        imageUrl: '',
        kind: 'official',
        stolenValor: 'none',
        scryfallUri,
      };
      next = setArts(next, localId, [...card.arts, sentinel], card.artsStatus);
    }
    next = toggleSelection(next, localId, '');
    mutateCard(next, localId);
  }
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

function doExport(): void {
  // Export in the same order the list shows: Scryfall-only picks at the bottom.
  const csv = toCsv({ ...state, cards: sortScryfallLast(state.cards) });
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const date = new Date().toISOString().slice(0, 10);
  a.href = url;
  a.download = `proxy-order-${date}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);

  state = { ...state, lastExportSignature: signature(state) };
  persist();
  render();
  document.dispatchEvent(new CustomEvent('proxy-changed'));
}

// ---------------------------------------------------------------------------
// Import
// ---------------------------------------------------------------------------

let pendingImportAction: (() => void) | null = null;

function doImport(): void {
  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = '.csv,.txt';
  fileInput.addEventListener('change', async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    const text = await file.text();
    const result = fromCsv(text);

    if (result.mode === 'restore') {
      const modal = importModal();
      const p = modal.querySelector('p');
      if (p) {
        p.textContent = `Replace current list with ${result.rows.length} card selection(s) from this file?`;
      }

      pendingImportAction = () => {
        state = applyRestore(result.rows);
        state = { ...state, lastExportSignature: signature(state) };
        persist();
        render();
        document.dispatchEvent(new CustomEvent('proxy-changed'));
        if (result.report.skipped.length > 0) {
          showNotice(
            `Imported. Skipped: ${result.report.skipped.join('; ')}`,
            5000,
          );
        }
      };

      modal.showModal();
    } else {
      // Seed mode
      await handleSeedImport(result.identifiers, result.report);
    }
  });
  fileInput.click();
}

async function handleSeedImport(
  identifiers: string[],
  report: { added: number; skipped: string[] },
): Promise<void> {
  const newCards: CardEntry[] = [];
  const failures: string[] = [];

  for (const id of identifiers) {
    try {
      const result = await identify(id);
      if ('name' in result) {
        newCards.push({
          localId: newLocalId(),
          name: result.name,
          oracleId: result.oracleId,
          repScryfallId: result.repScryfallId,
          arts: [],
          artsStatus: 'unloaded',
          selections: [],
          collapsed: true,
        });
      } else {
        failures.push(id);
      }
    } catch {
      failures.push(id);
    }
  }

  mutate(importSeed(state, newCards));

  const parts: string[] = [`Added ${newCards.length} card(s)`];
  if (failures.length > 0) {
    parts.push(
      `${failures.length} failed: ${failures.slice(0, 5).join(', ')}${failures.length > 5 ? '...' : ''}`,
    );
  }
  if (report.skipped.length > 0) {
    parts.push(`${report.skipped.length} skipped`);
  }
  showNotice(parts.join('. '), 5000);
}

// ---------------------------------------------------------------------------
// Exit modal / unload guard
// ---------------------------------------------------------------------------

function openExitModal(): void {
  exitModal().showModal();
}

// ---------------------------------------------------------------------------
// Event delegation
// ---------------------------------------------------------------------------

function wireEvents(): void {
  // Search
  const searchInput = $<HTMLInputElement>('#search-input')!;
  $<HTMLButtonElement>('#btn-add')!.addEventListener('click', () =>
    addCardFromInput(),
  );
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      addCardFromInput();
    }
  });

  // Import button: if dirty, offer export-first via exit modal
  $<HTMLButtonElement>('#btn-import')!.addEventListener('click', () => {
    if (isDirty(state, state.lastExportSignature)) {
      pendingImportAction = () => doImport();
      openExitModal();
    } else {
      doImport();
    }
  });

  // Expand / collapse all
  $<HTMLButtonElement>('#btn-expand-all')!.addEventListener('click', () => void expandAll());
  $<HTMLButtonElement>('#btn-collapse-all')!.addEventListener('click', collapseAll);

  // "Only remaining" filter — hide cards that already have a selection
  $<HTMLInputElement>('#filter-remaining')!.addEventListener('change', (e) => {
    hideChosen = (e.target as HTMLInputElement).checked;
    render();
  });

  // Export
  $<HTMLButtonElement>('#btn-export')!.addEventListener('click', doExport);

  // Import modal buttons
  $<HTMLButtonElement>('#import-confirm')!.addEventListener('click', () => {
    importModal().close();
    if (pendingImportAction) {
      pendingImportAction();
      pendingImportAction = null;
    }
  });
  $<HTMLButtonElement>('#import-cancel')!.addEventListener('click', () => {
    importModal().close();
    pendingImportAction = null;
  });

  // Exit modal buttons
  $<HTMLButtonElement>('#exit-export')!.addEventListener('click', async () => {
    exitModal().close();
    await doExport();
    // If this modal was opened by an import-when-dirty, proceed with that
    // import after exporting (and clear it so it can't fire again on dismiss).
    if (pendingImportAction) {
      pendingImportAction();
      pendingImportAction = null;
    }
  });
  $<HTMLButtonElement>('#exit-dismiss')!.addEventListener('click', () => {
    exitModal().close();
    // If a pending import was behind the dirty check, run it
    if (pendingImportAction) {
      pendingImportAction();
      pendingImportAction = null;
    }
  });

  // Card list -- delegated click handler
  cardListEl().addEventListener('click', (e) => {
    const target = e.target as HTMLElement;

    // Expand/collapse
    const toggleBtn = target.closest<HTMLElement>('[data-toggle]');
    if (toggleBtn) {
      expandCard(toggleBtn.dataset.toggle!);
      return;
    }

    // Remove card
    const removeBtn = target.closest<HTMLElement>('[data-remove]');
    if (removeBtn) {
      mutate(removeCard(state, removeBtn.dataset.remove!));
      return;
    }

    // Retry arts
    const retryBtn = target.closest<HTMLElement>('[data-retry]');
    if (retryBtn) {
      const localId = retryBtn.dataset.retry!;
      state = setArts(state, localId, [], 'unloaded');
      state = {
        ...state,
        cards: state.cards.map((c) =>
          c.localId === localId ? { ...c, collapsed: true } : c,
        ),
      };
      persist();
      renderRow(localId);
      expandCard(localId);
      return;
    }

    // Buy toggle
    const buyBtn = target.closest<HTMLElement>('[data-buy-toggle]');
    if (buyBtn) {
      handleBuyToggle(buyBtn.dataset.buyToggle!);
      return;
    }

    // Target quantity ("need N") steppers
    const tDec = target.closest<HTMLElement>('[data-target-dec]');
    if (tDec) {
      const localId = tDec.dataset.targetDec!;
      const c = state.cards.find((x) => x.localId === localId);
      if (c) mutateCard(setTargetQty(state, localId, c.targetQty - 1), localId);
      return;
    }
    const tInc = target.closest<HTMLElement>('[data-target-inc]');
    if (tInc) {
      const localId = tInc.dataset.targetInc!;
      const c = state.cards.find((x) => x.localId === localId);
      if (c) mutateCard(setTargetQty(state, localId, c.targetQty + 1), localId);
      return;
    }

    // Quantity decrement
    const decBtn = target.closest<HTMLElement>('[data-qty-dec]');
    if (decBtn) {
      const ppId = decBtn.dataset.ppId!;
      const localId = decBtn.dataset.localId!;
      const card = state.cards.find((c) => c.localId === localId);
      const sel = card?.selections.find((s) => s.ppId === ppId);
      if (sel && sel.quantity > 1) {
        mutateCard(
          setQuantity(state, localId, ppId, sel.quantity - 1),
          localId,
        );
      }
      return;
    }

    // Quantity increment
    const incBtn = target.closest<HTMLElement>('[data-qty-inc]');
    if (incBtn) {
      const ppId = incBtn.dataset.ppId!;
      const localId = incBtn.dataset.localId!;
      const card = state.cards.find((c) => c.localId === localId);
      const sel = card?.selections.find((s) => s.ppId === ppId);
      if (sel) {
        mutateCard(
          setQuantity(state, localId, ppId, sel.quantity + 1),
          localId,
        );
      }
      return;
    }

    // (Disambiguation picker clicks are handled on the picker element itself —
    // it lives outside #card-list, so this delegated handler never sees them.)

    // Art tile toggle (avoid if clicking qty steppers)
    const tile = target.closest<HTMLElement>('[data-pp-id][data-local-id]');
    if (tile && !target.closest('[data-qty-dec],[data-qty-inc]')) {
      const ppId = tile.dataset.ppId!;
      const localId = tile.dataset.localId!;
      if (ppId !== undefined && localId) {
        // Pure client-side toggle — the art's Scryfall link is already on the
        // art record (built inline in getArts from the UUID). No network here.
        mutateCard(toggleSelection(state, localId, ppId), localId);
      }
    }
  });

  // Custom arts toggle (change event)
  cardListEl().addEventListener('change', (e) => {
    const target = e.target as HTMLInputElement;
    if (target.dataset.showCustom) {
      const localId = target.dataset.showCustom;
      const section = document.querySelector<HTMLElement>(
        `[data-custom-section="${localId}"]`,
      );
      if (section) {
        section.classList.toggle('hidden', !target.checked);
      }
    }
  });

  // NOTE: intentionally NO `beforeunload` navigation guard. Every change is
  // autosaved to localStorage and restored on return, so navigating away never
  // loses work — a hard interrupt on every navigation was pure friction. The
  // header dirty-dot is the passive "unexported changes" hint, and the exit
  // modal is still offered on the in-app Import-replace path (which DOES discard).
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

/**
 * Optional agent/URL pre-population (spec: "the app can be instantiated with the
 * card list pre-populated"). `?seed=<url>` fetches a CSV and loads it:
 *   - restore-format CSV (has PP ID/Quantity) → applyRestore (replace)
 *   - bare list → additive seed (each identifier resolved via Scryfall)
 * Applies only when the list is empty, unless `?replace=1` forces it. A seeded
 * load is marked exported (not dirty) so it won't nag the unload guard.
 */
async function maybeSeed(): Promise<void> {
  const params = new URLSearchParams(location.search);
  const seedUrl = params.get('seed');
  if (!seedUrl) return;
  const replace = params.get('replace') === '1';
  const raw = params.get('raw') === '1';
  if (state.cards.length > 0 && !replace) return;
  try {
    const resp = await fetch(seedUrl);
    if (!resp.ok) return;
    const text = await resp.text();
    const parsed = fromCsv(text);
    // replace=1 loads ONLY this set (a clean review), even in additive seed mode.
    if (replace) {
      // Safety net: only back up when a reload would actually DISCARD UNEXPORTED
      // changes (dirty). A clean reload of the same set is not dirty, so it won't
      // nag with a download every time.
      if (isDirty(state, state.lastExportSignature)) {
        try {
          const backup = new Blob([toCsv(state)], { type: 'text/csv;charset=utf-8' });
          const bu = URL.createObjectURL(backup);
          const a = document.createElement('a');
          a.href = bu;
          a.download = `proxy-autobackup-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.csv`;
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(() => URL.revokeObjectURL(bu), 1000);
        } catch {
          /* backup best-effort */
        }
      }
      state = emptyState();
    }
    if (parsed.mode === 'restore') {
      state = applyRestore(parsed.rows);
    } else if (raw) {
      // Fast path: rows are exact card NAMES (+ optional target Quantity). Create
      // cards directly with no per-card Scryfall identify (which throttles on
      // large lists). The pp art fetch on expand keys off the name; oracleId/link
      // are backfilled lazily when a row is expanded.
      const lines = text.split(/\r?\n/).filter((l) => l.trim());
      const header = lines.length && /card name/i.test(lines[0]) ? lines.shift()! : '';
      const qtyCol = header ? header.split(',').findIndex((h) => /quantity|target|qty/i.test(h)) : -1;
      const parseLine = (l: string): string[] => {
        const m = l.match(/("([^"]|"")*"|[^,]*)/g) || [];
        return m.filter((_, i) => i % 2 === 0).map((f) => f.replace(/^"|"$/g, '').replace(/""/g, '"').trim());
      };
      const newCards: CardEntry[] = lines.map((l) => {
        const f = parseLine(l);
        const name = f[0];
        const t = qtyCol >= 0 ? parseInt(f[qtyCol] || '1', 10) : 1;
        return {
          localId: newLocalId(),
          name,
          oracleId: '',
          arts: [],
          artsStatus: 'unloaded' as const,
          selections: [],
          collapsed: true,
          targetQty: Number.isFinite(t) && t > 0 ? t : 1,
        };
      }).filter((c) => c.name);
      state = importSeed(state, newCards);
    } else {
      const newCards: CardEntry[] = [];
      for (const ident of parsed.identifiers) {
        try {
          const r = await identify(ident);
          if ('name' in r) {
            newCards.push({
              localId: newLocalId(),
              name: r.name,
              oracleId: r.oracleId,
              repScryfallId: r.repScryfallId,
              arts: [],
              artsStatus: 'unloaded',
              selections: [],
              collapsed: true,
              targetQty: 1,
            });
          }
        } catch {
          /* skip unresolved identifiers */
        }
      }
      state = importSeed(state, newCards);
    }
    state.lastExportSignature = signature(state);
    persist();
    render();
  } catch {
    /* seed is best-effort; app still works empty */
  }
}

function init(): void {
  state = loadState();
  render();
  wireEvents();
  void maybeSeed();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
