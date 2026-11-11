<script lang="ts">
  /**
   * Panneau des proxys publics validés. Le backend ne renvoie jamais
   * l'URL locale du worker (elle contient le token) : cette vue reste
   * strictement read-only côté secrets.
   */

  import { apiGet, apiPost } from '$lib/api';
  import { t } from '$lib/i18n.svelte';
  import { createQuery } from '$lib/query.svelte';
  import { showToast } from '$lib/toast.svelte';
  import Card from '$lib/components/Card.svelte';
  import Empty from '$lib/components/Empty.svelte';

  interface PublicEndpoint {
    host?: string;
    port?: number;
    egress_ip?: string;
    provider?: string;
    enabled?: boolean;
    transport?: string;
    last_status?: number;
    last_checked_at?: number;
  }

  interface PublicState {
    enabled?: boolean;
    isolation?: string;
    active?: number;
    worker?: { kind?: string; port?: number; alive?: boolean };
    endpoints?: PublicEndpoint[];
    error?: string;
  }

  interface AdaptiveHealth {
    ok?: number;
    n429?: number;
    recent_429?: number;
    blocked?: number;
    blocked_ratio?: number;
  }

  interface AdaptiveState {
    enabled?: boolean;
    available?: string[];
    base?: Record<string, number>;
    health?: Record<string, AdaptiveHealth>;
    effective?: Record<string, number>;
  }

  interface CatalogProvider {
    known?: number;
    usable?: number;
    quota?: number;
    auth?: number;
    dead?: number;
    unique_blocks?: number;
    last_run_at?: number;
  }

  interface CatalogState {
    enabled?: boolean;
    providers?: Record<string, CatalogProvider>;
    rotation?: Record<string, { block?: string; egress_ip?: string; score?: number; status?: string }>;
    threat_intel?: { exact?: number; networks?: number; feeds?: Array<{ source?: string; count?: number; error?: string }> } | null;
    last_run?: { id?: string; status?: string; started_at?: number } | null;
  }

  interface CatalogOption {
    key?: string;
    provider?: string;
    kind?: string;
    host?: string;
    port?: number;
    egress_ip?: string;
    block?: string;
    status?: string;
    score?: number;
    last_status?: number;
    last_checked_at?: number;
    expires_at?: number;
    reset_at?: number;
  }

  interface CatalogSource {
    id?: string;
    type?: string;
    url?: string;
    enabled?: boolean;
    interval_s?: number;
    last_run_at?: number;
    last_error?: string;
    candidates_count?: number;
    usable_count?: number;
    yield_pct?: number;
  }

  const publicQ = createQuery<PublicState>({
    load: (signal) => apiGet<PublicState>('/api/vpn/public-proxies', signal),
    tags: () => ['vpn'],
    initial: { enabled: false, isolation: 'process', active: 0, worker: {}, endpoints: [] }
  });

  const adaptiveQ = createQuery<AdaptiveState>({
    load: (signal) => apiGet<AdaptiveState>('/api/egress/adaptive', signal),
    tags: () => ['vpn'],
    initial: { enabled: false, available: [], base: {}, health: {}, effective: {} }
  });

  const catalogQ = createQuery<CatalogState>({
    load: (signal) => apiGet<CatalogState>('/api/egress/catalog', signal),
    tags: () => ['vpn'],
    initial: { enabled: false, providers: {}, last_run: null }
  });

  let catalogProvider = $state('');
  let catalogStatus = $state('');
  let catalogKind = $state('');
  const catalogOptionsQ = createQuery<{ options: CatalogOption[] }>({
    load: (signal) =>
      apiGet<{ options: CatalogOption[] }>(
        `/api/egress/catalog/options?provider=${encodeURIComponent(catalogProvider)}&status=${encodeURIComponent(catalogStatus)}&kind=${encodeURIComponent(catalogKind)}&limit=50`,
        signal
      ),
    deps: () => `${catalogProvider}:${catalogStatus}:${catalogKind}`,
    tags: () => ['vpn'],
    initial: { options: [] }
  });
  const catalogSourcesQ = createQuery<{ sources: CatalogSource[] }>({
    load: (signal) => apiGet<{ sources: CatalogSource[] }>('/api/egress/catalog/sources', signal),
    tags: () => ['vpn'],
    initial: { sources: [] }
  });

  const pubState = $derived(publicQ.data ?? {});
  const endpoints = $derived(pubState.endpoints ?? []);
  const adaptive = $derived(adaptiveQ.data ?? {});
  const adaptiveProviders = $derived(
    [...new Set([...Object.keys(adaptive.base ?? {}), ...Object.keys(adaptive.effective ?? {})])].sort()
  );
  const catalogProviders = $derived(
    Object.entries(catalogQ.data?.providers ?? {}).sort(([a], [b]) => a.localeCompare(b))
  );
  const catalogOptions = $derived(catalogOptionsQ.data?.options ?? []);
  const catalogSources = $derived(catalogSourcesQ.data?.sources ?? []);
  const threat = $derived(catalogQ.data?.threat_intel ?? null);
  let busyAction = $state<'refresh' | 'revalidate' | ''>('');
  let catalogBusy = $state('');
  let sourceId = $state('');
  let sourceType = $state('public_text');
  let sourceUrl = $state('');
  let sourceBusy = $state(false);
  let threatBusy = $state(false);
  let blockingKey = $state('');
  let error = $state('');

  async function refresh(enabled?: boolean): Promise<void> {
    busyAction = 'refresh';
    error = '';
    try {
      const body: Record<string, unknown> = {};
      if (enabled !== undefined) body.enabled = enabled;
      const res = await apiPost<{ error?: string }>('/api/vpn/public-proxies/refresh', body);
      if (res?.error) throw new Error(res.error);
      await publicQ.refresh(true);
      showToast(t('free_models.public_refreshed'), 'success');
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      showToast(error, 'error');
    } finally {
      busyAction = '';
    }
  }

  async function revalidate(): Promise<void> {
    busyAction = 'revalidate';
    error = '';
    try {
      const res = await apiPost<{ error?: string }>('/api/vpn/public-proxies/revalidate', {});
      if (res?.error) throw new Error(res.error);
      await publicQ.refresh(true);
      showToast(t('free_models.public_revalidated'), 'success');
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      showToast(error, 'error');
    } finally {
      busyAction = '';
    }
  }

  async function refreshCatalog(provider: string): Promise<void> {
    catalogBusy = provider;
    error = '';
    try {
      const res = await apiPost<{ error?: string }>('/api/egress/catalog/refresh', { provider });
      if (res?.error) throw new Error(res.error);
      await catalogQ.refresh(true);
      showToast(t('free_models.catalog_refreshed'), 'success');
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      showToast(error, 'error');
    } finally {
      catalogBusy = '';
    }
  }

  function ageSeconds(value?: number): string {
    if (!value) return '—';
    const seconds = Math.max(0, Math.round(Date.now() / 1000 - value));
    return String(seconds);
  }

  async function refreshThreatFeeds(): Promise<void> {
    threatBusy = true;
    error = '';
    try {
      const res = await apiPost<{ error?: string }>('/api/egress/catalog/threat-feeds/refresh', {});
      if (res?.error) throw new Error(res.error);
      await catalogQ.refresh(true);
      showToast(t('free_models.catalog_threat_refreshed'), 'success');
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      showToast(error, 'error');
    } finally {
      threatBusy = false;
    }
  }

  async function blockOption(option: CatalogOption): Promise<void> {
    const ip = option.egress_ip || option.host;
    if (!ip) return;
    blockingKey = option.key ?? ip;
    error = '';
    try {
      const res = await apiPost<{ error?: string }>('/api/egress/catalog/block', { ip });
      if (res?.error) throw new Error(res.error);
      await Promise.all([catalogQ.refresh(true), catalogOptionsQ.refresh(true)]);
      showToast(t('free_models.catalog_blocked'), 'success');
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      showToast(error, 'error');
    } finally {
      blockingKey = '';
    }
  }

  async function addSource(): Promise<void> {
    sourceBusy = true;
    error = '';
    try {
      const res = await apiPost<{ error?: string }>('/api/egress/catalog/sources', {
        id: sourceId,
        type: sourceType,
        url: sourceUrl,
        enabled: true
      });
      if (res?.error) throw new Error(res.error);
      sourceId = '';
      sourceUrl = '';
      await catalogSourcesQ.refresh(true);
      showToast(t('free_models.catalog_source_added'), 'success');
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      showToast(error, 'error');
    } finally {
      sourceBusy = false;
    }
  }
