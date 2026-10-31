<script lang="ts">
  /**
   * Section « routage VPN » de la page Modèles Gratuits : technologie
   * (WireGuard/OpenVPN), choix du serveur, stations parallèles, routage des
   * stations free, et retry transparent.
   *
   * Comme dans l'ancienne interface, chaque changement de select est persisté
   * immédiatement (POST `/api/vpn-config`) — seuls le nombre de stations et le
   * cooldown ont un bouton « Appliquer ».
   */

  import { apiGet, apiPost } from '$lib/api';
  import { t } from '$lib/i18n.svelte';
  import { createQuery } from '$lib/query.svelte';
  import { showToast } from '$lib/toast.svelte';
  import Card from '$lib/components/Card.svelte';

  interface VpnConfig {
    vpn_stack?: string;
    server_pick_mode?: string;
    station_count?: number | string;
    strict_free?: boolean;
    on_429_action?: string;
    station_bad_ttl_s?: number;
    auto_max_free_attempts?: boolean;
    max_free_attempts?: number;
    free_exception_fallback?: string;
    free_parallel?: {
      enabled?: boolean;
      routing?: string;
      mode?: string;
      hedge_delay_ms?: number;
    };
    [key: string]: unknown;
  }

  interface VpnConfigResponse {
    config?: VpnConfig;
    error?: string;
  }

  interface RetryConfig {
    enabled?: boolean;
    max_wait_s?: number;
    error?: string;
  }

  const STATION_MAX = 10;
  const COOLDOWN_MIN = 1;
  const COOLDOWN_MAX = 3600;

  const vpnQ = createQuery<VpnConfig>({
    load: (signal) => apiGet<VpnConfig>('/api/vpn-config', signal),
    tags: () => ['vpn'],
    initial: {}
  });

  const retryQ = createQuery<RetryConfig>({
    load: (signal) => apiGet<RetryConfig>('/api/retry-config', signal),
    tags: () => ['vpn'],
    initial: {}
  });

  /**
   * `GET /api/vpn-config` renvoie la configuration **à plat** (71 clés) — le
   * repli `{config}` n'existe que sur la réponse du POST.
   */
  const cfg = $derived(vpnQ.data);
  const parallel = $derived(cfg?.free_parallel ?? {});
  const retry = $derived(retryQ.data ?? {});

  let stationCount = $state('1');
  let cooldown = $state('60');
  let seeded = false;

  /**
   * Copie une seule fois les valeurs serveur, sans écraser la saisie en cours.
   * `initial: {}` est truthy : on attend que l'objet soit réellement rempli
   * (sinon on figerait les valeurs par défaut avant la première réponse).
   */
  $effect(() => {
    if (!cfg || Object.keys(cfg).length === 0 || seeded) return;
    seeded = true;
    stationCount = String(cfg.station_count ?? 1);
    cooldown = String(cfg.station_bad_ttl_s ?? 60);
  });

  let saving = $state<string | null>(null);

  /** POST partiel sur `/api/vpn-config`, puis relecture de la config. */
  async function patch(field: string, payload: Record<string, unknown>): Promise<void> {
    if (saving) return;
    saving = field;
    try {
      const res = await apiPost<VpnConfigResponse>('/api/vpn-config', payload);
      if (res.error) throw new Error(res.error);
      await vpnQ.refresh(true);
    } catch (e) {
      showToast(e instanceof Error ? e.message : String(e), 'error');
    } finally {
      saving = null;
    }
  }

  async function patchRetry(payload: Record<string, unknown>): Promise<void> {
    if (saving) return;
    saving = 'retry';
    try {
      const res = await apiPost<RetryConfig>('/api/retry-config', payload);
      if (res?.error) throw new Error(res.error);
      await retryQ.refresh(true);
    } catch (e) {
      showToast(e instanceof Error ? e.message : String(e), 'error');
    } finally {
      saving = null;
    }
  }

  function applyStations(): void {
    const n = Number(stationCount);
    if (!Number.isFinite(n) || n < 1 || n > STATION_MAX) return;
    void patch('stations', { station_count: n });
  }

  function applyCooldown(): void {
    const n = Number(cooldown);
    if (!Number.isFinite(n) || n < COOLDOWN_MIN || n > COOLDOWN_MAX) {
      showToast(t('vpn.cooldown_range'), 'warning');
      return;
    }
    void patch('cooldown', { station_bad_ttl_s: n });
  }

  /** Statut lisible du routage free (« ON · round-robin · hedge » / « OFF »). */
  const parallelStatus = $derived(
    parallel.enabled
      ? `${t('vpn.on')} · ${parallel.routing ?? 'round-robin'} · ${parallel.mode ?? 'load-balance'}`
      : t('vpn.off')
  );

  const retryStatus = $derived(
    retry.enabled
      ? Number(retry.max_wait_s) > 0
        ? `${t('vpn.on')} — max ${retry.max_wait_s}s`
        : `${t('vpn.on')} — ${t('vpn.infinite')}`
      : t('vpn.off')
  );

  const showCooldown = $derived((cfg?.on_429_action ?? 'both') === 'both');
