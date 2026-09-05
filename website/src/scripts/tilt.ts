// Tilt the foretold card toward the pointer. Fine-pointer devices only; on
// touch / no-hover the card just keeps its idle float. Honors reduced-motion.
const MAX_TILT = 9; // degrees

function initTilt(): void {
  const card = document.querySelector<HTMLElement>('.card[data-tilt]');
  if (!card) return;

  const finePointer = window.matchMedia('(hover: hover) and (pointer: fine)').matches;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!finePointer || reduced) return;

  const stage = card.closest<HTMLElement>('.card-stage') ?? card;

  const onMove = (event: PointerEvent): void => {
    const rect = card.getBoundingClientRect();
    // -0.5..0.5 relative to card center
    const px = (event.clientX - rect.left) / rect.width - 0.5;
    const py = (event.clientY - rect.top) / rect.height - 0.5;
    card.style.setProperty('--ry', `${(px * MAX_TILT * 2).toFixed(2)}deg`);
    card.style.setProperty('--rx', `${(-py * MAX_TILT * 2).toFixed(2)}deg`);
  };

  const reset = (): void => {
    card.style.setProperty('--rx', '0deg');
    card.style.setProperty('--ry', '0deg');
  };

  stage.addEventListener('pointermove', onMove);
  stage.addEventListener('pointerleave', reset);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initTilt);
} else {
  initTilt();
}
