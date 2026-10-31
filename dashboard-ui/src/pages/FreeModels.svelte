<script lang="ts">
  /**
   * Page « Modèles Gratuits » : inventaire des modèles réellement gratuits,
   * puis statut temps réel, politique de substitution payant → gratuit,
   * routage VPN et panneaux de configuration.
   *
   * L'inventaire distingue explicitement « détecté » (FREE_MODELS), « routé »
   * (présent dans MODELS) et « servi » (succès observé) : un id découvert mais
   * absent du routage n'est pas utilisable par un client.
   */

  import { apiGet } from '$lib/api';
  import { t } from '$lib/i18n.svelte';
  import { createQuery } from '$lib/query.svelte';
  import { navigate } from '$lib/router.svelte';
  import Card from '$lib/components/Card.svelte';
  import Empty from '$lib/components/Empty.svelte';
  import VpnStatus from './vpn/VpnStatus.svelte';
  import VpnRouting from './vpn/VpnRouting.svelte';
  import VpnServers from './vpn/VpnServers.svelte';
  import VpnAdvanced from './vpn/VpnAdvanced.svelte';

  interface FreeModelRow {
    id: string;
    detected?: boolean;
    routed?: boolean;
    tier?: string;
    tier_source?: string;
    route_leg?: string;
    route_target?: string | null;
    status?: string;
    mapped_from?: string[];
    cooldown?: { retry_in_s?: number; scope?: string } | null;
  }

  interface FreeModelsResponse {
    models?: FreeModelRow[];
    counts?: Record<string, number>;
    policy?: string;
  }

  const inventoryQ = createQuery<FreeModelsResponse>({
    load: (signal) => apiGet<FreeModelsResponse>('/api/free-models', signal),
    tags: () => ['free_models'],
    initial: {}
  });

  const inv = $derived(inventoryQ.data ?? {});
  const counts = $derived(inv.counts ?? {});
  const statusRank: Record<string, number> = {
    served: 0,
    probe_alive: 1,
    unproven: 2,
    cooldown: 3,
    limited: 4,
    systemone: 5,
    dead: 6,
    not_free: 7
  };
  const rows = $derived(
    [...(inv.models ?? [])].sort(
      (a, b) => (statusRank[a.status ?? ''] ?? 9) - (statusRank[b.status ?? ''] ?? 9) || a.id.localeCompare(b.id)
    )
  );

  function statusClass(status: string | undefined): string {
    if (status === 'served') return 'free';
    if (status === 'dead' || status === 'not_free') return 'warn';
    if (status === 'systemone') return 'unknown';
    if (status === 'probe_alive' || status === 'limited' || status === 'cooldown') return 'fallback';
    return 'ignored';
  }
</script>

<Card title={t('free_models.inventory')}>
  <div class="inventory-head">
    <p class="hint">{t('free_models.inventory_hint')}</p>
    <button class="btn btn--sm" onclick={() => navigate('config', { tier: 'free' })}>
      {t('free_models.view_in_config')}
    </button>
  </div>

  {#if rows.length === 0}
    <Empty title={t('free_models.inventory_empty')} />
  {:else}
    <p class="summary">
      {t('free_models.inventory_counts', {
        listed: counts.listed ?? rows.length,
        served: counts.served ?? 0,
        unproven: counts.unproven ?? 0,
        cooldown: counts.cooldown ?? 0,
        candidates: counts.candidates ?? 0
      })}
      {#if inv.policy}
        <span class="muted"> · {t('free_models.inventory_policy', { policy: inv.policy })}</span>
      {/if}
    </p>

    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{t('free_models.col_model')}</th>
            <th>{t('free_models.col_source')}</th>
            <th>{t('free_models.col_status')}</th>
            <th>{t('free_models.col_mapped_from')}</th>
          </tr>
        </thead>
        <tbody>
          {#each rows as row (row.id)}
            <tr>
              <td class="mono">
                {row.id}
                {#if !row.routed}
                  <span class="badge badge--ignored">{t('free_models.not_routed')}</span>
                {/if}
                {#if row.route_target}
                  <div class="muted">{t('free_models.route_target')} {row.route_target}</div>
                {/if}
              </td>
              <td>{t(`free_models.source_${row.tier_source ?? 'endpoint'}`)}</td>
              <td>
                <span class="badge badge--{statusClass(row.status)}">
                  {t(`free_models.status_${row.status ?? 'unproven'}`)}
                </span>
                {#if row.cooldown?.retry_in_s}
                  <span class="muted"> · {Math.ceil(row.cooldown.retry_in_s)}s</span>
                {/if}
              </td>
              <td class="mono">
                {row.mapped_from?.length ? row.mapped_from.join(', ') : '—'}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</Card>

<VpnStatus />
<VpnRouting />
<VpnServers />
<VpnAdvanced />

<style>
  .inventory-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--sp-3);
    flex-wrap: wrap;
  }

  .inventory-head .hint {
    margin: 0;
    flex: 1 1 320px;
  }

  .summary {
    margin: var(--sp-3) 0;
    font-size: var(--fs-sm);
    color: var(--text-dim);
  }

  .badge {
    display: inline-block;
    font-size: var(--fs-xs);
    padding: 1px 5px;
    border-radius: var(--radius-sm);
    background: var(--surface-3);
    border: 1px solid var(--border);
    color: var(--text-dim);
    margin: 1px 2px 1px 0;
    white-space: nowrap;
  }

  .badge--free {
    background: color-mix(in srgb, var(--success) 20%, transparent);
    border-color: color-mix(in srgb, var(--success) 45%, transparent);
    color: var(--text);
  }

  .badge--fallback {
    background: color-mix(in srgb, var(--warn) 20%, transparent);
    border-color: color-mix(in srgb, var(--warn) 45%, transparent);
    color: var(--text);
  }

  .badge--ignored {
    background: color-mix(in srgb, var(--warn) 10%, transparent);
    border-style: dashed;
  }

  .badge--warn {
    background: var(--warn-bg);
    border-color: color-mix(in srgb, var(--warn) 45%, transparent);
    color: var(--text);
  }

  .badge--unknown {
    color: var(--text-faint);
  }
</style>
