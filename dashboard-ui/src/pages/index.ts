import type { Component } from 'svelte';
import type { TabId } from '$lib/router.svelte';

/**
 * Chargeurs de vues, un par onglet.
 *
 * Chaque `import()` est une frontière de découpage Vite : le bundle initial
 * ne contient que la coquille (header, navigation, toasts). Une page n'est
 * téléchargée et évaluée que lorsqu'on l'ouvre, puis mise en cache pour les
 * passages suivants. C'est le gain de « moins gourmand » par rapport à
 * l'ancien monolithe de 247 Ko chargé d'un bloc.
 */

type PageModule = { default: Component<Record<string, never>> };

export const PAGE_LOADERS: Record<TabId, () => Promise<PageModule>> = {
  stats: () => import('./Stats.svelte'),
  logs: () => import('./Logs.svelte'),
  quotas: () => import('./Quotas.svelte'),
  'free-models': () => import('./FreeModels.svelte'),
  config: () => import('./Config.svelte'),
  traffic: () => import('./Traffic.svelte'),
  debug: () => import('./Debug.svelte')
};