</script>

<Card title={t('vpn.routing_title')}>
  <div class="panel">
    <div class="row">
      <strong class="row-title">{t('vpn.stack.title')}</strong>
      <select
        class="select row-control"
        value={cfg?.vpn_stack ?? 'auto'}
        disabled={saving === 'stack'}
        onchange={(e) => patch('stack', { vpn_stack: e.currentTarget.value })}
      >
        <option value="auto">{t('vpn.stack.auto')}</option>
        <option value="wireguard">{t('vpn.stack.wireguard')}</option>
        <option value="openvpn">{t('vpn.stack.openvpn')}</option>
      </select>
      <button
        class="btn btn--sm"
        disabled={saving === 'stack'}
        onclick={() => patch('stack', { vpn_stack: cfg?.vpn_stack ?? 'auto' })}
      >
        {t('common.save')}
      </button>
    </div>
    <p class="muted small">{t('vpn.stack.hint')}</p>
  </div>

  <div class="panel">
    <div class="row">
      <strong class="row-title">{t('vpn.pick_title')}</strong>
      <select
        class="select row-control row-control--wide"
        value={cfg?.server_pick_mode ?? 'proxy'}
        disabled={saving === 'pick'}
        onchange={(e) => patch('pick', { server_pick_mode: e.currentTarget.value })}
      >
        <option value="proxy">{t('vpn.pick_proxy')}</option>
        <option value="gluetun">{t('vpn.pick_gluetun')}</option>
      </select>
      <button
        class="btn btn--sm"
        disabled={saving === 'pick'}
        onclick={() => patch('pick', { server_pick_mode: cfg?.server_pick_mode ?? 'proxy' })}
      >
        {t('common.save')}
      </button>
    </div>
    <p class="muted small">{t('vpn.pick_hint')}</p>
  </div>

  <div class="panel">
    <div class="row">
      <strong class="row-title">{t('vpn.stations.title')}</strong>
      <div class="row-actions">
        <select class="select row-control row-control--narrow" bind:value={stationCount}>
          {#each Array.from({ length: STATION_MAX }, (_, i) => String(i + 1)) as n (n)}
            <option value={n}>{n}</option>
          {/each}
        </select>
        <button class="btn btn--sm" disabled={saving === 'stations'} onclick={applyStations}>
          {saving === 'stations' ? t('common.working') : t('vpn.stations.save')}
        </button>
      </div>
    </div>
    <p class="muted small">{t('vpn.stations.hint')}</p>

    <div class="row">
      <span class="label">{t('vpn.exhaust_mode')}</span>
      <select
        class="select row-control row-control--wide"
        value={cfg?.strict_free ? 'strict' : 'fallback'}
        onchange={(e) => patch('exhaust', { strict_free: e.currentTarget.value === 'strict' })}
      >
        <option value="fallback">{t('vpn.exhaust_fallback')}</option>
        <option value="strict">{t('vpn.exhaust_strict')}</option>
      </select>
    </div>

    <div class="row">
      <span class="label">{t('vpn.on_429')}</span>
      <select
        class="select row-control row-control--wide"
        value={cfg?.on_429_action ?? 'both'}
        onchange={(e) => patch('on429', { on_429_action: e.currentTarget.value })}
      >
        <option value="both">{t('vpn.on_429_both')}</option>
        <option value="cooldown">{t('vpn.on_429_cooldown')}</option>
        <option value="rotate">{t('vpn.on_429_rotate')}</option>
      </select>
    </div>

    {#if showCooldown}
      <div class="row">
        <span class="label">{t('vpn.cooldown_s')}</span>
        <input
          class="input input--narrow"
          type="number"
          min={COOLDOWN_MIN}
          max={COOLDOWN_MAX}
          bind:value={cooldown}
        />
        <button class="btn btn--sm" disabled={saving === 'cooldown'} onclick={applyCooldown}>
          {t('vpn.stations.save')}
        </button>
        <span class="muted small">{t('vpn.cooldown_only_visible')}</span>
      </div>
    {/if}

    <div class="row">
      <span class="label">{t('vpn.free_attempts')}</span>
      <select
        class="select row-control row-control--wide"
        value={cfg?.auto_max_free_attempts === false ? String(cfg.max_free_attempts ?? 2) : 'auto'}
        onchange={(e) =>
          patch(
            'attempts',
            e.currentTarget.value === 'auto'
              ? { auto_max_free_attempts: true }
              : {
                  auto_max_free_attempts: false,
                  max_free_attempts: Number(e.currentTarget.value) || 2
                }
          )}
      >
        <option value="auto">{t('vpn.free_attempts_auto')}</option>
        {#each ['1', '2', '3'] as n (n)}
          <option value={n}>{t('vpn.free_attempts_n', { n })}</option>
        {/each}
      </select>
    </div>

    <div class="row">
      <span class="label">{t('vpn.exception_mode')}</span>
      <select
        class="select row-control row-control--wide"
        value={cfg?.free_exception_fallback ?? 'station-first'}
        onchange={(e) => patch('exception', { free_exception_fallback: e.currentTarget.value })}
      >
        <option value="station-first">{t('vpn.exception_station')}</option>
        <option value="direct">{t('vpn.exception_direct')}</option>
      </select>
    </div>

    <div class="panel panel--inner">
      <div class="row">
        <label class="check">
          <input
            type="checkbox"
            checked={!!parallel.enabled}
            onchange={(e) =>
              patch('parallel', {
                free_parallel: {
                  enabled: e.currentTarget.checked,
                  routing: parallel.routing ?? 'round-robin',
                  mode: parallel.mode ?? 'load-balance',
                  hedge_delay_ms: parallel.hedge_delay_ms ?? 1500
                }
              })}
          />
          <span>{t('config.free_parallel')}</span>
        </label>
        <span class="muted small">{parallelStatus}</span>
      </div>

      {#if parallel.enabled}
        <div class="row">
          <span class="label">{t('config.free_routing')}</span>
          <select
            class="select row-control row-control--narrow"
            value={parallel.routing ?? 'round-robin'}
            onchange={(e) =>
              patch('parallel', {
                free_parallel: {
                  enabled: true,
                  routing: e.currentTarget.value,
                  mode: parallel.mode ?? 'load-balance',
                  hedge_delay_ms: parallel.hedge_delay_ms ?? 1500
                }
              })}
          >
            <option value="round-robin">{t('config.free_routing_rr')}</option>
            <option value="failover">{t('config.free_routing_failover')}</option>
          </select>

          <select
            class="select row-control row-control--wide"
            value={parallel.mode ?? 'load-balance'}
            onchange={(e) =>
              patch('parallel', {
                free_parallel: {
                  enabled: true,
                  routing: parallel.routing ?? 'round-robin',
                  mode: e.currentTarget.value,
                  hedge_delay_ms: parallel.hedge_delay_ms ?? 1500
                }
              })}
          >
            <option value="load-balance">{t('vpn.load_balance')}</option>
            <option value="strict">{t('vpn.strict_counter')}</option>
            <option value="hedge">{t('vpn.hedge')}</option>
          </select>

          <span class="label label--inline">{t('config.free_hedge_delay')}</span>
          <input
            class="input input--narrow"
            type="number"
            min="0"
            max="2000"
            step="100"
            value={String(parallel.hedge_delay_ms ?? 1500)}
            onchange={(e) =>
              patch('parallel', {
                free_parallel: {
                  enabled: true,
                  routing: parallel.routing ?? 'round-robin',
                  mode: parallel.mode ?? 'load-balance',
                  hedge_delay_ms: Math.max(0, Math.min(2000, Number(e.currentTarget.value) || 0))
                }
              })}
          />
        </div>
      {/if}

      <p class="muted small">{t('config.free_parallel_hint')}</p>
    </div>

    <div class="panel panel--inner">
      <div class="row">
        <label class="check">
          <input
            type="checkbox"
            checked={!!retry.enabled}
            onchange={(e) => patchRetry({ enabled: e.currentTarget.checked })}
          />
          <span>{t('config.retry_until_success')}</span>
        </label>
        <span class="muted small">{retryStatus}</span>
      </div>

      {#if retry.enabled}
        <div class="row">
          <span class="label">{t('config.retry_max_wait')}</span>
          <input
            class="input input--narrow"
            type="number"
            min="0"
            max="86400"
            step="30"
            value={String(retry.max_wait_s ?? 900)}
            onchange={(e) =>
              patchRetry({
                max_wait_s: Math.max(0, Math.min(86400, Number(e.currentTarget.value) || 0))
              })}
          />
          <span class="muted small">{t('config.retry_max_wait_hint')}</span>
        </div>
      {/if}

      <p class="muted small">{t('config.retry_hint')}</p>
    </div>
  </div>
</Card>

<style>
  .panel {
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: var(--surface-2);
    padding: var(--sp-3);
    margin-bottom: var(--sp-3);
  }

  .panel--inner {
    background: var(--surface);
    margin-top: var(--sp-3);
    margin-bottom: 0;
  }

  .row {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    flex-wrap: wrap;
    margin-bottom: var(--sp-2);
  }

  .row:last-child {
    margin-bottom: 0;
  }

  .row-title {
    font-size: var(--fs-md);
    color: var(--text);
  }

  .row-actions {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    margin-left: auto;
  }

  .row-control {
    width: 180px;
    max-width: 100%;
  }

  .row-control--wide {
    width: 240px;
  }

  .row-control--narrow {
    width: 90px;
  }

  .label {
    font-size: var(--fs-sm);
    color: var(--text-dim);
    min-width: 150px;
  }

  .label--inline {
    min-width: 0;
    margin-left: var(--sp-2);
  }

  .check {
    display: inline-flex;
    align-items: center;
    gap: var(--sp-2);
    font-size: var(--fs-sm);
    cursor: pointer;
    user-select: none;
  }

  .muted {
    color: var(--text-faint);
  }

  .small {
    font-size: var(--fs-xs);
    line-height: 1.5;
    margin: var(--sp-1) 0 0;
  }

  .input--narrow {
    width: 100px;
  }
</style>
