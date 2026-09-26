import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// `npm run dev` serves the UI with hot reload and forwards /api to a running hub.
const hub = process.env.KIWIBIT_HUB_URL ?? 'http://127.0.0.1:8765';

export default defineConfig({
  plugins: [svelte()],
  server: {
    port: 5173,
    // Keep the browser's Host header: the hub refuses cross-origin writes.
    proxy: { '/api': { target: hub, ws: true, changeOrigin: false } },
  },
  build: { outDir: 'dist', emptyOutDir: true },
});
