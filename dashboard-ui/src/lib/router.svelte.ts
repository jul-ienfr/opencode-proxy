/**
 * Routeur de vues minimal, piloté par le hash (`#stats`, `#logs?from=…`).
 *
 * Pourquoi le hash plutôt que des chemins ? Le dashboard est servi par le
 * proxy FastAPI derrière un seul point d'entrée (`/`) : tout le routage doit
 * rester côté client, sans intervention du back-end.
 *
 * Avantages sur l'ancien système de classes `active` :
 *  - rafraîchir la page garde l'onglet ET les filtres (deep-link) ;
 *  - les filtres vivent dans l'URL, partageables ;
 *  - un seul endroit pilote « quel onglet est visible », ce qui évite que
 *    chaque composant reconstruise sa propre logique d'affichage.
 */

export type TabId =
  | 'stats'
  | 'logs'
  | 'quotas'
  | 'free-models'
  | 'config'
  | 'traffic'
  | 'debug';

/** Ordre de navigation (sert aussi au clavier flèche gauche/droite). */
export const TABS: readonly TabId[] = [
  'stats',
  'logs',
  'quotas',
  'free-models',
  'config',
  'traffic',
  'debug'
];

interface RouteState {
  readonly tab: TabId;
  readonly params: Readonly<Record<string, string>>;
}

function parseHash(hash: string): RouteState {
  const raw = hash.replace(/^#/, '');
  const [pathPart, queryPart] = raw.split('?');
  const tab = (pathPart || 'stats') as TabId;

  const params: Record<string, string> = {};
  if (queryPart) {
    for (const [k, v] of new URLSearchParams(queryPart)) params[k] = v;
  }

  return {
    tab: TABS.includes(tab) ? tab : 'stats',
    params
  };
}

function currentHash(): string {
  return typeof location !== 'undefined' ? location.hash : '#stats';
}

export const route = $state<{ current: RouteState }>({
  current: parseHash(currentHash())
});

function readRoute(): RouteState {
  return parseHash(currentHash());
}

// Synchronisation : hash -> état.
if (typeof window !== 'undefined') {
  addEventListener('hashchange', () => {
    route.current = readRoute();
  });
}

/** Navigue vers un onglet, en conservant (ou remplaçant) les paramètres. */
export function navigate(tab: TabId, params?: Record<string, string | null>, replace = false): void {
  const merged: Record<string, string> = { ...route.current.params };
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v === null || v === '') delete merged[k];
      else merged[k] = String(v);
    }
  }
  const query = new URLSearchParams(merged).toString();
  const hash = `#${tab}${query ? `?${query}` : ''}`;
  if (replace) {
    history.replaceState(null, '', hash);
    route.current = readRoute();
  } else if (location.hash !== hash) {
    location.hash = hash;
  }
}

/** Met à jour les paramètres de l'onglet courant (filtres, page, période). */
export function setParams(params: Record<string, string | null>, replace = true): void {
  navigate(route.current.tab, params, replace);
}

/** Lit un paramètre de l'URL, avec valeur par défaut. */
export function param(name: string, fallback = ''): string {
  return route.current.params[name] ?? fallback;
}
