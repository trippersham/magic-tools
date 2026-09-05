import { defineConfig } from 'astro/config';
import tailwind from '@tailwindcss/vite';

// Parked placeholder for make-magic.org — a single static page, no backend.
// A face-down card, foretold: exiled face-down, waiting to be cast.
export default defineConfig({
  output: 'static',
  vite: {
    plugins: [tailwind()],
  },
});
