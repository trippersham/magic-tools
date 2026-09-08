import { defineConfig } from 'astro/config';
import tailwind from '@tailwindcss/vite';

// Static deck-diff viewer — no backend; data is baked into src/data/enriched.json.
export default defineConfig({
  output: 'static',
  vite: {
    plugins: [tailwind()],
  },
});
