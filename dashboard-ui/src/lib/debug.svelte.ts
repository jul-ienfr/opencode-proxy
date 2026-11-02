/**
 * État du mode debug + accès au journal débogage.
 *
 * L'onglet « Journal de debug » n'est visible QUE quand le mode est actif :
 * c'est le comportement historique, conservé (état partagé, un seul GET).
 */

import { apiGet, apiPost } from './api';
import { createQuery } from './query.svelte';

export interface DebugStatus {
  enabled: boolean;
}

export const debugQuery = createQuery<DebugStatus>({
  load: (signal) => apiGet<DebugStatus>('/api/debug', signal),
  tags: () => ['config'],
  initial: { enabled: false }
});

export async function setDebugMode(enabled: boolean): Promise<void> {
  await apiPost<DebugStatus>('/api/debug', { enabled });
  await debugQuery.invalidate();
}
