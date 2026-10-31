import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// Le dashboard est servi par le proxy FastAPI en local. On build dans
// `static/dist/` (monté sous /static/) pour que le back-end serve les
// assets compilés, sans dépendance réseau (uPlot est bundlé, pas de CDN).
export default defineConfig({
  base: '/static/dist/',
  resolve: {
    alias: {
      $lib: fileURLToPath(new URL('./src/lib', import.meta.url))
    }
  },
  plugins: [svelte()],
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
    // Pas de hash de nom : le back-end sert `index.html` depuis la mémoire
    // et les assets sous /static/dist/. Noms stables + revalidation serveur.
    assetsDir: 'assets',
    sourcemap: false,
    target: 'es2022',
    cssCodeSplit: true
  },
  server: {
    port: 5173,
    proxy: {
      // En dev, on relaie l'API sur le proxy qui tourne en local (:4000).
      '/api': 'http://127.0.0.1:4000'
    }
  }
});
