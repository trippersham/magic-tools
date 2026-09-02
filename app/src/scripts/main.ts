// Client entry — wires the store, view/density/info-pane, drag-to-tag, and the
// export/reset controls. Loaded as a single module so the pieces share state
// via ES imports (one bundle).

import { initStore, exportCuration, resetToProposed, dismissedCount, restoreDismissed } from './store';
import { initView } from './view';
import { initDrag } from './drag';
import { initMetadata } from './metadataLive';

function download(filename: string, text: string): void {
  const blob = new Blob([text], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function flash(btn: HTMLElement, text: string): void {
  const prev = btn.textContent;
  btn.textContent = text;
  setTimeout(() => {
    btn.textContent = prev;
  }, 1400);
}

function wireControls(): void {
  const exportBtn = document.querySelector<HTMLButtonElement>('[data-export]');
  exportBtn?.addEventListener('click', async () => {
    const json = JSON.stringify(exportCuration(), null, 2);
    let copied = false;
    try {
      await navigator.clipboard.writeText(json);
      copied = true;
    } catch {
      /* clipboard blocked — download still happens */
    }
    download('curation.json', json);
    flash(exportBtn, copied ? 'Copied + downloaded ✓' : 'Downloaded ✓');
  });

  const resetBtn = document.querySelector<HTMLButtonElement>('[data-reset]');
  resetBtn?.addEventListener('click', () => {
    resetToProposed();
    flash(resetBtn, 'Reset ✓');
  });
}

function init(): void {
  initStore();
  initView();
  initMetadata();
  initDrag();
  wireControls();
  // Expose a couple of hooks for Playwright verification + minimal recovery.
  (window as unknown as Record<string, unknown>).__curation = {
    export: exportCuration,
    reset: resetToProposed,
    dismissedCount,
    restoreDismissed,
  };
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
