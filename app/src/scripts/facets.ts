// Faceted dropdown controls (v3.3 / v3.4). Two dropdown-button menus — Group By
// and Render — drive grouping, render mode, density, and the two Stack toggles.
// State persists to its own localStorage key and restores on load. Defaults
// reproduce today's look: type / cards / art / stackDuplicates / stackBasicLands.

import { applyGrouping, setDensity, setStacking } from './view';

type GroupBy = 'type' | 'color' | 'labels';
type RenderMode = 'cards' | 'text';
type Density = 'compact' | 'art' | 'full';

interface FacetState {
  groupBy: GroupBy;
  render: RenderMode;
  density: Density;
  stackDuplicates: boolean;
  stackBasicLands: boolean;
}

const KEY = 'deck-diff:facets';
export const DEFAULTS: FacetState = {
  groupBy: 'type',
  render: 'cards',
  density: 'art',
  stackDuplicates: true,
  stackBasicLands: true,
};

/**
 * Merge persisted (partial) facet state onto the defaults and coerce an invalid
 * groupBy. `labels` is a conditional facet: when the loaded data carries no
 * labels there is no Labels column axis to restore, so a persisted
 * `groupBy: "labels"` falls back to `type` (v5).
 */
export function normalizeFacets(parsed: Partial<FacetState>, hasLabels: boolean): FacetState {
  const merged = { ...DEFAULTS, ...parsed };
  if (merged.groupBy === 'labels' && !hasLabels) merged.groupBy = 'type';
  return merged;
}

let state: FacetState = { ...DEFAULTS };

/** Whether the loaded enriched data has at least one label (from #facets-data). */
function readHasLabels(): boolean {
  try {
    const el = document.getElementById('facets-data');
    if (!el?.textContent) return false;
    return Boolean((JSON.parse(el.textContent) as { hasLabels?: boolean }).hasLabels);
  } catch {
    return false;
  }
}

function load(): void {
  const hasLabels = readHasLabels();
  try {
    const raw = localStorage.getItem(KEY);
    const parsed = raw ? (JSON.parse(raw) as Partial<FacetState>) : {};
    state = normalizeFacets(parsed, hasLabels);
  } catch {
    state = { ...DEFAULTS };
  }
}

function persist(): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    /* storage unavailable — in-memory still works */
  }
}

function cap(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Reflect state onto labels, radio/checkbox inputs, and cards-only visibility. */
function syncControls(): void {
  const groupLabel = document.querySelector<HTMLElement>('[data-dropdown-label="group"]');
  if (groupLabel) groupLabel.textContent = `Group By: ${cap(state.groupBy)}`;
  const renderLabel = document.querySelector<HTMLElement>('[data-dropdown-label="render"]');
  if (renderLabel) {
    renderLabel.textContent =
      state.render === 'text' ? 'Render: Text' : `Render: Cards · ${cap(state.density)}`;
  }

  const setRadio = (name: string, value: string) => {
    document
      .querySelectorAll<HTMLInputElement>(`input[name="${name}"]`)
      .forEach((el) => (el.checked = el.value === value));
  };
  setRadio('groupBy', state.groupBy);
  setRadio('mode', state.render);
  setRadio('density', state.density);

  const dup = document.querySelector<HTMLInputElement>('input[name="stackDuplicates"]');
  if (dup) dup.checked = state.stackDuplicates;
  const basics = document.querySelector<HTMLInputElement>('input[name="stackBasicLands"]');
  if (basics) basics.checked = state.stackBasicLands;

  // Hide the Cards-only section when Mode = Text.
  document.querySelectorAll<HTMLElement>('[data-cards-only]').forEach((el) => {
    el.hidden = state.render === 'text';
  });
}

/** Push the current facet state into the render/view layer. */
function apply(): void {
  document.documentElement.dataset.mode = state.render === 'text' ? 'text' : 'image';
  applyGrouping(state.groupBy);
  setDensity(state.density);
  setStacking(state.stackDuplicates, state.stackBasicLands);
}

function commit(): void {
  persist();
  syncControls();
  apply();
}

/* ------------------------------------------------------------------ *
 * Dropdown open/close — one panel at a time; click-outside + Esc close.
 * ------------------------------------------------------------------ */

function closeAll(except?: HTMLElement): void {
  document.querySelectorAll<HTMLElement>('[data-dropdown]').forEach((dd) => {
    if (dd === except) return;
    dd.querySelector<HTMLElement>('[data-dropdown-panel]')!.hidden = true;
    dd.querySelector<HTMLElement>('[data-dropdown-btn]')!.setAttribute('aria-expanded', 'false');
  });
}

function toggleDropdown(dd: HTMLElement): void {
  const panel = dd.querySelector<HTMLElement>('[data-dropdown-panel]')!;
  const btn = dd.querySelector<HTMLElement>('[data-dropdown-btn]')!;
  const willOpen = panel.hidden;
  closeAll(dd);
  panel.hidden = !willOpen;
  btn.setAttribute('aria-expanded', String(willOpen));
  if (willOpen) {
    panel.querySelector<HTMLInputElement>('input')?.focus();
  }
}

function wireDropdowns(): void {
  document.querySelectorAll<HTMLElement>('[data-dropdown]').forEach((dd) => {
    const btn = dd.querySelector<HTMLElement>('[data-dropdown-btn]')!;
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleDropdown(dd);
    });
    // Keep clicks inside the panel from bubbling to the document close handler.
    dd.querySelector<HTMLElement>('[data-dropdown-panel]')!.addEventListener('click', (e) => {
      e.stopPropagation();
    });
  });

  document.addEventListener('click', () => closeAll());
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeAll();
  });
}

function wireInputs(): void {
  document.querySelectorAll<HTMLInputElement>('input[name="groupBy"]').forEach((el) =>
    el.addEventListener('change', () => {
      if (el.checked) {
        state.groupBy = el.value as GroupBy;
        commit();
      }
    }),
  );
  document.querySelectorAll<HTMLInputElement>('input[name="mode"]').forEach((el) =>
    el.addEventListener('change', () => {
      if (el.checked) {
        state.render = el.value as RenderMode;
        commit();
      }
    }),
  );
  document.querySelectorAll<HTMLInputElement>('input[name="density"]').forEach((el) =>
    el.addEventListener('change', () => {
      if (el.checked) {
        state.density = el.value as Density;
        commit();
      }
    }),
  );
  const dup = document.querySelector<HTMLInputElement>('input[name="stackDuplicates"]');
  dup?.addEventListener('change', () => {
    state.stackDuplicates = dup.checked;
    commit();
  });
  const basics = document.querySelector<HTMLInputElement>('input[name="stackBasicLands"]');
  basics?.addEventListener('change', () => {
    state.stackBasicLands = basics.checked;
    commit();
  });
}

export function initFacets(): void {
  load();
  wireDropdowns();
  wireInputs();
  syncControls();
  apply();
}
