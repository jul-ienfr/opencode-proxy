/**
 * Index statique des réglages/sections du dashboard.
 *
 * Volontairement sans backend : la recherche doit rester instantanée et ne
 * jamais exposer de secret. Elle sert à ouvrir le bon sous-onglet, pas à
 * modifier une valeur.
 */

export type SearchGroup = 'free-models' | 'config';

export interface SearchEntry {
  group: SearchGroup;
  tab: string;
  /** Libellé affiché dans les résultats. */
  label: string;
  /** Mots-clés FR/EN, sans accents (normalisés à la recherche). */
  keywords: string[];
}

export const SEARCH_INDEX: readonly SearchEntry[] = [
  {
    group: 'free-models',
    tab: 'overview',
    label: 'Inventaire des modèles gratuits',
    keywords: ['inventory', 'inventaire', 'models', 'modeles', 'free', 'gratuit', 'counts', 'quota']
  },
  {
    group: 'free-models',
    tab: 'egress',
    label: 'Egress & providers',
    keywords: ['egress', 'nordvpn', 'warp', 'proxy', 'proxies', 'mode', 'rotation', 'allocator', 'allocateur', 'providers']
  },
  {
    group: 'free-models',
    tab: 'routing',
    label: 'Routage free',
    keywords: ['routing', 'routage', 'attempts', 'essais', 'strict', 'fallback', 'hedge', 'parallel', 'parallele']
  },
  {
    group: 'free-models',
    tab: 'quality',
    label: 'Qualité & probes',
    keywords: ['quality', 'qualite', 'latency', 'latence', 'probe', 'timeout', 'preselection', 'rotation']
  },
  {
    group: 'free-models',
    tab: 'advanced',
    label: 'Avancé',
    keywords: ['advanced', 'avance', 'https', 'socks5', 'diagnostic', 'circuit breaker', 'watchdog', 'nordvpn api']
  },
  {
    group: 'config',
    tab: 'server',
    label: 'Serveur',
    keywords: ['server', 'serveur', 'port', 'host', 'bind', 'restart', 'redemarrer', 'debug']
  },
  {
    group: 'config',
    tab: 'models',
    label: 'Modèles & routage',
    keywords: ['models', 'modeles', 'mapping', 'routes', 'catalogue', 'claude', 'opus', 'sonnet', 'haiku']
  },
  {
    group: 'config',
    tab: 'keys',
    label: 'Clés & accès',
    keywords: ['keys', 'cles', 'api key', 'token', 'workspace', 'cookie', 'auth']
  },
  {
    group: 'config',
    tab: 'web',
    label: 'Outils web',
    keywords: ['web', 'search', 'recherche', 'fetch', 'duckduckgo', 'web_search', 'web_fetch']
  },
  {
    group: 'config',
    tab: 'maintenance',
    label: 'Maintenance',
    keywords: ['maintenance', 'logs', 'debug', 'backups', 'sauvegardes', 'journaux']
  }
];

function normalize(value: string): string {
  return value
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '');
}

export function searchSettings(query: string, group?: SearchGroup): SearchEntry[] {
  const q = normalize(query).trim();
  if (!q) return [];
  return SEARCH_INDEX.filter((entry) => {
    if (group && entry.group !== group) return false;
    const haystack = normalize(`${entry.label} ${entry.keywords.join(' ')}`);
    return haystack.includes(q);
  }).slice(0, 8);
}
