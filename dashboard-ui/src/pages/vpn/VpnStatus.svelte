<script lang="ts">
  /**
   * Panneau de statut « Modèles Gratuits » : bascule master, mode de sortie
   * (VPN / SOCKS5 / direct), politique de substitution, état temps réel du VPN
   * (stations, rotation, cooldown latence) et géolocalisation.
   *
   * L'ancienne interface rafraîchissait ce bloc sur un intervalle ; ici on
   * s'abonne aux événements SSE `vpn_event` / `geo_warning` et on re-tire
   * l'état après chaque action.
   */

  import { apiGet, apiPost, apiPut } from '$lib/api';
  import { config, reloadConfig } from '$lib/config.svelte';
  import { t } from '$lib/i18n.svelte';
  import { onEvent } from '$lib/realtime.svelte';
  import { createQuery } from '$lib/query.svelte';
  import { showToast } from '$lib/toast.svelte';
  import Card from '$lib/components/Card.svelte';
  import Toggle from '$lib/components/Toggle.svelte';

  type ProxyMode = 'vpn' | 'socks5' | 'direct';

  interface Station {
    station?: number;
    name?: string;
    hostname?: string;
    ip?: string;
    current_ip?: string;
    vpn_status?: string;
    requests?: number;
    requests_this_ip?: number;
    error_detail?: string;
    station_usable?: boolean;
    model_usable?: boolean;
    model_usable_reason?: string | null;
    non_routable_reason?: string | null;
    egress_state?: string;
    ip_state?: string;
    quota_headroom?: number | null;
    quota_estimate?: number | null;
    quality_score?: number | null;
    health?: { state?: string; last_error?: string | null; action_required?: boolean };
  }

  interface VpnStatus {
    status?: string;
    enabled?: boolean;
    proxy_mode?: string;
    latency_enabled?: boolean;
    healthy?: number | null;
    total?: number | null;
    healthy_routable?: number | null;
    degraded_routable?: number | null;
    stale?: boolean;
    refresh_error?: string;
    refreshed_at?: string;
    boot_error?: string;
    docker_daemon?: { reachable?: boolean };
    env_divergence?: { key?: string }[];
    config_yaml_dirty?: boolean;
    geo_warning?: string;
    current_ip?: string;
    current_server?: string;
    requests_this_ip?: number;
    quota_per_ip?: number;
    total_free_requests?: number;
    ips_used?: number;
    stations?: Station[];
    vpn?: {
      current_identity?: { impersonate?: string; user_agent?: string | null };
      identity_index?: number;
      total_switches?: number;
    };
    wire_identity?: { version?: string; client?: string };
    [key: string]: unknown;
  }

  interface GeoPolicies {
    enabled?: boolean;
    allow_direct_when_compatible?: boolean;
    version?: number;
  }

  const MODES: ProxyMode[] = ['vpn', 'socks5', 'direct'];

  const statusQ = createQuery<VpnStatus>({
    load: (signal) => apiGet<VpnStatus>('/api/vpn-status', signal),
    tags: () => ['vpn'],
    initial: {}
  });

  const geoQ = createQuery<GeoPolicies>({
    load: (signal) => apiGet<GeoPolicies>('/api/geo-policies', signal),
    tags: () => ['vpn'],
    initial: {}
  });

  const s = $derived(statusQ.data ?? {});
  const geo = $derived(geoQ.data ?? {});
  const stations = $derived(s.stations ?? []);
  const capacity = $derived({
    configured: Number(s.configured_stations ?? s.total ?? stations.length ?? 0),
    usable: Number(s.usable_stations ?? s.healthy_routable ?? 0),
    withQuota: Number(s.active_stations_with_quota ?? s.usable_stations ?? 0),
    degraded: Boolean(s.capacity_degraded),
    reason: String(s.capacity_reason ?? ''),
    spare: Number(s.spare_station ?? 0),
    warm: Boolean(s.warm_spare_ready),
    promoted: Boolean(s.spare_promoted)
  });

  let busy = $state<string | null>(null);
  let substitutionStatus = $state('');

  /**
   * `free_substitution_policy` est exposé par `GET /api/config` (lecture
   * live). La valeur locale reste la source immédiate du select ; le serveur
   * la confirme après chaque POST.
   */
  let substitutionPolicy = $state('homonym');

  $effect(() => {
    const p = config.data?.free_substitution_policy;
    if (p) substitutionPolicy = p;
  });

  /** Recharge les deux panneaux (appelé après chaque action et sur événement). */
  async function reload(): Promise<void> {
    await Promise.all([statusQ.refresh(true), geoQ.refresh(true)]);
  }

  // Le backend signale les changements VPN/geo par SSE : inutile de poller.
  onEvent('vpn_event', () => void reload());
  onEvent('geo_warning', () => void reload());

  async function act(field: string, run: () => Promise<unknown>): Promise<void> {
    if (busy) return;
    busy = field;
    try {
      await run();
      await reload();
    } catch (e) {
      showToast(e instanceof Error ? e.message : String(e), 'error');
    } finally {
      busy = null;
    }
  }

  function setEnabled(enabled: boolean): void {
    void act('enabled', () => apiPost('/api/vpn/toggle', { enabled }));
  }

  function setMode(mode: ProxyMode): void {
    void act('mode', () => apiPost('/api/vpn/proxy-mode', { mode }));
  }

  function setLatency(enabled: boolean): void {
    void act('latency', () => apiPost('/api/vpn-config', { latency_rotation: { enabled } }));
  }

  function setQuota(value: number): void {
    const n = Number(value) || 300;
    void act('quota', () => apiPost('/api/vpn-config', { quota_per_ip: n }));
  }

  async function setSubstitutionPolicy(value: string): Promise<void> {
    if (busy) return;
    const previous = substitutionPolicy;
    busy = 'substitution';
    substitutionPolicy = value;
    substitutionStatus = '…';
    try {
      const res = await apiPost<{ error?: string }>('/api/vpn-config', {
        free_substitution_policy: value
      });
      if (res?.error) throw new Error(res.error);
      await reloadConfig();
      substitutionStatus = t('config.cr_saved');
      window.setTimeout(() => (substitutionStatus = ''), 2500);
    } catch (e) {
      substitutionPolicy = previous;
      substitutionStatus = '';
      showToast(e instanceof Error ? e.message : String(e), 'error');
    } finally {
      busy = null;
    }
  }

  function toggleGeo(): void {
    void act('geo', async () => {
      const cur = await apiGet<GeoPolicies>('/api/geo-policies');
      await apiPut('/api/geo-policies', {
        enabled: !cur.enabled,
        version: cur.version
      });
    });
  }

  function toggleGeoDirect(): void {
    void act('geo-direct', async () => {
      const cur = await apiGet<GeoPolicies>('/api/geo-policies');
      await apiPut('/api/geo-policies', {
        allow_direct_when_compatible: !cur.allow_direct_when_compatible,
        version: cur.version,
        enabled: cur.enabled
      });
    });
  }

  function rollbackGeo(): void {
    void act('geo-rollback', () => apiPost('/api/geo-policies/rollback'));
  }

  /** Libellé d'état agrégé : N/M healthy + routable + dégradées + alertes. */
  const statusLabel = $derived.by(() => {
    const map: Record<string, string> = {
      connected: t('vpn.connected'),
      degraded: t('vpn.degraded'),
      connecting: t('vpn.connecting'),
      disconnected: t('vpn.disconnected'),
      error: t('vpn.error')
    };
    let label = map[s.status ?? ''] ?? map.disconnected;

    let healthy = s.healthy;
    let total = s.total;
    if ((healthy == null || total == null) && stations.length > 0) {
      total = stations.length;
      healthy = stations.filter(
        (st) => st.vpn_status === 'connected' || st.vpn_status === 'degraded'
      ).length;
    }
    if (healthy != null && total != null) label += ` — ${healthy}/${total} healthy`;
    if (s.healthy_routable != null && s.healthy_routable !== healthy && total != null) {
      label += ` (${s.healthy_routable}/${total} routable)`;
    }
    if ((s.degraded_routable ?? 0) > 0) {
      const n = s.degraded_routable as number;
      label += ` (~${n} ${t(n > 1 ? 'vpn.degraded_pl' : 'vpn.degraded')})`;
    }
    if (s.stale) label += ' ⟳';
    if (s.boot_error) label += ` ⚠ ${s.boot_error}`;
    if (s.docker_daemon && s.docker_daemon.reachable === false) {
      label += ` ⚠ ${t('vpn.docker_down')}`;
    }
    return label;
  });

  const statusColor = $derived(
    s.status === 'connected'
      ? 'var(--success)'
      : s.status === 'error'
        ? 'var(--danger)'
        : s.status === 'degraded' || s.status === 'connecting'
          ? 'var(--warn)'
          : 'var(--text-dim)'
  );

  const statusTitle = $derived(
    s.boot_error
      ? s.boot_error
      : s.refresh_error
        ? String(s.refresh_error)
        : (s.stale ? `stale — ${s.refreshed_at ?? ''}` : '')
  );

  /** Identité affichée : client OpenCode officiel. Priorité au wire réel
   *  (vault par IP), sinon UA officiel du profil geo. Plus de navigateur. */
  const identityLabel = $derived.by(() => {
    const ident = s.vpn?.current_identity;
    const wire = s.wire_identity;
    const official =
      typeof ident?.user_agent === 'string' && ident.user_agent ? ident.user_agent : '';
    if (wire && wire.version) {
      return `${wire.version}/${wire.client} #${s.vpn?.identity_index ?? ''}`;
    }
    return official ? `${official} #${s.vpn?.identity_index ?? ''}` : '—';
  });

  const mode = $derived((s.proxy_mode ?? 'vpn') as ProxyMode);

  /** Carte payant → free configurée, avec le verdict de la politique active.
   *  Une entrée absente de `free_model_map_effective` est ignorée (grisée) :
   *  aucune substitution cachée. */
  const substitutionRows = $derived.by(() => {
    const raw = config.data?.free_model_map ?? {};
    const effective = config.data?.free_model_map_effective ?? {};
    return Object.entries(raw)
      .filter(([from, to]) => Boolean(from && to))
      .map(([from, to]) => ({ from, to, active: effective[from] === to }));
  });

  const envKeys = $derived((s.env_divergence ?? []).map((d) => d.key).filter(Boolean));
