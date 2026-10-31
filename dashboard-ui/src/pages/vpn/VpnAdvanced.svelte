<script lang="ts">
  /**
   * Blocs restants de la page Modèles Gratuits : fiabilité (circuit breaker,
   * backoff, watchdog), outils de maintenance, découverte de serveurs via
   * l'API NordVPN, et le panneau SOCKS5.
   *
   * `api_cache_ttl` de l'ancienne interface est retiré : aucune clé du même
   * nom n'existe côté backend, le champ ne sauvegardait rien.
   */

  import { apiGet, apiPost } from '$lib/api';
  import { t } from '$lib/i18n.svelte';
  import { createQuery } from '$lib/query.svelte';
  import { showToast } from '$lib/toast.svelte';
  import Card from '$lib/components/Card.svelte';
  import Toggle from '$lib/components/Toggle.svelte';

  interface QualityPreselection {
    enabled?: boolean;
    fast_tps?: number;
    slow_tps?: number;
    max_load?: number;
    tier_b_quota?: number;
    tier_c_quota?: number;
  }

  interface QualityRotation {
    enabled?: boolean;
    min_requests?: number;
    min_samples?: number;
    trend_pct?: number;
    ttfb_ceiling_ms?: number;
    max_per_hour?: number;
    global_slow_guard?: number;
  }

  interface PostRotationProbe {
    enabled?: boolean;
    max_attempts?: number;
    max_tokens?: number;
  }

  interface VpnConfig {
    circuit_breaker_threshold?: number;
    circuit_breaker_recovery?: number;
    backoff_max_delay?: number;
    watchdog_interval?: number;
    quality_preselection?: QualityPreselection;
    quality_rotation?: QualityRotation;
    post_rotation_probe?: PostRotationProbe;
    [key: string]: unknown;
  }

  interface Socks5Proxy {
    host?: string;
    port?: number;
    enabled?: boolean;
    username?: string | null;
  }

  interface Socks5State {
    proxies?: Socks5Proxy[];
    rotate?: boolean;
  }

  interface Country {
    code?: string;
    name?: string;
  }

  interface ToolResult {
    error?: string;
    [key: string]: unknown;
  }

  /** Déclaré avant les requêtes : `enabled()` est évalué à la construction. */
  let useApi = $state(false);

  const cfgQ = createQuery<VpnConfig>({
    load: (signal) => apiGet<VpnConfig>('/api/vpn-config', signal),
    tags: () => ['vpn'],
    initial: {}
  });

  const socksQ = createQuery<Socks5State>({
    load: (signal) => apiGet<Socks5State>('/api/vpn/socks5', signal),
    tags: () => ['vpn'],
    initial: {}
  });

  const countriesQ = createQuery<{ countries?: Country[] }>({
    load: (signal) => apiGet<{ countries?: Country[] }>('/api/vpn/countries', signal),
    tags: () => ['vpn'],
    initial: {},
    enabled: () => useApi
  });

  const cfg = $derived(cfgQ.data);
  const proxies = $derived(socksQ.data?.proxies ?? []);

  let busy = $state<string | null>(null);
  let country = $state('');
  let limit = $state(5);
  let toolResult = $state('');
  let discoverResult = $state('');
  let testResults = $state<Record<string, string>>({});

  // Formulaire d'ajout
  let host = $state('');
  let port = $state(1080);
  let username = $state('');
  let password = $state('');

  async function run(field: string, fn: () => Promise<unknown>): Promise<ToolResult | null> {
    if (busy) return null;
    busy = field;
    try {
      return (await fn()) as ToolResult;
    } catch (e) {
      showToast(e instanceof Error ? e.message : String(e), 'error');
      return null;
    } finally {
      busy = null;
    }
  }

  function saveAdvanced(patch: Partial<VpnConfig>): void {
    void run('advanced', () =>
      apiPost('/api/vpn-config', {
        circuit_breaker_threshold: Number(cfg?.circuit_breaker_threshold) || 3,
        circuit_breaker_recovery: Number(cfg?.circuit_breaker_recovery) || 300,
        backoff_max_delay: Number(cfg?.backoff_max_delay) || 60,
        watchdog_interval: Number(cfg?.watchdog_interval) ?? 60,
        ...patch
      })
    );
  }

  type QualityBlock = 'quality_preselection' | 'quality_rotation' | 'post_rotation_probe';

  async function saveQuality(block: QualityBlock, patch: Record<string, unknown>): Promise<void> {
    const res = await run('quality', () => apiPost('/api/vpn-config', { [block]: patch }));
    if (res) await cfgQ.refresh(true);
  }

  async function healthCheck(): Promise<void> {
    const res = await run('health', () => apiPost<ToolResult>('/api/vpn/health-check'));
    if (!res) return;
    toolResult = res.error
      ? res.error
      : Object.entries(res)
          .filter(([, v]) => typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean')
          .map(([k, v]) => `${k}: ${String(v)}`)
          .join(' · ');
  }

  async function diagnostic(): Promise<void> {
    const res = await run('diagnostic', () => apiGet<ToolResult>('/api/vpn/diagnostic'));
    if (!res) return;
    toolResult = res.error
      ? res.error
      : Object.entries(res)
          .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
          .join('\n');
  }

  async function saveState(): Promise<void> {
    const res = await run('save-state', () => apiPost<ToolResult>('/api/vpn/save-state'));
    if (!res) return;
    showToast(res.error ?? t('vpn.config_summary'), res.error ? 'error' : 'success');
  }

  async function exportConfig(): Promise<void> {
    const res = await run('export', () => apiGet<ToolResult>('/api/vpn/export'));
    if (!res || res.error) {
      if (res?.error) showToast(res.error, 'error');
      return;
    }
    // L'export est un téléchargement : on le déclenche depuis un blob, le
    // header d'auth ne peut pas être porté par un simple window.open.
    const blob = new Blob([JSON.stringify(res, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'opencode-vpn-config.json';
    a.click();
    URL.revokeObjectURL(url);
  }

  function discover(): void {
    void run('discover', async () => {
      if (!country) {
        showToast(t('vpn.api_country'), 'warning');
        return null;
      }
      const res = await apiPost<{ count?: number; country?: string; error?: string }>(
        '/api/vpn/discover-and-add',
        { country, limit }
      );
      if (res.error) throw new Error(res.error);
      discoverResult = t('vpn.discovered', { n: res.count ?? 0, country: res.country ?? country });
      showToast(discoverResult, 'success');
      await cfgQ.refresh(true);
      return res;
    });
  }

  function setRotate(value: boolean): void {
    void run('rotate', async () => {
      await apiPost('/api/vpn/socks5/rotate', { rotate: value });
      await socksQ.refresh(true);
    });
  }

  function addProxy(): void {
    void run('add-proxy', async () => {
      if (!host) {
        showToast(t('free_models.socks5_host'), 'warning');
        return null;
      }
      const res = await apiPost<{ error?: string }>('/api/vpn/socks5', {
        host,
        port: Number(port) || 1080,
        username: username || null,
        password: password || null
      });
      if (res?.error) throw new Error(res.error);
      host = '';
      username = '';
      password = '';
      await socksQ.refresh(true);
      return res;
    });
  }

  function removeProxy(index: number): void {
    void run('remove-proxy', async () => {
      const res = await apiPost<{ error?: string }>('/api/vpn/socks5/remove', { index });
      if (res?.error) throw new Error(res.error);
      await socksQ.refresh(true);
      return res;
    });
  }

  function toggleProxy(index: number, enabled: boolean): void {
    void run('toggle-proxy', async () => {
      const res = await apiPost<{ error?: string }>('/api/vpn/socks5/toggle', { index, enabled });
      if (res?.error) throw new Error(res.error);
      await socksQ.refresh(true);
      return res;
    });
  }

  function testProxy(index: number, p: Socks5Proxy): void {
    void run('test-proxy', async () => {
      const key = `${p.host}:${p.port}`;
      testResults = { ...testResults, [key]: '…' };
      const res = await apiPost<{ ok?: boolean; ip?: string; latency_ms?: number; error?: string }>(
        '/api/vpn/socks5/test',
        { host: p.host, port: p.port }
      );
      testResults = {
        ...testResults,
        [key]: res.error
          ? t('vpn.test_fail', { err: res.error })
          : t('vpn.test_ok', { ip: res.ip ?? '?', ms: res.latency_ms ?? 0 })
      };
      return res;
    });
  }
</script>

<Card title={t('vpn.advanced')}>
  <div class="grid">
    <div class="field">
      <span class="label">{t('vpn.cb_threshold')}</span>
      <input
        class="input input--narrow"
        type="number"
        min="1"
        max="10"
        value={String(cfg?.circuit_breaker_threshold ?? 3)}
        disabled={busy === 'advanced'}
        onchange={(e) => saveAdvanced({ circuit_breaker_threshold: Number(e.currentTarget.value) || 3 })}
      />
    </div>
    <div class="field">
      <span class="label">{t('vpn.cb_recovery')}</span>
      <input
        class="input input--narrow"
        type="number"
        min="30"
        max="3600"
        value={String(cfg?.circuit_breaker_recovery ?? 300)}
        disabled={busy === 'advanced'}
        onchange={(e) => saveAdvanced({ circuit_breaker_recovery: Number(e.currentTarget.value) || 300 })}
      />
    </div>
    <div class="field">
      <span class="label">{t('vpn.backoff_max')}</span>
      <input
        class="input input--narrow"
        type="number"
        min="10"
        max="300"
        value={String(cfg?.backoff_max_delay ?? 60)}
        disabled={busy === 'advanced'}
        onchange={(e) => saveAdvanced({ backoff_max_delay: Number(e.currentTarget.value) || 60 })}
      />
    </div>
    <div class="field">
      <span class="label">{t('vpn.watchdog')}</span>
      <input
        class="input input--narrow"
        type="number"
        min="0"
        max="600"
        value={String(cfg?.watchdog_interval ?? 60)}
        disabled={busy === 'advanced'}
        onchange={(e) => saveAdvanced({ watchdog_interval: Number(e.currentTarget.value) || 60 })}
      />
    </div>
  </div>

  <div class="tools">
    <button class="btn btn--sm" disabled={busy === 'health'} onclick={healthCheck}>
      🔍 {t('vpn.health_check')}
    </button>
    <button class="btn btn--sm" disabled={busy === 'diagnostic'} onclick={diagnostic}>
      🩺 {t('vpn.diagnostic')}
    </button>
    <button class="btn btn--sm" disabled={busy === 'save-state'} onclick={saveState}>
      💾 {t('vpn.save_state')}
    </button>
    <button class="btn btn--sm" disabled={busy === 'export'} onclick={exportConfig}>
      📤 {t('vpn.export')}
    </button>
  </div>

  {#if toolResult}
    <pre class="result">{toolResult}</pre>
  {/if}

  <div class="discovery">
    <div class="row">
      <Toggle
        checked={useApi}
        label={t('vpn.api_discovery')}
        disabled={busy === 'discover'}
        onchange={(v) => {
          useApi = v;
          discoverResult = '';
        }}
      />
      <span class="muted small">{t('vpn.api_hint')}</span>
    </div>

    {#if useApi}
      <div class="row">
        <label class="label" for="vpn-api-country">{t('vpn.api_country')}</label>
        <select id="vpn-api-country" class="select select--country" bind:value={country}>
          <option value="">{t('vpn.api_all')}</option>
          {#each countriesQ.data?.countries ?? [] as c (c.code ?? c.name ?? '')}
            <option value={c.code ?? ''}>{c.name ?? c.code}</option>
          {/each}
        </select>

        <label class="label" for="vpn-api-limit">{t('vpn.api_limit')}</label>
        <input
          id="vpn-api-limit"
          class="input input--narrow"
          type="number"
          min="1"
          max="20"
          bind:value={limit}
        />

        <button class="btn btn--primary btn--sm" disabled={busy === 'discover'} onclick={discover}>
          {t('vpn.discover')}
        </button>

        {#if discoverResult}
          <span class="muted small">{discoverResult}</span>
        {/if}
      </div>
    {/if}
  </div>
</Card>

<Card title={t('vpn.quality_title')} hint={t('vpn.quality_hint')}>
  <div class="quality-section">
    <div class="quality-head">
      <h4>{t('vpn.q_preselect')}</h4>
      <span class="muted small">{t('vpn.q_preselect_hint')}</span>
    </div>
    <div class="grid">
      <div class="field">
        <Toggle
          checked={!!cfg?.quality_preselection?.enabled}
          label={t('vpn.q_enabled')}
          disabled={busy === 'quality'}
          onchange={(v) => void saveQuality('quality_preselection', { enabled: v })}
        />
      </div>
      <div class="field">
        <span class="label">{t('vpn.q_fast_tps')}</span>
        <input
          class="input input--narrow"
          type="number"
          min="1"
          max="1000"
          value={String(cfg?.quality_preselection?.fast_tps ?? 50)}
          disabled={busy === 'quality'}
          onchange={(e) =>
            void saveQuality('quality_preselection', { fast_tps: Number(e.currentTarget.value) })}
        />
      </div>
      <div class="field">
        <span class="label">{t('vpn.q_slow_tps')}</span>
        <input
          class="input input--narrow"
          type="number"
          min="1"
          max="1000"
          value={String(cfg?.quality_preselection?.slow_tps ?? 20)}
          disabled={busy === 'quality'}
          onchange={(e) =>
            void saveQuality('quality_preselection', { slow_tps: Number(e.currentTarget.value) })}
        />
      </div>
      <div class="field">
        <span class="label">{t('vpn.q_max_load')}</span>
        <input
          class="input input--narrow"
          type="number"
          min="0"
          max="100"
          value={String(cfg?.quality_preselection?.max_load ?? 80)}
          disabled={busy === 'quality'}
          onchange={(e) =>
            void saveQuality('quality_preselection', { max_load: Number(e.currentTarget.value) })}
        />
      </div>
      <div class="field">
        <span class="label">{t('vpn.q_tier_b')}</span>
        <input
          class="input input--narrow"
          type="number"
          min="1"
          max="500"
          value={String(cfg?.quality_preselection?.tier_b_quota ?? 250)}
          disabled={busy === 'quality'}
          onchange={(e) =>
            void saveQuality('quality_preselection', { tier_b_quota: Number(e.currentTarget.value) })}
        />
      </div>
      <div class="field">
        <span class="label">{t('vpn.q_tier_c')}</span>
        <input
          class="input input--narrow"
          type="number"
          min="1"
          max="500"
          value={String(cfg?.quality_preselection?.tier_c_quota ?? 60)}
          disabled={busy === 'quality'}
          onchange={(e) =>
            void saveQuality('quality_preselection', { tier_c_quota: Number(e.currentTarget.value) })}
        />
      </div>
    </div>
  </div>

  <div class="quality-section">
    <div class="quality-head">
      <h4>{t('vpn.q_rotation')}</h4>
      <span class="muted small">{t('vpn.q_rotation_hint')}</span>
    </div>
    <div class="grid">
      <div class="field">
        <Toggle
          checked={!!cfg?.quality_rotation?.enabled}
          label={t('vpn.q_enabled')}
          disabled={busy === 'quality'}
          onchange={(v) => void saveQuality('quality_rotation', { enabled: v })}
        />
      </div>
      <div class="field">
        <span class="label">{t('vpn.q_min_requests')}</span>
        <input
          class="input input--narrow"
          type="number"
          min="1"
          max="500"
          value={String(cfg?.quality_rotation?.min_requests ?? 10)}
          disabled={busy === 'quality'}
          onchange={(e) =>
            void saveQuality('quality_rotation', { min_requests: Number(e.currentTarget.value) })}
        />
      </div>
      <div class="field">
        <span class="label">{t('vpn.q_min_samples')}</span>
        <input
          class="input input--narrow"
          type="number"
          min="1"
          max="20"
          value={String(cfg?.quality_rotation?.min_samples ?? 3)}
          disabled={busy === 'quality'}
          onchange={(e) =>
            void saveQuality('quality_rotation', { min_samples: Number(e.currentTarget.value) })}
        />
      </div>
      <div class="field">
        <span class="label">{t('vpn.q_trend')}</span>
        <input
          class="input input--narrow"
          type="number"
          min="0"
          max="100"
          step="5"
          value={String(cfg?.quality_rotation?.trend_pct ?? 25)}
          disabled={busy === 'quality'}
          onchange={(e) =>
            void saveQuality('quality_rotation', { trend_pct: Number(e.currentTarget.value) })}
        />
      </div>
      <div class="field">
        <span class="label">{t('vpn.q_ttfb')}</span>
        <input
          class="input input--narrow"
          type="number"
          min="100"
          max="60000"
          step="100"
          value={String(cfg?.quality_rotation?.ttfb_ceiling_ms ?? 8000)}
          disabled={busy === 'quality'}
          onchange={(e) =>
            void saveQuality('quality_rotation', { ttfb_ceiling_ms: Number(e.currentTarget.value) })}
        />
      </div>
      <div class="field">
        <span class="label">{t('vpn.q_max_per_hour')}</span>
        <input
          class="input input--narrow"
          type="number"
          min="0"
          max="60"
          value={String(cfg?.quality_rotation?.max_per_hour ?? 6)}
          disabled={busy === 'quality'}
          onchange={(e) =>
            void saveQuality('quality_rotation', { max_per_hour: Number(e.currentTarget.value) })}
        />
      </div>
      <div class="field">
        <span class="label">{t('vpn.q_global_guard')}</span>
        <input
          class="input input--narrow"
          type="number"
          min="1"
          max="10"
          value={String(cfg?.quality_rotation?.global_slow_guard ?? 2)}
          disabled={busy === 'quality'}
          onchange={(e) =>
            void saveQuality('quality_rotation', { global_slow_guard: Number(e.currentTarget.value) })}
        />
      </div>
    </div>
  </div>

  <div class="quality-section">
    <div class="quality-head">
      <h4>{t('vpn.q_probe')}</h4>
      <span class="muted small">{t('vpn.q_probe_hint')}</span>
    </div>
    <div class="grid">
      <div class="field">
        <Toggle
          checked={!!cfg?.post_rotation_probe?.enabled}
          label={t('vpn.q_enabled')}
          disabled={busy === 'quality'}
          onchange={(v) => void saveQuality('post_rotation_probe', { enabled: v })}
        />
      </div>
      <div class="field">
        <span class="label">{t('vpn.q_probe_attempts')}</span>
        <input
          class="input input--narrow"
          type="number"
          min="1"
          max="5"
          value={String(cfg?.post_rotation_probe?.max_attempts ?? 2)}
          disabled={busy === 'quality'}
          onchange={(e) =>
            void saveQuality('post_rotation_probe', { max_attempts: Number(e.currentTarget.value) })}
        />
      </div>
      <div class="field">
        <span class="label">{t('vpn.q_probe_tokens')}</span>
        <input
          class="input input--narrow"
          type="number"
          min="1"
          max="32"
          value={String(cfg?.post_rotation_probe?.max_tokens ?? 4)}
          disabled={busy === 'quality'}
          onchange={(e) =>
            void saveQuality('post_rotation_probe', { max_tokens: Number(e.currentTarget.value) })}
        />
      </div>
    </div>
  </div>
</Card>

<Card title={t('free_models.socks5_title')} hint={t('free_models.socks5_hint')}>
  <div class="row">
    <Toggle
      checked={socksQ.data?.rotate !== false}
      label={t('free_models.socks5_rotation')}
      disabled={busy === 'rotate'}
      onchange={setRotate}
    />
  </div>

  <div class="proxy-list">
    {#if proxies.length === 0}
      <p class="muted small">{t('vpn.no_proxies')}</p>
    {:else}
      {#each proxies as p, i (i)}
        <div class="proxy" class:off={!p.enabled}>
          <span class="mono proxy-addr">{p.host}:{p.port}</span>
          {#if p.username}
            <span class="muted small">{p.username}</span>
          {/if}
          <span class="muted small">{p.enabled ? t('vpn.on') : t('vpn.off')}</span>
          {#if testResults[`${p.host}:${p.port}`]}
            <span class="muted small">{testResults[`${p.host}:${p.port}`]}</span>
          {/if}
          <div class="proxy-actions">
            <input
              type="checkbox"
              checked={!!p.enabled}
              aria-label={t('vpn.proxy_enabled')}
              onchange={(e) => toggleProxy(i, e.currentTarget.checked)}
            />
            <button class="btn btn--sm" disabled={busy === 'test-proxy'} onclick={() => testProxy(i, p)}>
              {t('vpn.test')}
            </button>
            <button
              class="btn btn--danger btn--sm"
              disabled={busy === 'remove-proxy'}
              onclick={() => removeProxy(i)}
            >
              {t('vpn.remove')}
            </button>
          </div>
        </div>
      {/each}
    {/if}
  </div>

  <div class="add-form">
    <label class="field-inline">
      <span class="label">{t('free_models.socks5_host')}</span>
      <input class="input input--host" type="text" placeholder="1.2.3.4" bind:value={host} />
    </label>
    <label class="field-inline">
      <span class="label">{t('free_models.socks5_port')}</span>
      <input class="input input--port" type="number" placeholder="1080" bind:value={port} />
    </label>
    <label class="field-inline">
      <span class="label">{t('free_models.socks5_user')}</span>
      <input class="input input--opt" type="text" bind:value={username} />
    </label>
    <label class="field-inline">
      <span class="label">{t('free_models.socks5_pass')}</span>
      <input class="input input--opt" type="password" bind:value={password} />
    </label>
    <button class="btn btn--primary" disabled={busy === 'add-proxy'} onclick={addProxy}>
      {t('free_models.socks5_add')}
    </button>
  </div>
</Card>

<style>
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: var(--sp-3);
    margin-bottom: var(--sp-4);
  }

  .quality-section + .quality-section {
    margin-top: var(--sp-4);
    padding-top: var(--sp-3);
    border-top: 1px solid var(--border);
  }

  .quality-head {
    display: flex;
    align-items: baseline;
    gap: var(--sp-3);
    flex-wrap: wrap;
    margin-bottom: var(--sp-2);
  }

  .quality-head h4 {
    margin: 0;
    font-size: var(--fs-sm);
    font-weight: 600;
  }

  .field {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
  }

  .label {
    font-size: var(--fs-sm);
    color: var(--text-dim);
  }

  .input--narrow {
    width: 84px;
  }

  .tools {
    display: flex;
    gap: var(--sp-2);
    flex-wrap: wrap;
    margin-bottom: var(--sp-3);
  }

  .result {
    margin: 0 0 var(--sp-4);
    padding: var(--sp-2) var(--sp-3);
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    font-family: var(--font-mono);
    font-size: var(--fs-xs);
    color: var(--text-dim);
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 180px;
    overflow-y: auto;
  }

  .discovery {
    padding-top: var(--sp-3);
    border-top: 1px solid var(--border);
  }

  .row {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    flex-wrap: wrap;
    margin-bottom: var(--sp-3);
  }

  .row:last-child {
    margin-bottom: 0;
  }

  .select--country {
    width: 170px;
  }

  .proxy-list {
    max-height: 220px;
    overflow-y: auto;
    margin-bottom: var(--sp-4);
  }

  .proxy {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    flex-wrap: wrap;
    padding: var(--sp-2);
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    margin-bottom: var(--sp-2);
    font-size: var(--fs-sm);
  }

  .proxy.off {
    opacity: 0.55;
  }

  .proxy-addr {
    font-weight: 600;
    min-width: 130px;
  }

  .proxy-actions {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    margin-left: auto;
  }

  .add-form {
    display: flex;
    gap: var(--sp-3);
    flex-wrap: wrap;
    align-items: flex-end;
  }

  .field-inline {
    display: flex;
    flex-direction: column;
    gap: var(--sp-1);
  }

  .input--host {
    width: 130px;
  }

  .input--port {
    width: 80px;
  }

  .input--opt {
    width: 120px;
  }

  .muted {
    color: var(--text-faint);
  }

  .small {
    font-size: var(--fs-xs);
  }

  .mono {
    font-family: var(--font-mono);
  }
</style>
