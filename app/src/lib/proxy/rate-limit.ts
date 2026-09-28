/**
 * Returns a gate function that serializes calls with at least `ms`
 * milliseconds between each resolution.
 */
export function rateLimit(ms: number): () => Promise<void> {
  let last = 0;
  let pending: Promise<void> = Promise.resolve();

  return () => {
    pending = pending.then(
      () =>
        new Promise<void>((resolve) => {
          const now = Date.now();
          const wait = Math.max(0, ms - (now - last));
          setTimeout(() => {
            last = Date.now();
            resolve();
          }, wait);
        }),
    );
    return pending;
  };
}