</script>

<Card title={t('free_models.title')} hint={t('free_models.hint')}>
  <!-- Bascule master -->
  <div class="block">
    <Toggle
      checked={!!s.enabled}
      label={t('free_models.enable')}
      disabled={busy === 'enabled'}
      onchange={setEnabled}
    />
  </div>

  <!-- Mode de sortie -->
  <div class="block">
    <span class="block-title">{t('free_models.proxy_mode')}</span>
    <div class="modes" role="radiogroup" aria-label={t('free_models.proxy_mode')}>
      {#each MODES as m (m)}
        <button
          class="mode"
          class:mode--active={mode === m}
          role="radio"
          aria-checked={mode === m}
          disabled={busy === 'mode'}
          onclick={() => setMode(m)}
        >
          {t(`free_models.mode_${m}`)}
        </button>
      {/each}
    </div>
    {#if s.geo_warning}
      <p class="warn-box">{s.geo_warning}</p>
    {/if}
  </div>

  <!-- Politique de substitution -->
  <div class="block">
    <div class="row">
      <span class="block-title">{t('free_models.substitution')}</span>
      <select
        class="select"
        value={substitutionPolicy}
        disabled={busy === 'substitution'}
        onchange={(e) => void setSubstitutionPolicy(e.currentTarget.value)}
      >
        <option value="homonym">{t('free_models.substitution_homonym')}</option>
        <option value="family">{t('free_models.substitution_family')}</option>
      </select>
      <span class="muted small">{substitutionStatus}</span>
    </div>
    <p class="hint">{t('free_models.substitution_hint')}</p>
    {#if substitutionRows.length > 0}
      <div class="map">
        {#each substitutionRows as row (row.from)}
          <span class="map-row mono" class:inactive={!row.active}>
            {row.from} → {row.to}
            {#if !row.active}
              <span class="muted">({t('free_models.substitution_ignored')})</span>
            {/if}
          </span>
        {/each}
      </div>
    {/if}
  </div>

  <!-- Statut temps réel -->
  <div class="status">
    <div class="status-head">
      <div class="status-left">
        <span
          class="dot"
          style="background: {statusColor}; opacity: {s.stale ? 0.45 : 1}"
          title={statusTitle}
        ></span>
        <strong style="color: {statusColor}" title={statusTitle}>{statusLabel}</strong>
      </div>

      <div class="rotations">
        <Toggle
          checked={!!s.enabled}
          label={t('vpn.rotation')}
          disabled={busy === 'enabled'}
          onchange={setEnabled}
        />
        <Toggle
          checked={!!s.latency_enabled}
          label={t('vpn.latency_cooldown')}
          disabled={busy === 'latency'}
          onchange={setLatency}
        />
      </div>
    </div>

    <div class="capacity" class:capacity--degraded={capacity.degraded}>
      <span class="capacity-label">{t('vpn.capacity')}</span>
      <strong class="mono">{capacity.usable}/{capacity.configured}</strong>
      <span class="muted small">{t('vpn.capacity_quota')} {capacity.withQuota}/{capacity.configured}</span>
      {#if capacity.spare > 0}
        <span class="badge" class:badge--ok={capacity.warm} class:badge--warn={!capacity.warm}>
          {t('vpn.spare')} #{capacity.spare} · {capacity.warm ? t('vpn.spare_warm') : t('vpn.spare_cold')}
          {#if capacity.promoted} · {t('vpn.spare_promoted')}{/if}
        </span>
      {/if}
      {#if capacity.reason}
        <span class="muted small">{capacity.reason}</span>
      {/if}
    </div>

    {#if stations.length > 0}
      <div class="stations">
        {#each stations as st, i (st.station ?? i)}
          <div class="station" class:station--off={st.station_usable === false} title={st.error_detail ?? st.model_usable_reason ?? ''}>
            <span
              class="dot dot--sm"
              style="background: {st.station_usable
                ? 'var(--success)'
                : st.vpn_status === 'degraded'
                  ? 'var(--warn)'
                  : 'var(--text-dim)'}"
            ></span>
            <span class="station-name">#{st.station ?? i + 1}</span>
            <span class="muted mono small">{st.current_ip ?? st.ip ?? '—'}</span>
            <span class="badge" class:badge--ok={st.station_usable} class:badge--warn={!st.station_usable}>
              {st.station_usable ? t('vpn.station_usable') : (st.non_routable_reason ?? t('vpn.station_unusable'))}
            </span>
            {#if st.quota_headroom != null}
              <span class="muted small mono">
                {t('vpn.quota_remaining')} {st.quota_headroom}/{st.quota_estimate ?? '—'}
              </span>
            {/if}
            {#if st.ip_state}
              <span class="badge badge--dim">{st.ip_state}</span>
            {/if}
          </div>
        {/each}
      </div>
    {/if}

    {#if envKeys.length > 0}
      <p class="warn-box">⚠ <strong>{t('vpn.env_stale')}</strong> — {envKeys.join(', ')}</p>
    {/if}
    {#if s.config_yaml_dirty}
      <p class="error-box">{t('vpn.config_dirty')}</p>
    {/if}

    <div class="geo-controls">
      <button class="btn btn--sm" onclick={toggleGeo} disabled={busy === 'geo'}>
        🌍 {t('vpn.geo_toggle')}: {geo.enabled ? t('vpn.geo_on') : t('vpn.geo_off')}
      </button>
      <button class="btn btn--sm" onclick={rollbackGeo} disabled={busy === 'geo-rollback'}>
        ↩ {t('vpn.geo_rollback')}
      </button>
      <button class="btn btn--sm" onclick={toggleGeoDirect} disabled={busy === 'geo-direct'}>
        {t('vpn.geo_direct')}:
        {geo.allow_direct_when_compatible ? t('vpn.geo_on') : t('vpn.geo_off')}
      </button>
    </div>

    <div class="grid">
      <div class="item">
        <span class="label">{t('vpn.current_ip')}</span>
        <span class="value mono">{s.current_ip ?? '—'}</span>
      </div>
      <div class="item">
        <span class="label">{t('vpn.identity')}</span>
        <span class="value mono">{identityLabel}</span>
      </div>
      <div class="item">
        <span class="label">{t('vpn.current_server')}</span>
        <span class="value">{s.current_server ?? '—'}</span>
      </div>
      <div class="item">
        <span class="label">{t('vpn.country')}</span>
        <span class="value">{s.current_country ?? '—'}</span>
      </div>
      <div class="item">
        <span class="label">{t('vpn.requests_this_ip')}</span>
        <span class="value mono">{s.requests_this_ip ?? 0} / {s.quota_per_ip ?? 300}</span>
      </div>
      <div class="item">
        <span class="label">{t('vpn.total_free')}</span>
        <span class="value mono">{s.total_free_requests ?? 0}</span>
      </div>
      <div class="item">
        <span class="label">{t('vpn.ips_used')}</span>
        <span class="value mono">{s.ips_used ?? 0}</span>
      </div>
      <div class="item">
        <span class="label">{t('vpn.switches')}</span>
        <span class="value mono">{s.vpn?.total_switches ?? 0}</span>
      </div>
      <div class="item">
        <span class="label">{t('vpn.quota_per_ip')}</span>
        <input
          class="input input--narrow"
          type="number"
          min="1"
          value={String(s.quota_per_ip ?? 300)}
          disabled={busy === 'quota'}
          onchange={(e) => setQuota(Number(e.currentTarget.value))}
        />
      </div>
    </div>
  </div>
</Card>

<style>
  .block {
    padding: var(--sp-3);
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    margin-bottom: var(--sp-3);
  }

  .block-title {
    font-weight: 600;
    font-size: var(--fs-sm);
    color: var(--text);
    margin-right: var(--sp-3);
  }

  .row {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    flex-wrap: wrap;
  }

  .modes {
    display: inline-flex;
    gap: var(--sp-1);
    margin-top: var(--sp-2);
  }

  .mode {
    background: var(--surface-3);
    border: 1px solid var(--border);
    color: var(--text-dim);
    border-radius: var(--radius-sm);
    padding: var(--sp-1) var(--sp-3);
    font-size: var(--fs-sm);
    cursor: pointer;
  }

  .mode--active {
    background: color-mix(in srgb, var(--accent) 22%, transparent);
    border-color: var(--accent);
    color: var(--text);
  }

  .hint {
    margin: var(--sp-2) 0 0;
    color: var(--text-dim);
    font-size: var(--fs-xs);
    line-height: 1.5;
  }

  .map {
    display: flex;
    flex-wrap: wrap;
    gap: var(--sp-1) var(--sp-3);
    margin-top: var(--sp-2);
    font-size: var(--fs-xs);
  }

  .map-row {
    color: var(--text-dim);
  }

  .map-row.inactive {
    opacity: 0.55;
  }

  .status {
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: var(--surface-2);
    padding: var(--sp-3);
  }

  .status-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--sp-4);
    flex-wrap: wrap;
    margin-bottom: var(--sp-3);
  }

  .capacity {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    flex-wrap: wrap;
    padding: var(--sp-2) var(--sp-3);
    margin-bottom: var(--sp-3);
    border: 1px solid var(--border);
    border-left: 3px solid var(--success);
    border-radius: var(--radius-sm);
    background: var(--surface-2);
    font-size: var(--fs-sm);
  }

  .capacity--degraded {
    border-left-color: var(--warn);
  }

  .capacity-label {
    font-weight: 600;
    color: var(--text);
  }

  .badge {
    display: inline-flex;
    align-items: center;
    padding: 0 var(--sp-2);
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface-3);
    color: var(--text-dim);
    font-size: var(--fs-xs);
    white-space: nowrap;
  }

  .badge--ok {
    border-color: color-mix(in srgb, var(--success) 45%, var(--border));
    color: var(--success);
  }

  .badge--warn {
    border-color: color-mix(in srgb, var(--warn) 50%, var(--border));
    color: var(--warn);
  }

  .badge--dim {
    opacity: 0.75;
  }

  .status-left {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    min-width: 0;
  }

  .rotations {
    display: flex;
    gap: var(--sp-4);
    flex-wrap: wrap;
  }

  .dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    display: inline-block;
    flex: none;
    background: var(--text-dim);
  }

  .dot--sm {
    width: 8px;
    height: 8px;
  }

  .stations {
    display: flex;
    flex-wrap: wrap;
    gap: var(--sp-2);
    margin-bottom: var(--sp-3);
  }

  .station {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    padding: var(--sp-1) var(--sp-2);
    background: var(--surface-3);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    font-size: var(--fs-xs);
  }

  .station-name {
    font-weight: 600;
  }

  .station--off {
    opacity: 0.62;
  }

  .warn-box {
    margin: var(--sp-2) 0 0;
    padding: var(--sp-2) var(--sp-3);
    background: color-mix(in srgb, var(--warn) 14%, transparent);
    border: 1px solid var(--warn);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-size: var(--fs-xs);
    line-height: 1.5;
  }

  .error-box {
    margin: var(--sp-2) 0 0;
    padding: var(--sp-2) var(--sp-3);
    background: color-mix(in srgb, var(--danger) 14%, transparent);
    border: 1px solid var(--danger);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-size: var(--fs-xs);
  }

  .geo-controls {
    display: flex;
    gap: var(--sp-2);
    flex-wrap: wrap;
    margin-bottom: var(--sp-3);
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: var(--sp-2) var(--sp-4);
  }

  .item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--sp-2);
    padding: var(--sp-1) 0;
    border-bottom: 1px dashed var(--border);
  }

  .label {
    font-size: var(--fs-xs);
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .value {
    font-size: var(--fs-sm);
    color: var(--text);
    text-align: right;
    word-break: break-all;
  }

  .mono {
    font-family: var(--font-mono);
  }

  .muted {
    color: var(--text-faint);
  }

  .small {
    font-size: var(--fs-xs);
  }

  .input--narrow {
    width: 84px;
    text-align: right;
  }
</style>
