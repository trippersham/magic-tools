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
  setArts,
  importSeed,
  applyRestore,
  signature,
  isDirty,
} from '../lib/proxy/state';
import { identify, resolvePrinting } from '../lib/proxy/scryfall';
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
  renderRow(localId);
  renderHeader();
  document.dispatchEvent(new CustomEvent('proxy-changed'));
}

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

  // Escape ppId for HTML attribute safety (some ids may have special chars)
  const safePpId = art.ppId.replace(/"/g, '&quot;');

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
        <div class="text-zinc-500 font-mono text-[10px] truncate">${safePpId}</div>
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
  const collapsed = `
    <div class="flex items-center gap-3 px-4 py-3 bg-zinc-800 rounded-lg border border-zinc-700 ${mutedClass}"
         data-row="${card.localId}">
      <button data-toggle="${card.localId}"
              class="text-zinc-400 hover:text-zinc-200 text-sm shrink-0 w-5 text-center">${chevron}</button>
      <span class="font-medium flex-1 truncate">${card.name}</span>
      <span class="text-xs bg-zinc-700 px-2 py-0.5 rounded text-zinc-300 shrink-0">${artsCount} art${artsCount !== '1' ? 's' : ''}</span>
      <span class="text-xs text-zinc-400 truncate max-w-[260px] ${noSelection ? 'italic' : ''}">
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
  if (state.cards.length === 0) {
    list.innerHTML = `
      <div class="text-center py-16 text-zinc-500">
        <p class="text-lg mb-1">No cards yet</p>
        <p class="text-sm">Search for a card above or import a list</p>
      </div>`;
  } else {
    list.innerHTML = state.cards.map(renderCardRow).join('');
  }
  renderHeader();
}

// ---------------------------------------------------------------------------
// Card expand + lazy art fetch
// ---------------------------------------------------------------------------

async function expandCard(localId: string): Promise<void> {
  const card = state.cards.find((c) => c.localId === localId);
  if (!card) return;

  // Toggle collapsed flag
  state = {
    ...state,
    cards: state.cards.map((c) =>
      c.localId === localId ? { ...c, collapsed: !c.collapsed } : c,
    ),
  };
  persist();

  const updated = state.cards.find((c) => c.localId === localId)!;

  if (!updated.collapsed && updated.artsStatus === 'unloaded') {
    // Begin async art fetch
    state = setArts(state, localId, [], 'loading');
    persist();
    renderRow(localId);
    renderHeader();

    try {
      const ppCardId = await searchCard(card.name);
      if (ppCardId === null) {
        mutateCard(setArts(state, localId, [], 'not-on-pp'), localId);
        return;
      }
      const arts = await getArts(ppCardId, fetch, { resolvePrinting });
      let next = setArts(state, localId, arts, 'loaded');

      // Backfill oracleId if empty (from a restore)
      const c = next.cards.find((x) => x.localId === localId);
      if (c && c.oracleId === '') {
        try {
          const idResult = await identify(c.name);
          if ('name' in idResult && idResult.oracleId) {
            next = {
              ...next,
              cards: next.cards.map((x) =>
                x.localId === localId ? { ...x, oracleId: idResult.oracleId } : x,
              ),
            };
          }
        } catch {
          /* backfill is best-effort */
        }
      }

      mutateCard(next, localId);
    } catch {
      mutateCard(setArts(state, localId, [], 'error'), localId);
    }
  } else {
    renderRow(localId);
    renderHeader();
    document.dispatchEvent(new CustomEvent('proxy-changed'));
  }
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

async function handleBuyToggle(localId: string): Promise<void> {
  const card = state.cards.find((c) => c.localId === localId);
  if (!card) return;

  const existing = card.selections.find((s) => s.ppId === '');
  if (existing) {
    mutateCard(toggleSelection(state, localId, ''), localId);
  } else {
    let next = state;
    // Add sentinel PpArt if absent
    if (!card.arts.some((a) => a.ppId === '')) {
      let scryfallUri = '';
      if (card.repScryfallId) {
        try {
          const resolved = await resolvePrinting(card.repScryfallId);
          scryfallUri = resolved.scryfallUri;
        } catch {
          /* best effort */
        }
      }
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
  const csv = toCsv(state);
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
  $<HTMLButtonElement>('#exit-export')!.addEventListener('click', () => {
    exitModal().close();
    doExport();
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

    // Dismiss disambiguation
    const dismissBtn = target.closest<HTMLElement>('[data-dismiss-disambig]');
    if (dismissBtn) {
      clearDisambiguation();
      return;
    }

    // Pick from disambiguation
    const pickBtn = target.closest<HTMLElement>('[data-pick-card]');
    if (pickBtn) {
      const item = JSON.parse(pickBtn.dataset.pickCard!);
      addCardFromDisambiguation(item);
      return;
    }

    // Art tile toggle (avoid if clicking qty steppers)
    const tile = target.closest<HTMLElement>('[data-pp-id][data-local-id]');
    if (tile && !target.closest('[data-qty-dec],[data-qty-inc]')) {
      const ppId = tile.dataset.ppId!;
      const localId = tile.dataset.localId!;
      if (ppId !== undefined && localId) {
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

  // Unload guard
  window.addEventListener('beforeunload', (e) => {
    if (!isDirty(state, state.lastExportSignature)) return;
    openExitModal();
    e.preventDefault();
    e.returnValue = '';
  });
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

function init(): void {
  state = loadState();
  render();
  wireEvents();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
