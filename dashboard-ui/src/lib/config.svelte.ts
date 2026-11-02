/**
 * Configuration du proxy — magasin partagé (singleton).
 *
 * L'ancien code lançait `fetchConfig()` depuis plusieurs endroits
 * (`refreshAll`, changement de langue, onglet Config, rendu des quotas) :
 * chaque appel refaisait un GET /api/config et risquait de se piétiner.
 * Ici : UNE requête réactive, partagée par toutes les vues, réexécutée
 * uniquement quand le tag `config` est invalidé (SSE `models_updated`,
 * sauvegarde réussie, ...).
 */

import { apiGet } from './api';
import { createQuery } from './query.svelte';

export interface RouteTarget {
  model?: string;
}

export interface ModelInfo {
  protocol: string;
  endpoint: string;
  source?: string;
  /** Tier canonique calculé côté serveur (jamais déduit du suffixe -free). */
  tier?: 'free' | 'paid' | 'unknown';
  tier_source?: string;
  route_leg?: 'free' | 'paid' | 'internal' | 'unknown';
  catalog_legs?: string[];
  free_first?: boolean;
  free_target?: string | null;
  route_target?: string | null;
  free_mapping?: string | null;
  free_mapping_active?: boolean;
  route_mismatch?: boolean;
  catalog_mismatch?: boolean;
  [key: string]: unknown;
}

export interface ModelCounts {
  total: number;
  free: number;
  paid: number;
  paid_unproven: number;
  paid_free_fallback: number;
  paid_free_ignored: number;
  unknown: number;
  route_mismatch: number;
  catalog_mismatch: number;
}

export interface CustomRoute {
  match?: string[];
  model?: string;
  enabled?: boolean;
  thinking?: string;
  effort?: string;
}

export interface ApiKey {
  alias?: string;
  /** Masqué côté serveur ; la valeur réelle n'est jamais renvoyée. */
  key?: string;
  go_workspace_id?: string;
  go_auth_cookie?: string;
  enabled?: boolean;
}

export interface ConfigResponse {
  proxy_running?: boolean;
  port?: number;
  host?: string;
  local_ips?: string[];
  routes?: Record<string, RouteTarget>;
  disable_mapping?: boolean;
  routing?: string;
  models?: Record<string, ModelInfo>;
  model_counts?: ModelCounts;
  model_limits?: Record<string, number[]>;
  model_capabilities?: Record<string, string[]>;
  api_keys?: ApiKey[];
  custom_routes?: Record<string, CustomRoute>;
  lang?: string;
  /** Politique de substitution free (feature concurrente ajoutée en cours de session). */
  free_substitution_policy?: 'homonym' | 'family';
  free_model_map?: Record<string, string>;
  free_model_map_effective?: Record<string, string>;
  [key: string]: unknown;
}

export const configQuery = createQuery<ConfigResponse>({
  load: (signal) => apiGet<ConfigResponse>('/api/config', signal),
  tags: () => ['config'],
  initial: {} as ConfigResponse
});

/** Accès réactif : `config().models` se repeint automatiquement. */
export const config = configQuery;

/** Rang d'affichage : gratuit, payant avec repli, puis payant. */
function tierRank(info: ModelInfo | undefined): number {
  if (info?.tier === 'free') return 0;
  if (info?.free_target) return 1;
  return 2;
}

/** Liste triée des identifiants de modèles connus, gratuits d'abord. */
export function modelIds(cfg: ConfigResponse | undefined): string[] {
  if (!cfg?.models) return [];
  return Object.keys(cfg.models).sort((a, b) => {
    const rank = tierRank(cfg.models?.[a]) - tierRank(cfg.models?.[b]);
    return rank || a.localeCompare(b);
  });
}

/** Recharge la configuration (appeler après une sauvegarde réussie). */
export async function reloadConfig(): Promise<void> {
  await configQuery.invalidate();
}
