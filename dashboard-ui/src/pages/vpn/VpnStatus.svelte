<script lang="ts">
  /**
   * Panneau de statut « Modèles Gratuits » : bascule master, mode de sortie
   * (NordVPN / WARP / Direct), politique de substitution, état temps réel
   * (stations NordVPN ou endpoints WARP), rotation, cooldown latence et
   * géolocalisation.
   *
   * L'ancienne interface rafraîchissait ce bloc sur un intervalle ; ici on
   * s'abonne aux événements SSE `vpn_event` / `geo_warning` et on re-tire
   * l'état après chaque action.
   */

  import { apiGet, apiPost, apiPut } from '$lib/api';
  import { config, reloadConfig } from '$lib/config.svelte';
  import { t } from '$lib/i18n.svelte';
  import { onEvent } from '$lib/realtime.svelte';
  import { createQuery, invalidateTag } from '$lib/query.svelte';
  import { showToast } from '$lib/toast.svelte';
  import Card from '$lib/components/Card.svelte';
  import Toggle from '$lib/components/Toggle.svelte';
  import VpnCapacity from './VpnCapacity.svelte';

  type ProxyMode = 'vpn' | 'socks5' | 'direct' | 'auto_egress';
  type ProviderId = 'nordvpn' | 'warp' | 'proxies' | 'direct';

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
    egress_providers?: string[];
    egress_strategy?: string;
    egress_weights?: Record<string, number>;
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

  const PROVIDERS: { id: ProviderId; label: string }[] = [
    { id: 'nordvpn', label: 'free_models.mode_vpn' },
    { id: 'warp', label: 'free_models.mode_socks5' },
    { id: 'proxies', label: 'free_models.mode_proxies' },
    { id: 'direct', label: 'free_models.mode_direct' }
  ];

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

  function saveEgress(
    next: ProviderId[],
    strategy: EgressStrategy = egressStrategy,
    weights: Record<string, number> = egressWeights
  ): void {
    if (next.length === 0) {
      showToast(t('free_models.provider_required'), 'warning');
      return;
    }
    localProviders = next;
    localStrategy = strategy;
    const effectiveWeights =
      strategy === 'balance' ? normalizeWeights(weights, next) : weights;
    if (strategy === 'balance') localWeights = effectiveWeights;
    void act('mode', async () => {
      await apiPost('/api/vpn/proxy-mode', {
        providers: next,
        strategy,
        weights: effectiveWeights
      });
      // Les panneaux VpnRouting/VpnServers/VpnAdvanced lisent /api/vpn-config :
      // sans invalidation, ils gardaient les sections de l'ancien mode.
      invalidateTag('vpn');
    });
  }

  function toggleProvider(id: ProviderId): void {
    const adding = !selectedProviders.includes(id);
    const next = adding
      ? [...selectedProviders, id]
      : selectedProviders.filter((p) => p !== id);
    let nextWeights = egressWeights;
    if (egressStrategy === 'balance') {
      if (adding) {
        const existingTotal = selectedProviders.reduce(
          (sum, p) => sum + (displayWeights[p] ?? 0),
          0
        );
        if (existingTotal <= 0) {
          nextWeights = equalWeights(next);
        } else {
          // Le nouveau provider prend sa part ; les autres gardent leurs
          // proportions. Évite d'ajouter un provider à 0 % par défaut.
          const share = Math.max(1, Math.round(100 / next.length));
          const scale = (100 - share) / existingTotal;
          nextWeights = normalizeWeights(
            Object.fromEntries(
              next.map((p) => [
                p,
                p === id ? share : Math.round((displayWeights[p] ?? 0) * scale)
              ])
            ),
            next
          );
        }
      } else {
        nextWeights = normalizeWeights(displayWeights, next);
      }
    }
    saveEgress(next, egressStrategy, nextWeights);
  }

  function moveProvider(id: ProviderId, delta: number): void {
    const current = [...selectedProviders];
    const index = current.indexOf(id);
    const target = index + delta;
    if (index < 0 || target < 0 || target >= current.length) return;
    [current[index], current[target]] = [current[target], current[index]];
    saveEgress(current, egressStrategy, egressWeights);
  }

  function setStrategy(strategy: EgressStrategy): void {
    if (strategy === egressStrategy) return;
    const nextWeights =
      strategy === 'balance'
        ? normalizeWeights(displayWeights, selectedProviders)
        : egressWeights;
    saveEgress(selectedProviders, strategy, nextWeights);
  }

  function equalize(): void {
    saveEgress(selectedProviders, 'balance', equalWeights(selectedProviders));
  }

  function priorityFirst(): void {
    saveEgress(selectedProviders, 'fallback', {});
  }

  function commitWeights(): void {
    saveEgress(selectedProviders, 'balance', localWeights ?? displayWeights);
  }

  function setLatency(enabled: boolean): void {
    void act('latency', () => apiPost('/api/vpn-config', { latency_rotation: { enabled } }));
  }

  function setProactiveRotation(enabled: boolean): void {
    void act('proactive', () =>
      apiPost('/api/vpn-config', { rotation_planner: { enabled } })
    );
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
  let localProviders = $state<ProviderId[] | null>(null);
  const serverProviders = $derived.by((): ProviderId[] => {
    const raw = s.egress_providers;
    if (Array.isArray(raw) && raw.length > 0) {
      const known = new Set(PROVIDERS.map((p) => p.id));
      return raw.filter((id): id is ProviderId => known.has(id as ProviderId));
    }
    if (mode === 'socks5') return ['warp'];
    if (mode === 'direct') return ['direct'];
    if (mode === 'auto_egress') return ['nordvpn', 'warp'];
    return ['nordvpn'];
  });
  const selectedProviders = $derived(localProviders ?? serverProviders);

  // L'ordre local est optimiste : le serveur peut être caché 5 s. On le
  // libère dès que la réponse correspond, pour reprendre la source serveur.
  $effect(() => {
    if (!localProviders) return;
    const raw = s.egress_providers;
    if (!Array.isArray(raw)) return;
    const server = PROVIDERS.map((p) => p.id).filter((id) => raw.includes(id));
    if (server.join(',') === localProviders.join(',')) localProviders = null;
  });
  const orderedProviders = $derived([
    ...selectedProviders.map((id) => PROVIDERS.find((p) => p.id === id)).filter((p) => p !== undefined),
    ...PROVIDERS.filter((p) => !selectedProviders.includes(p.id))
  ]);

  type EgressStrategy = 'fallback' | 'balance';
  let localStrategy = $state<EgressStrategy | null>(null);
  const serverStrategy = $derived<EgressStrategy>(
    s.egress_strategy === 'balance' ? 'balance' : 'fallback'
  );
  const egressStrategy = $derived(localStrategy ?? serverStrategy);
  let localWeights = $state<Record<string, number> | null>(null);
  const egressWeights = $derived(localWeights ?? s.egress_weights ?? {});

  $effect(() => {
    if (localStrategy && s.egress_strategy === localStrategy) localStrategy = null;
  });

  $effect(() => {
    if (!localWeights) return;
    const server = normalizeWeights(s.egress_weights ?? {}, serverProviders);
    if (JSON.stringify(server) === JSON.stringify(localWeights)) localWeights = null;
  });

  function equalWeights(providers: ProviderId[]): Record<string, number> {
    if (providers.length === 0) return {};
    const base = Math.floor(100 / providers.length);
    const remainder = 100 - base * providers.length;
    return Object.fromEntries(
      providers.map((p, i) => [p, base + (i < remainder ? 1 : 0)])
    );
  }

  function normalizeWeights(
    raw: Record<string, number>,
    providers: ProviderId[]
  ): Record<string, number> {
    if (providers.length === 0) return {};
    const values = providers.map((p) => Math.max(0, Math.min(100, Number(raw[p] ?? 0) || 0)));
    const total = values.reduce((a, b) => a + b, 0);
    if (total <= 0) return equalWeights(providers);
    const scaled = values.map((v) => (v * 100) / total);
    const floors = scaled.map((v) => Math.floor(v));
    let missing = 100 - floors.reduce((a, b) => a + b, 0);
    const order = scaled
      .map((v, i) => ({ i, frac: v - floors[i] }))
      .sort((a, b) => b.frac - a.frac);
    for (const item of order) {
      if (missing <= 0) break;
      floors[item.i] += 1;
      missing -= 1;
    }
    return Object.fromEntries(providers.map((p, i) => [p, floors[i]]));
  }

  const displayWeights = $derived(normalizeWeights(egressWeights, selectedProviders));
  const weightTotal = $derived(
    selectedProviders.reduce((sum, p) => sum + (displayWeights[p] ?? 0), 0)
  );

  function onWeightInput(id: ProviderId, value: number): void {
    const target = Math.max(0, Math.min(100, Math.round(Number(value) || 0)));
    const others = selectedProviders.filter((p) => p !== id);
    if (others.length === 0) {
      localWeights = { [id]: 100 };
      return;
    }
    const remaining = 100 - target;
    const otherTotal = others.reduce((sum, p) => sum + (displayWeights[p] ?? 0), 0);
    // Les autres gardent leurs proportions sur l'espace restant : le slider
    // suit exactement la valeur demandée et le total reste 100.
    const shares = others.map((p) => {
      const exact =
        otherTotal > 0
          ? ((displayWeights[p] ?? 0) / otherTotal) * remaining
          : remaining / others.length;
      return { p, exact, value: Math.floor(exact) };
    });
    let missing = remaining - shares.reduce((sum, s) => sum + s.value, 0);
    const byFraction = [...shares].sort((a, b) => (b.exact % 1) - (a.exact % 1));
    for (const share of byFraction) {
      if (missing <= 0) break;
      share.value += 1;
      missing -= 1;
    }
    localWeights = {
      [id]: target,
      ...Object.fromEntries(shares.map((s) => [s.p, s.value]))
    };
  }

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
    <div class="strategy" role="radiogroup" aria-label={t('free_models.egress_strategy')}>
      <button
        type="button"
        class="strategy-btn"
        class:strategy-btn--active={egressStrategy === 'fallback'}
        role="radio"
        aria-checked={egressStrategy === 'fallback'}
        disabled={busy === 'mode'}
        onclick={() => setStrategy('fallback')}
      >{t('free_models.strategy_fallback')}</button>
      <button
        type="button"
        class="strategy-btn"
        class:strategy-btn--active={egressStrategy === 'balance'}
        role="radio"
        aria-checked={egressStrategy === 'balance'}
        disabled={busy === 'mode'}
        onclick={() => setStrategy('balance')}
      >{t('free_models.strategy_balance')}</button>
    </div>
    <div
      class="modes"
      class:modes--balance={egressStrategy === 'balance'}
      role="group"
      aria-label={t('free_models.proxy_mode')}
    >
      {#each orderedProviders as provider, i (provider.id)}
        <div class="provider-row">
          {#if egressStrategy === 'fallback' && selectedProviders.includes(provider.id)}
            <span class="provider-rank">{i + 1}</span>
          {/if}
          <label
            class="mode"
            class:mode--active={selectedProviders.includes(provider.id)}
            title={provider.id === 'direct' ? t('free_models.provider_direct_warning') : ''}
          >
            <input
              type="checkbox"
              checked={selectedProviders.includes(provider.id)}
              disabled={busy === 'mode'}
              onchange={() => toggleProvider(provider.id)}
            />
            <span>{t(provider.label)}</span>
          </label>
          {#if egressStrategy === 'balance' && selectedProviders.includes(provider.id)}
            <input
              class="weight-slider"
              type="range"
              min="0"
              max="100"
              step="1"
              value={displayWeights[provider.id] ?? 0}
              disabled={busy === 'mode'}
              aria-label={`${t('free_models.weight')} ${t(provider.label)}`}
              oninput={(e) => onWeightInput(provider.id, Number(e.currentTarget.value))}
              onchange={commitWeights}
            />
            <span class="weight-value">{(displayWeights[provider.id] ?? 0)}%</span>
            {#if (displayWeights[provider.id] ?? 0) === 0}
              <span class="badge-standby">{t('free_models.standby')}</span>
            {/if}
          {/if}
          {#if selectedProviders.includes(provider.id)}
            <button
              type="button"
              class="order-btn"
              aria-label={t('free_models.provider_up')}
              title={t('free_models.provider_up')}
              disabled={busy === 'mode' || i === 0}
              onclick={() => moveProvider(provider.id, -1)}
            >↑</button>
            <button
              type="button"
              class="order-btn"
              aria-label={t('free_models.provider_down')}
              title={t('free_models.provider_down')}
              disabled={busy === 'mode' || i >= selectedProviders.length - 1}
              onclick={() => moveProvider(provider.id, 1)}
            >↓</button>
          {/if}
        </div>
      {/each}
    </div>
    <VpnCapacity />
    {#if egressStrategy === 'balance'}
      <div class="share-preview" role="img" aria-label={t('free_models.strategy_balance')}>
        {#each selectedProviders as id (id)}
          {@const weight = displayWeights[id] ?? 0}
          {#if weight > 0}
            <span
              class="share-seg share-seg--{id}"
              style={`width: ${weight}%`}
              title={`${id} ${weight}%`}
            ></span>
          {/if}
        {/each}
      </div>
      <div class="share-meta">
        <span>{t('free_models.total')} : {weightTotal} %</span>
        <button type="button" class="btn btn--sm" disabled={busy === 'mode'} onclick={equalize}>
          {t('free_models.equalize')}
        </button>
        <button type="button" class="btn btn--sm" disabled={busy === 'mode'} onclick={priorityFirst}>
          {t('free_models.priority_first')}
        </button>
      </div>
    {/if}
    <p class="hint">{t('free_models.provider_priority')}</p>
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
        {#if (mode === 'vpn' || mode === 'auto_egress')}
          <Toggle
            checked={!!s.rotation_planner_enabled}
            label={t('vpn.proactive_rotation')}
            disabled={busy === 'proactive'}
            onchange={setProactiveRotation}
          />
        {/if}
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

    {#if (mode === 'vpn' || mode === 'auto_egress') && stations.length > 0}
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
    {:else if mode === 'socks5'}
      <p class="muted small">{t('vpn.warp_stations_hint')}</p>
    {:else if mode === 'direct'}
      <p class="muted small">{t('vpn.direct_no_station')}</p>
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
      {#if (mode === 'vpn' || mode === 'auto_egress')}
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
      {/if}
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
      {#if (mode === 'vpn' || mode === 'auto_egress')}
        <div class="item">
          <span class="label">{t('vpn.switches')}</span>
          <span class="value mono">{s.vpn?.total_switches ?? 0}</span>
        </div>
      {/if}
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
    display: flex;
    flex-wrap: wrap;
    gap: var(--sp-1);
    margin-top: var(--sp-2);
  }

  .modes--balance {
    flex-direction: column;
    align-items: stretch;
    gap: var(--sp-2);
  }

  .modes--balance .provider-row {
    display: flex;
    width: 100%;
  }

  .modes--balance .mode {
    flex: 0 0 auto;
    min-width: 9.5rem;
  }

  .strategy {
    display: inline-flex;
    margin-top: var(--sp-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    overflow: hidden;
  }

  .strategy-btn {
    border: 0;
    background: var(--surface-3);
    color: var(--text-dim);
    padding: var(--sp-1) var(--sp-3);
    font-size: var(--fs-sm);
    cursor: pointer;
  }

  .strategy-btn + .strategy-btn {
    border-left: 1px solid var(--border);
  }

  .strategy-btn--active {
    background: color-mix(in srgb, var(--accent) 22%, transparent);
    color: var(--text);
    font-weight: 600;
  }

  .strategy-btn:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .provider-rank {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    height: 18px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface-3);
    color: var(--text-dim);
    font-size: var(--fs-xs);
    font-variant-numeric: tabular-nums;
  }

  .weight-slider {
    flex: 1 1 80px;
    min-width: 60px;
    max-width: 180px;
    accent-color: var(--accent, var(--text));
  }

  .weight-value {
    min-width: 34px;
    text-align: right;
    color: var(--text);
    font-size: var(--fs-xs);
    font-variant-numeric: tabular-nums;
  }

  .badge-standby {
    padding: 1px 6px;
    border: 1px solid var(--warn);
    border-radius: 999px;
    color: var(--warn);
    font-size: var(--fs-xs);
    white-space: nowrap;
  }

  .share-preview {
    display: flex;
    width: 100%;
    height: 12px;
    margin-top: var(--sp-3);
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--surface-3);
    overflow: hidden;
  }

  .share-seg {
    height: 100%;
    transition: width 0.18s ease;
  }

  .share-seg--nordvpn {
    background: #4f8cff;
  }

  .share-seg--warp {
    background: #f5a623;
  }

  .share-seg--proxies {
    background: #2ec4b6;
  }

  .share-seg--direct {
    background: #9aa0a6;
  }

  .share-meta {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--sp-2);
    margin-top: var(--sp-2);
    color: var(--text-dim);
    font-size: var(--fs-xs);
  }

  .mode {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: var(--surface-3);
    border: 1px solid var(--border);
    color: var(--text-dim);
    border-radius: var(--radius-sm);
    padding: var(--sp-1) var(--sp-3);
    font-size: var(--fs-sm);
    cursor: pointer;
  }

  .mode input {
    accent-color: var(--accent, var(--text));
    margin: 0;
  }

  .provider-row {
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }

  .order-btn {
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text-dim);
    border-radius: var(--radius-sm);
    width: 24px;
    height: 24px;
    padding: 0;
    cursor: pointer;
    font-size: var(--fs-xs);
  }

  .order-btn:disabled {
    opacity: 0.35;
    cursor: default;
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