</script>

<Card title={t('free_models.public_title')}>
  <p class="hint">{t('free_models.public_hint')}</p>

  <div class="summary">
    <span class="chip" class:on={pubState.enabled}>
      {pubState.enabled ? t('free_models.public_enabled') : t('free_models.public_disabled')}
    </span>
    <span class="chip">{t('free_models.public_isolation')} : {pubState.isolation ?? 'process'}</span>
    <span class="chip">{t('free_models.public_active')} : {pubState.active ?? 0}</span>
    {#if pubState.worker?.kind}
      <span class="chip" class:on={pubState.worker.alive}>
        {t('free_models.public_worker')} : {pubState.worker.kind}
        {#if pubState.worker.port}· :{pubState.worker.port}{/if}
        · {pubState.worker.alive ? t('free_models.public_alive') : t('free_models.public_dead')}
      </span>
    {/if}
  </div>

  {#if error}
    <p class="error" role="alert">{error}</p>
  {/if}

  <div class="actions">
    <button
      type="button"
      class="btn btn--sm"
      data-option-map="public-pool-toggle"
      disabled={busyAction !== ''}
      onclick={() => refresh(!pubState.enabled)}
    >
      {busyAction === 'refresh'
        ? t('free_models.public_refreshing')
        : pubState.enabled
          ? t('free_models.public_disable')
          : t('free_models.public_enable')}
    </button>
    <button
      type="button"
      class="btn btn--sm btn--ghost"
      data-option-map="public-pool-reload"
      disabled={busyAction !== ''}
      onclick={() => refresh()}
    >
      {busyAction === 'refresh' ? t('free_models.public_refreshing') : t('free_models.public_refresh')}
    </button>
    <button
      type="button"
      class="btn btn--sm btn--ghost"
      data-option-map="public-pool-revalidate"
      disabled={busyAction !== '' || !pubState.enabled}
      onclick={() => revalidate()}
    >
      {busyAction === 'revalidate'
        ? t('free_models.public_revalidating')
        : t('free_models.public_revalidate')}
    </button>
  </div>

  {#if endpoints.length === 0}
    <Empty title={t('free_models.public_empty')} />
  {:else}
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{t('free_models.public_col_egress')}</th>
            <th>{t('free_models.public_col_host')}</th>
            <th>{t('free_models.public_col_port')}</th>
            <th>{t('free_models.public_col_transport')}</th>
            <th>{t('free_models.public_col_status')}</th>
          </tr>
        </thead>
        <tbody>
          {#each endpoints as ep (ep.egress_ip ?? `${ep.host}:${ep.port}`)}
            <tr>
              <td class="mono">{ep.egress_ip || '—'}</td>
              <td class="mono">{ep.host || '—'}</td>
              <td class="mono">{ep.port ?? '—'}</td>
              <td>{ep.transport ?? 'worker'}</td>
              <td class="mono status" class:ok={ep.last_status === 200} class:warn={ep.last_status === 429} class:dead={!ep.last_status || (ep.last_status >= 400 && ep.last_status !== 429)}>
                {ep.last_status || '—'}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</Card>

<Card title={t('free_models.catalog_title')}>
  <div data-option-map="catalog-summary">
  <p class="hint">{t('free_models.catalog_hint')}</p>
  {#if !catalogQ.data?.enabled}
    <Empty title={t('free_models.catalog_disabled')} />
  {:else if catalogProviders.length === 0}
    <Empty title={t('free_models.catalog_empty')} />
  {:else}
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{t('free_models.catalog_provider')}</th>
            <th>{t('free_models.catalog_known')}</th>
            <th>{t('free_models.catalog_usable')}</th>
            <th>{t('free_models.catalog_quota')}</th>
            <th>{t('free_models.catalog_auth')}</th>
            <th>{t('free_models.catalog_dead')}</th>
            <th>{t('free_models.catalog_blocks')}</th>
            <th>{t('free_models.catalog_rotation')}</th>
            <th>{t('free_models.catalog_age')}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each catalogProviders as [provider, info] (provider)}
            <tr>
              <td class="mono">{provider}</td>
              <td class="mono">{info.known ?? 0}</td>
              <td class="mono"><span class:ok={(info.usable ?? 0) > 0}>{info.usable ?? 0}</span></td>
              <td class="mono">{info.quota ?? 0}</td>
              <td class="mono">{info.auth ?? 0}</td>
              <td class="mono">{info.dead ?? 0}</td>
              <td class="mono">{info.unique_blocks ?? 0}</td>
              <td class="mono">
                {catalogQ.data?.rotation?.[provider]?.block ||
                  catalogQ.data?.rotation?.[provider]?.egress_ip ||
                  '—'}
              </td>
              <td class="mono">{ageSeconds(info.last_run_at)} s</td>
              <td>
                <button
                  type="button"
                  class="btn btn--sm btn--ghost"
                  data-option-map="catalog-refresh"
                  disabled={catalogBusy !== ''}
                  onclick={() => refreshCatalog(provider)}
                >
                  {catalogBusy === provider
                    ? t('free_models.catalog_refreshing')
                    : t('free_models.catalog_refresh')}
                </button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
  {#if catalogQ.data?.enabled}
    <div class="catalog-filters">
      <label>
        {t('free_models.catalog_provider')}
        <select data-option-map="catalog-filter-provider" bind:value={catalogProvider}>
          <option value="">{t('free_models.catalog_all')}</option>
          <option value="public">public</option>
          <option value="warp">warp</option>
          <option value="nordvpn">nordvpn</option>
        </select>
      </label>
      <label>
        {t('free_models.catalog_status')}
        <select data-option-map="catalog-filter-status" bind:value={catalogStatus}>
          <option value="">{t('free_models.catalog_all')}</option>
          <option value="usable">usable</option>
          <option value="quota">quota</option>
          <option value="auth">auth</option>
          <option value="dead">dead</option>
          <option value="reachable">reachable</option>
        </select>
      </label>
      <label>
        {t('free_models.catalog_kind')}
        <select data-option-map="catalog-filter-kind" bind:value={catalogKind}>
          <option value="">{t('free_models.catalog_all')}</option>
          <option value="https">https</option>
          <option value="http">http</option>
          <option value="socks5">socks5</option>
        </select>
      </label>
    </div>
    {#if catalogOptions.length === 0}
      <Empty title={t('free_models.catalog_options_empty')} />
    {:else}
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>{t('free_models.catalog_provider')}</th>
              <th>{t('free_models.catalog_kind')}</th>
              <th>{t('free_models.catalog_status')}</th>
              <th>{t('free_models.catalog_score')}</th>
              <th>{t('free_models.catalog_egress')}</th>
              <th>{t('free_models.catalog_age')}</th>
              <th>{t('free_models.catalog_reset')}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {#each catalogOptions as option (option.key)}
              <tr>
                <td class="mono">{option.provider ?? ''}</td>
                <td class="mono">{option.kind ?? ''}</td>
                <td class="mono">{option.status ?? ''}</td>
                <td class="mono">{option.score != null ? option.score.toFixed(1) : '—'}</td>
                <td class="mono">{option.egress_ip || option.block || ''}</td>
                <td class="mono">{ageSeconds(option.last_checked_at)} s</td>
                <td class="mono">{option.reset_at ? `${ageSeconds(option.reset_at)} s` : '—'}</td>
                <td>
                  <button
                    type="button"
                    class="btn btn--sm btn--ghost"
                    data-option-map="catalog-block-ip"
                    disabled={blockingKey !== ''}
                    onclick={() => blockOption(option)}
                  >
                    {blockingKey === (option.key ?? '')
                      ? t('free_models.catalog_blocking')
                      : t('free_models.catalog_block')}
                  </button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
    <h3 class="catalog-sources-title">{t('free_models.catalog_sources')}</h3>
    <div class="catalog-sources" data-option-map="catalog-sources">
      {#if catalogSources.length === 0}
        <Empty title={t('free_models.catalog_sources_empty')} />
      {:else}
        <ul>
          {#each catalogSources as source (source.id)}
            <li class="mono">
              {source.id} · {source.type} · {source.enabled ? t('free_models.catalog_enabled') : t('free_models.catalog_disabled_short')}
              · {source.usable_count ?? 0}/{source.candidates_count ?? 0}
              ({source.yield_pct ?? 0} %)
              {#if source.last_error}<span class="error"> · {source.last_error}</span>{/if}
            </li>
          {/each}
        </ul>
      {/if}
      <div class="catalog-source-form">
        <input data-option-map="catalog-source-id" bind:value={sourceId} placeholder={t('free_models.catalog_source_id')} />
        <select data-option-map="catalog-source-type" bind:value={sourceType}>
          <option value="public_text">public_text</option>
          <option value="public_json">public_json</option>
          <option value="geonode_json">geonode_json</option>
        </select>
        <input data-option-map="catalog-source-url" bind:value={sourceUrl} placeholder={t('free_models.catalog_source_url')} />
        <button
          type="button"
          class="btn btn--sm"
          data-option-map="catalog-source-add"
          disabled={sourceBusy || !sourceId || !sourceUrl}
          onclick={() => addSource()}
        >
          {sourceBusy ? t('free_models.catalog_source_adding') : t('free_models.catalog_source_add')}
        </button>
      </div>
    </div>
    <h3 class="catalog-sources-title">{t('free_models.catalog_threat_feeds')}</h3>
    <div class="catalog-threat" data-option-map="catalog-threat-feeds">
      {#if threat}
        <span class="mono">
          {threat.exact ?? 0} IP · {threat.networks ?? 0} CIDR
        </span>
        <button
          type="button"
          class="btn btn--sm btn--ghost"
          data-option-map="catalog-threat-refresh"
          disabled={threatBusy}
          onclick={() => refreshThreatFeeds()}
        >
          {threatBusy ? t('free_models.catalog_threat_refreshing') : t('free_models.catalog_threat_refresh')}
        </button>
      {:else}
        <Empty title={t('free_models.catalog_threat_disabled')} />
      {/if}
    </div>
  {/if}
  </div>
</Card>

<Card title={t('free_models.adaptive_title')}>
  <div data-option-map="public-adaptive-card">
  <p class="hint">{t('free_models.adaptive_hint')}</p>
  {#if !adaptive.enabled}
    <Empty title={t('free_models.adaptive_off')} />
  {:else if adaptiveProviders.length === 0}
    <Empty title={t('free_models.adaptive_empty')} />
  {:else}
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{t('free_models.adaptive_provider')}</th>
            <th>{t('free_models.adaptive_base')}</th>
            <th>{t('free_models.adaptive_effective')}</th>
            <th>{t('free_models.adaptive_recent_429')}</th>
            <th>{t('free_models.adaptive_blocked')}</th>
          </tr>
        </thead>
        <tbody>
          {#each adaptiveProviders as provider (provider)}
            <tr>
              <td class="mono">{provider}</td>
              <td class="mono">{adaptive.base?.[provider] ?? 0} %</td>
              <td class="mono">
                <span class:ok={(adaptive.effective?.[provider] ?? 0) > 0}>
                  {adaptive.effective?.[provider] ?? 0} %
                </span>
              </td>
              <td class="mono">{adaptive.health?.[provider]?.recent_429 ?? 0}</td>
              <td class="mono">{adaptive.health?.[provider]?.blocked ?? 0}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
  </div>
</Card>

<style>
  .summary {
    display: flex;
    flex-wrap: wrap;
    gap: var(--sp-2);
    margin: var(--sp-3) 0;
  }

  .chip {
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 2px 8px;
    font-size: var(--fs-xs);
    color: var(--text-dim);
  }

  .chip.on {
    border-color: var(--success);
    color: var(--success);
  }

  .catalog-filters {
    display: flex;
    flex-wrap: wrap;
    gap: var(--sp-3);
    margin: var(--sp-3) 0;
  }

  .catalog-filters label {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    font-size: var(--fs-xs);
    color: var(--text-dim);
  }

  .catalog-sources-title {
    margin: var(--sp-4) 0 var(--sp-2);
    font-size: var(--fs-sm);
  }

  .catalog-sources ul {
    margin: 0 0 var(--sp-2);
    padding-left: var(--sp-4);
    font-size: var(--fs-xs);
  }

  .catalog-source-form {
    display: flex;
    flex-wrap: wrap;
    gap: var(--sp-2);
    align-items: center;
  }

  .catalog-source-form input,
  .catalog-source-form select {
    min-width: 10rem;
    padding: 2px 6px;
    font-size: var(--fs-xs);
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  .catalog-threat {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    margin-top: var(--sp-2);
    font-size: var(--fs-xs);
  }

  .actions {
    display: flex;
    gap: var(--sp-2);
    flex-wrap: wrap;
    margin-bottom: var(--sp-3);
  }

  .error {
    color: var(--danger);
    font-size: var(--fs-sm);
  }

  .status.ok,
  .ok {
    color: var(--success);
  }

  .status.warn {
    color: var(--warn);
  }

  .status.dead {
    color: var(--danger);
  }
</style>
