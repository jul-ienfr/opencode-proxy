<script lang="ts">
  /**
   * Page Configuration : état du proxy, mapping de modèles, routes
   * personnalisées, réglages web_search / web_fetch, clés API et modèles
   * disponibles.
   *
   * Tous les tableaux éditables (routes custom, clés API) vivent dans un
   * brouillon local : le serveur n'est touché qu'au clic sur « Save », comme
   * dans l'ancienne interface.
   */

  import { apiGet, apiPost } from '$lib/api';
  import { config, modelIds, reloadConfig, type ModelInfo } from '$lib/config.svelte';
  import { debugQuery, setDebugMode } from '$lib/debug.svelte';
  import { getErrorDetails } from '$lib/format';
  import { t } from '$lib/i18n.svelte';
  import { createQuery } from '$lib/query.svelte';
  import { param, setParams } from '$lib/router.svelte';
  import { showToast } from '$lib/toast.svelte';
  import Card from '$lib/components/Card.svelte';
  import Empty from '$lib/components/Empty.svelte';
  import Spinner from '$lib/components/Spinner.svelte';

  /** Délai d'affichage des messages de sauvegarde. */
  const STATUS_MS = 3000;
  /** Attente après un redémarrage avant de re-interroger l'état. */
  const RESTART_DELAY_MS = 1500;
  const FULL_RESTART_DELAY_MS = 3000;

  const CAP_LABEL: Record<string, string> = {
    chat: 'cap.chat',
    vision: 'cap.vision',
    tools: 'cap.tools',
    code: 'cap.code',
    'web-search': 'cap.web_search'
  };

  interface ProxyActionResponse {
    needs_restart?: boolean;
    message?: string;
  }

  /** Ligne éditable d'une clé API (les secrets masqués viennent du serveur). */
  interface KeyRow {
    alias: string;
    api_key: string;
    api_key_masked?: string;
    go_workspace_id: string;
    go_workspace_id_masked?: string;
    go_auth_cookie: string;
    go_auth_cookie_masked?: string;
    enabled: boolean;
    /** Ligne ajoutée localement : le masqué n'existe pas encore. */
    fresh?: boolean;
  }

  interface CrRow {
    key: string;
    match: string;
    model: string;
    enabled: boolean;
    thinking: string;
    effort: string;
  }

  interface WebToolConfig {
    mode?: string;
    target_model?: string | null;
    max_results?: number;
    max_bytes?: number;
    timeout?: number;
    enabled?: boolean;
    via_vpn?: boolean;
    available_models?: string[];
    web_fetch?: {
      mode?: string;
      target_model?: string | null;
      max_bytes?: number;
      timeout?: number;
      enabled?: boolean;
      via_vpn?: boolean;
    };
  }

  // ── Paramètres serveur (brouillon) ──
  let port = $state('4000');
  let host = $state('0.0.0.0');
  let routing = $state('round-robin');
  let disableMapping = $state(false);
  let routeOpus = $state('');
  let routeSonnet = $state('');
  let routeHaiku = $state('');
  let seeded = false;

  let saveStatus = $state<{ kind: 'ok' | 'error'; message: string } | null>(null);
  let needsRestart = $state(false);
  let busy = $state<'start' | 'stop' | 'restart' | 'full' | null>(null);

  // ── Routes personnalisées ──
  let crRows = $state<CrRow[]>([]);
  let crMatch = $state('');
  let crModel = $state('');
  let crThinking = $state('auto');
  let crEffort = $state('auto');
  let crStatus = $state<{ kind: 'ok' | 'error'; message: string } | null>(null);
  let crSaving = $state(false);

  // ── Clés API ──
  let keyRows = $state<KeyRow[]>([]);
  let keyStatus = $state<{ kind: 'ok' | 'error'; message: string } | null>(null);
  let wfStatus = $state<{ kind: 'ok' | 'error'; message: string } | null>(null);
  /** Secrets affichés en clair (`"3"` = clé API, `"c3"` = cookie). */
  let revealed = $state<Record<string, boolean>>({});

  // ── web_search / web_fetch ──
  let wsMode = $state('duckduckgo');
  let wsModel = $state('');
  let wsMaxResults = $state('5');
  let wsTimeout = $state('10');
  let wsEnabled = $state('true');
  let wsViaVpn = $state('false');
  let wfMode = $state('direct');
  let wfModel = $state('');
  let wfMaxBytes = $state('12000');
  let wfTimeout = $state('15');
  let wfEnabled = $state('true');
  let wfViaVpn = $state('false');
  let wsSeeded = false;
  let wsStatus = $state<{ kind: 'ok' | 'error'; message: string } | null>(null);

  const cfg = $derived(config.data);
  const models = $derived(cfg?.models ?? {});
  const ids = $derived(modelIds(cfg));
  const limits = $derived(cfg?.model_limits ?? {});
  const caps = $derived(cfg?.model_capabilities ?? {});
  const debugEnabled = $derived(debugQuery.data?.enabled ?? false);

  // ── Classement Gratuit / Payant ──
  // Le tier vient du serveur (route réelle), jamais d'un suffixe deviné ici.
  const tierFilter = $derived(param('tier', 'all'));
  const filters = $derived([
    { value: 'all', label: t('config.filter_all') },
    { value: 'free', label: t('config.filter_free') },
    { value: 'paid', label: t('config.filter_paid') },
    { value: 'fallback', label: t('config.filter_fallback') }
  ]);
  const visibleIds = $derived.by(() => {
    if (tierFilter === 'free') return ids.filter((id) => models[id]?.tier === 'free');
    if (tierFilter === 'paid') return ids.filter((id) => models[id]?.tier === 'paid');
    if (tierFilter === 'fallback') return ids.filter((id) => Boolean(models[id]?.free_target));
    return ids;
  });
  const modelGroups = $derived.by(() => {
    const free = visibleIds.filter((id) => models[id]?.tier === 'free');
    const paid = visibleIds.filter((id) => models[id]?.tier !== 'free');
    const groups: { key: string; label: string; ids: string[] }[] = [];
    if (free.length) groups.push({ key: 'free', label: t('config.tier_free'), ids: free });
    if (paid.length) groups.push({ key: 'paid', label: t('config.tier_paid'), ids: paid });
    return groups;
  });
  const counts = $derived(cfg?.model_counts);

  function setTierFilter(value: string): void {
    setParams({ tier: value === 'all' ? null : value });
  }

  function tierState(
    info: ModelInfo | undefined
  ): 'free' | 'paid' | 'fallback' | 'unproven' | 'ignored' | 'unknown' {
    if (info?.tier === 'free') return 'free';
    if (info?.tier === 'unknown') return 'unknown';
    if (info?.free_target) return 'fallback';
    if (info?.tier_source === 'unproven') return 'unproven';
    if (info?.free_mapping && !info.free_mapping_active) return 'ignored';
    return 'paid';
  }

  function tierLabel(info: ModelInfo | undefined): string {
    const state = tierState(info);
    if (state === 'free') return t('config.tier_free');
    if (state === 'fallback') return t('config.tier_fallback');
    if (state === 'unproven') return t('config.tier_unproven');
    if (state === 'ignored') return t('config.tier_ignored');
    if (state === 'unknown') return t('config.tier_unknown');
    return t('config.tier_paid');
  }

  function legLabel(leg: string | undefined): string {
    if (leg === 'free') return t('config.leg_free');
    if (leg === 'paid') return t('config.leg_paid');
    if (leg === 'internal') return t('config.leg_internal');
    return t('config.leg_unknown');
  }

  function optionLabel(id: string): string {
    const info = models[id];
    if (info?.free_target) {
      return `${id} · ${t('config.option_fallback')} → ${info.free_target}`;
    }
    if (info?.route_target) {
      return `${id} · ${tierLabel(info)} · ${t('config.route_alias')} ${info.route_target}`;
    }
    if (info?.tier === 'free') return `${id} · ${t('config.option_free')}`;
    if (info?.free_mapping && !info.free_mapping_active) {
      return `${id} · ${t('config.option_ignored')}`;
    }
    return `${id} · ${t('config.option_paid')}`;
  }

  const webToolQ = createQuery<WebToolConfig>({
    load: (signal) => apiGet<WebToolConfig>('/api/config/web-search', signal),
    tags: () => ['config'],
    initial: {}
  });

  /** Seeding unique : on ne recopie jamais par-dessus les saisies en cours. */
  $effect(() => {
    if (!cfg || seeded) return;
    seeded = true;
    port = String(cfg.port ?? 4000);
    host = cfg.host ?? '0.0.0.0';
    routing = cfg.routing ?? 'round-robin';
    disableMapping = !!cfg.disable_mapping;
    routeOpus = cfg.routes?.opus?.model ?? '';
    routeSonnet = cfg.routes?.sonnet?.model ?? '';
    routeHaiku = cfg.routes?.haiku?.model ?? '';

    crRows = Object.entries(cfg.custom_routes ?? {}).map(([key, r]) => ({
      key,
      match: (r.match ?? []).join(', '),
      model: r.model ?? '',
      enabled: r.enabled !== false,
      thinking: r.thinking ?? 'auto',
      effort: r.effort ?? 'auto'
    }));

    keyRows = ((cfg.api_keys ?? []) as unknown as Array<Record<string, unknown>>).map((k) => ({
      alias: (k.alias as string) ?? '',
      api_key: (k.api_key as string) ?? '',
      api_key_masked: k.api_key_masked as string | undefined,
      go_workspace_id: (k.go_workspace_id as string) ?? '',
      go_workspace_id_masked: k.go_workspace_id_masked as string | undefined,
      go_auth_cookie: (k.go_auth_cookie as string) ?? '',
      go_auth_cookie_masked: k.go_auth_cookie_masked as string | undefined,
      enabled: k.enabled !== false
    }));
  });

  $effect(() => {
    const data = webToolQ.data;
    if (!data || wsSeeded || !data.mode) return;
    wsSeeded = true;
    wsMode = data.mode ?? 'duckduckgo';
    wsModel = data.target_model ?? '';
    wsMaxResults = String(data.max_results ?? 5);
    wsTimeout = String(data.timeout ?? 10);
    wsEnabled = String(data.enabled !== false);
    wsViaVpn = String(!!data.via_vpn);
    const wf = data.web_fetch;
    if (wf) {
      wfMode = wf.mode ?? 'direct';
      wfModel = wf.target_model ?? '';
      wfMaxBytes = String(wf.max_bytes ?? 12000);
      wfTimeout = String(wf.timeout ?? 15);
      wfEnabled = String(wf.enabled !== false);
      wfViaVpn = String(!!wf.via_vpn);
    }
  });

  function flash(
    target: 'save' | 'cr' | 'key' | 'ws',
    kind: 'ok' | 'error',
    message: string
  ): void {
    const entry = { kind, message };
    if (target === 'save') saveStatus = entry;
    else if (target === 'cr') crStatus = entry;
    else if (target === 'key') keyStatus = entry;
    else wsStatus = entry;
    window.setTimeout(() => {
      if (target === 'save') saveStatus = null;
      else if (target === 'cr') crStatus = null;
      else if (target === 'key') keyStatus = null;
      else wsStatus = null;
    }, STATUS_MS);
  }

  function errorMessage(e: unknown): string {
    return getErrorDetails(e instanceof Error ? e.message : String(e)).raw;
  }

  // ── Proxy ──
  async function proxyAction(
    kind: 'start' | 'stop' | 'restart' | 'full'
  ): Promise<void> {
    if (busy) return;
    if (kind === 'full' && !window.confirm(t('proxy.full_restart_confirm'))) return;
    busy = kind;
    try {
      const path = kind === 'full' ? '/api/proxy/restart?full=true' : `/api/proxy/${kind}`;
      await apiPost<ProxyActionResponse>(path);
      await new Promise((r) =>
        window.setTimeout(r, kind === 'full' ? FULL_RESTART_DELAY_MS : RESTART_DELAY_MS)
      );
      await reloadConfig();
    } catch (e) {
      showToast(errorMessage(e), 'error');
    } finally {
      busy = null;
    }
  }

  async function saveConfig(): Promise<void> {
    try {
      const result = await apiPost<ProxyActionResponse>('/api/config', {
        routes: { opus: routeOpus, sonnet: routeSonnet, haiku: routeHaiku },
        port: Number(port),
        host,
        routing,
        disable_mapping: disableMapping
      });
      needsRestart = !!result.needs_restart;
      flash('save', 'ok', result.message || t('config.saved'));
      await reloadConfig();
    } catch (e) {
      flash('save', 'error', t('status.error'));
      showToast(errorMessage(e), 'error');
    }
  }

  async function onDebugToggle(checked: boolean): Promise<void> {
    try {
      await setDebugMode(checked);
    } catch (e) {
      showToast(errorMessage(e), 'error');
    }
  }

  // ── Routes personnalisées ──
  function addCustomRoute(): void {
    const match = crMatch.trim();
    if (!match || !crModel) {
      showToast(t('config.cr_alert'), 'warning');
      return;
    }
    crRows = [
      ...crRows,
      {
        key: makeRouteKey(match),
        match,
        model: crModel,
        enabled: true,
        thinking: crThinking,
        effort: crEffort
      }
    ];
    crMatch = '';
    crModel = '';
  }

  function removeCustomRoute(key: string): void {
    crRows = crRows.filter((r) => r.key !== key);
  }

  /** Reprise de `makeRouteKey` du legacy : clé normalisée d'une règle. */
  function makeRouteKey(match: string): string {
    if (match === '*') return '*';
    return match.toLowerCase().replace(/[^a-z0-9]/g, '');
  }

  async function saveCustomRoutes(): Promise<void> {
    if (crSaving) return;
    crSaving = true;
    const routes: Record<string, unknown> = {};
    for (const row of crRows) {
      const match = row.match.trim();
      const model = row.model.trim();
      if (!match || !model) continue;
      const data: Record<string, unknown> = {
        match: match.split(',').map((s) => s.trim()),
        model
      };
      if (!row.enabled) data.enabled = false;
      if (row.thinking && row.thinking !== 'auto') data.thinking = row.thinking;
      if (row.effort && row.effort !== 'auto') data.effort = row.effort;
      routes[makeRouteKey(match)] = data;
    }
    try {
      await apiPost('/api/config/custom-routes', routes);
      flash('cr', 'ok', t('config.cr_saved'));
      await reloadConfig();
    } catch (e) {
      flash('cr', 'error', t('status.error'));
      showToast(errorMessage(e), 'error');
    } finally {
      crSaving = false;
    }
  }

  // ── Clés API ──
  function addKeyRow(): void {
    keyRows = [
      ...keyRows,
      {
        alias: '',
        api_key: '',
        go_workspace_id: '',
        go_auth_cookie: '',
        enabled: true,
        fresh: true
      }
    ];
  }

  function removeKeyRow(index: number): void {
    keyRows = keyRows.filter((_, i) => i !== index);
    const next: Record<string, boolean> = {};
    for (const [k, v] of Object.entries(revealed)) {
      const numeric = Number(k.replace(/^c/, ''));
      const prefix = k.startsWith('c') ? 'c' : '';
      next[`${prefix}${numeric > index ? numeric - 1 : numeric}`] = v;
    }
    revealed = next;
  }

  async function saveKeys(): Promise<void> {
    // [audit 2026-10-30] Never filter out existing rows: their api_key is
    // masked/empty by design. Sending only the newly typed row made the
    // server replace the whole list and erased every other key.
    const keys = keyRows
      .filter((k) => k.api_key.trim() || k.api_key_masked || k.go_workspace_id_masked || k.go_auth_cookie_masked)
      .map((k) => ({
        api_key: k.api_key.trim(),
        api_key_masked: k.api_key_masked ?? '',
        alias: k.alias.trim(),
        go_workspace_id: k.go_workspace_id.trim(),
        go_auth_cookie: k.go_auth_cookie.trim(),
        enabled: k.enabled
      }));
    try {
      await apiPost('/api/config/api-keys', { api_keys: keys, replace: true });
      flash('key', 'ok', t('config.ak_saved'));
      await reloadConfig();
    } catch (e) {
      flash('key', 'error', t('status.error'));
      showToast(errorMessage(e), 'error');
    }
  }

  // ── web_search / web_fetch ──
  async function saveWebSearch(): Promise<void> {
    try {
      await apiPost('/api/config/web-search', {
        mode: wsMode,
        target_model: wsModel || null,
        max_results: Number(wsMaxResults) || 5,
        timeout: Number(wsTimeout) || 10,
        enabled: wsEnabled === 'true',
        via_vpn: wsViaVpn === 'true'
      });
      flash('ws', 'ok', t('config.cr_saved'));
    } catch (e) {
      flash('ws', 'error', t('status.error'));
      showToast(errorMessage(e), 'error');
    }
  }

  async function saveWebFetch(): Promise<void> {
    try {
      await apiPost('/api/config/web-fetch', {
        mode: wfMode,
        target_model: wfModel || null,
        max_bytes: Number(wfMaxBytes) || 12000,
        timeout: Number(wfTimeout) || 15,
        enabled: wfEnabled === 'true',
        via_vpn: wfViaVpn === 'true'
      });
      wfStatus = { kind: 'ok', message: t('config.cr_saved') };
      window.setTimeout(() => (wfStatus = null), STATUS_MS);
    } catch (e) {
      wfStatus = { kind: 'error', message: t('status.error') };
      window.setTimeout(() => (wfStatus = null), STATUS_MS);
      showToast(errorMessage(e), 'error');
    }
  }

  function copyText(value: string): void {
    void navigator.clipboard?.writeText(value).catch(() => undefined);
  }

  const showWsModel = $derived(wsMode !== 'duckduckgo');
  const showWfModel = $derived(wfMode !== 'direct');
  const modelList = $derived(webToolQ.data?.available_models ?? []);
</script>

{#if !cfg}
  <Spinner />
{:else}
  <!-- ── Paramètres serveur ── -->
  <Card title={t('config.server')}>
    <div class="status-row">
      <div class="status">
        <span class="dot" class:dot--running={cfg.proxy_running} class:dot--stopped={!cfg.proxy_running}></span>
        <span>{cfg.proxy_running ? t('proxy.running') : t('proxy.stopped')}</span>
        <button class="addr" title={t('proxy.copy')} onclick={() => copyText(`http://localhost:${cfg.port}`)}>
          http://localhost:{cfg.port}
        </button>
        {#if cfg.host === '0.0.0.0'}
          {#each (cfg.local_ips ?? []).filter((ip) => ip !== '127.0.0.1') as ip (ip)}
            <button class="addr" title={t('proxy.copy')} onclick={() => copyText(`http://${ip}:${cfg.port}`)}>
              / http://{ip}:{cfg.port}
            </button>
          {/each}
        {/if}
      </div>

      <div class="actions">
        <button
          class="btn btn--sm"
          disabled={cfg.proxy_running || busy === 'start'}
          onclick={() => proxyAction('start')}
        >
          {t('proxy.start')}
        </button>
        <button
          class="btn btn--sm btn--danger"
          disabled={!cfg.proxy_running || busy === 'stop'}
          onclick={() => proxyAction('stop')}
        >
          {t('proxy.stop')}
        </button>
        <button
          class="btn btn--sm"
          disabled={!cfg.proxy_running || busy === 'restart'}
          onclick={() => proxyAction('restart')}
        >
          {t('proxy.restart')}
        </button>
        <button
          class="btn btn--sm btn--danger"
          disabled={busy === 'full'}
          onclick={() => proxyAction('full')}
        >
          {t('proxy.full_restart')}
        </button>
      </div>
    </div>

    {#if needsRestart}
      <p class="notice">{t('proxy.restart_notice')}</p>
    {/if}

    <div class="grid grid--3">
      <label class="field">
        <span class="field-label">{t('config.api_port')}</span>
        <input class="input" type="number" min="1" max="65535" bind:value={port} />
      </label>
      <label class="field">
        <span class="field-label">{t('config.bind_address')}</span>
        <select class="select" bind:value={host}>
          <option value="0.0.0.0">{t('config.bind_lan')}</option>
          <option value="127.0.0.1">{t('config.bind_local')}</option>
        </select>
      </label>
      <label class="field">
        <span class="field-label">{t('config.key_routing')}</span>
        <select class="select" bind:value={routing}>
          <option value="round-robin">{t('config.routing_rr')}</option>
          <option value="failover">{t('config.routing_failover')}</option>
        </select>
      </label>
    </div>

    <div class="actions actions--spaced">
      <button class="btn btn--primary" onclick={saveConfig}>{t('config.save')}</button>
      {#if saveStatus}
        <span class="save-status save-status--{saveStatus.kind}" role="status">
          {saveStatus.message}
        </span>
      {/if}
    </div>

    <div class="debug-row">
      <label class="check">
        <input
          type="checkbox"
          checked={debugEnabled}
          onchange={(e) => onDebugToggle(e.currentTarget.checked)}
        />
        <span>{t('config.debug_mode')}</span>
      </label>
      <span class="debug-text" class:debug-text--on={debugEnabled}>
        {debugEnabled ? t('config.debug_enabled') : t('config.debug_off')}
      </span>
    </div>
    <p class="hint">{t('config.debug_desc')}</p>
  </Card>

  <!-- ── Mapping de modèles ── -->
  <Card title={t('config.model_mapping')}>
    <p class="hint">{t('config.mapping_desc')}</p>

    <label class="check check--block">
      <input type="checkbox" bind:checked={disableMapping} />
      <span>{t('config.disable_mapping')}</span>
    </label>
    <p class="hint">{t('config.disable_mapping_hint')}</p>

    <div class="grid grid--3" class:grid--dimmed={disableMapping}>
      <label class="field">
        <span class="field-label">{t('config.opus')}</span>
        <select class="select" disabled={disableMapping} bind:value={routeOpus}>
          <option value="">—</option>
          {#each ids as id (id)}
            <option value={id}>{optionLabel(id)}</option>
          {/each}
        </select>
      </label>
      <label class="field">
        <span class="field-label">{t('config.sonnet')}</span>
        <select class="select" disabled={disableMapping} bind:value={routeSonnet}>
          <option value="">—</option>
          {#each ids as id (id)}
            <option value={id}>{optionLabel(id)}</option>
          {/each}
        </select>
      </label>
      <label class="field">
        <span class="field-label">{t('config.haiku')}</span>
        <select class="select" disabled={disableMapping} bind:value={routeHaiku}>
          <option value="">—</option>
          {#each ids as id (id)}
            <option value={id}>{optionLabel(id)}</option>
          {/each}
        </select>
      </label>
    </div>
  </Card>

  <!-- ── Routes personnalisées ── -->
  <Card title={t('config.custom_routes')}>
    <p class="hint">{t('config.custom_routes_desc')}</p>

    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{t('config.cr_match')}</th>
            <th>{t('config.cr_backend')}</th>
            <th class="center">{t('config.cr_enabled')}</th>
            <th>{t('config.cr_thinking')}</th>
            <th>{t('config.cr_effort')}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#if crRows.length === 0}
            <tr><td colspan="6" class="muted">{t('config.cr_no_routes')}</td></tr>
          {:else}
            {#each crRows as row (row.key)}
              <tr>
                <td>
                  <input class="input" type="text" bind:value={row.match} />
                </td>
                <td>
                  <select class="select" bind:value={row.model}>
                    <option value="">{t('config.cr_select_model')}</option>
                    {#each ids as id (id)}
                      <option value={id}>{optionLabel(id)}</option>
                    {/each}
                  </select>
                </td>
                <td class="center">
                  <input type="checkbox" bind:checked={row.enabled} />
                </td>
                <td>
                  <select class="select" bind:value={row.thinking}>
                    <option value="auto">{t('config.cr_thinking_auto')}</option>
                    <option value="adaptive">{t('config.cr_thinking_adaptive')}</option>
                    <option value="disabled">{t('config.cr_thinking_disabled')}</option>
                  </select>
                </td>
                <td>
                  <select class="select" bind:value={row.effort}>
                    <option value="auto">{t('config.cr_effort_auto')}</option>
                    <option value="low">{t('config.cr_effort_low')}</option>
                    <option value="medium">{t('config.cr_effort_medium')}</option>
                    <option value="high">{t('config.cr_effort_high')}</option>
                  </select>
                </td>
                <td>
                  <button class="btn btn--sm btn--danger" onclick={() => removeCustomRoute(row.key)}>
                    {t('config.ak_delete')}
                  </button>
                </td>
              </tr>
            {/each}
          {/if}
        </tbody>
      </table>
    </div>

    <div class="add-row">
      <input class="input" type="text" placeholder="ex : nimo, web_search" bind:value={crMatch} />
      <select class="select" bind:value={crThinking}>
        <option value="auto">{t('config.cr_thinking_auto')}</option>
        <option value="adaptive">{t('config.cr_thinking_adaptive')}</option>
        <option value="disabled">{t('config.cr_thinking_disabled')}</option>
      </select>
      <select class="select" bind:value={crEffort}>
        <option value="auto">{t('config.cr_effort_auto')}</option>
        <option value="low">{t('config.cr_effort_low')}</option>
        <option value="medium">{t('config.cr_effort_medium')}</option>
        <option value="high">{t('config.cr_effort_high')}</option>
      </select>
      <select class="select" bind:value={crModel}>
        <option value="">{t('config.cr_select_model')}</option>
        {#each ids as id (id)}
          <option value={id}>{optionLabel(id)}</option>
        {/each}
      </select>
      <button class="btn" onclick={addCustomRoute}>{t('config.cr_add_btn')}</button>
    </div>

    <div class="actions actions--spaced">
      <button class="btn btn--primary" disabled={crSaving} onclick={saveCustomRoutes}>
        {t('config.cr_save')}
      </button>
      {#if crStatus}
        <span class="save-status save-status--{crStatus.kind}" role="status">{crStatus.message}</span>
      {/if}
    </div>
  </Card>

  <!-- ── Recherche web ── -->
  <Card title={t('config.web_search_title')}>
    <p class="hint">{t('config.web_search_desc')}</p>

    <div class="add-row">
      <label class="field">
        <span class="field-label">{t('config.mode')}</span>
        <select class="select" bind:value={wsMode}>
          <option value="duckduckgo">{t('config.ws_mode_ddg')}</option>
          <option value="model">{t('config.ws_mode_model')}</option>
          <option value="ddg_then_model">{t('config.ws_mode_ddg_model')}</option>
          <option value="model_then_ddg">{t('config.ws_mode_model_ddg')}</option>
        </select>
      </label>

      {#if showWsModel}
        <label class="field">
          <span class="field-label">{t('config.target_model')}</span>
          <select class="select" bind:value={wsModel}>
            <option value="">{t('config.select_model')}</option>
            {#each modelList as m (m)}
              <option value={m}>{m}</option>
            {/each}
          </select>
        </label>
      {/if}

      <label class="field">
        <span class="field-label">{t('config.max_results')}</span>
        <input class="input input--narrow" type="number" min="1" max="10" bind:value={wsMaxResults} />
      </label>
      <label class="field">
        <span class="field-label">{t('config.timeout_s')}</span>
        <input class="input input--narrow" type="number" min="5" max="30" bind:value={wsTimeout} />
      </label>
      <label class="field">
        <span class="field-label">{t('config.enabled')}</span>
        <select class="select" bind:value={wsEnabled}>
          <option value="true">{t('logs.yes')}</option>
          <option value="false">{t('logs.no')}</option>
        </select>
      </label>
      <label class="field">
        <span class="field-label">{t('config.via_vpn')}</span>
        <select class="select" bind:value={wsViaVpn}>
          <option value="false">{t('config.egress_direct')}</option>
          <option value="true">{t('logs.yes')}</option>
        </select>
      </label>
    </div>

    <div class="actions actions--spaced">
      <button class="btn btn--primary" onclick={saveWebSearch}>{t('common.save')}</button>
      {#if wsStatus}
        <span class="save-status save-status--{wsStatus.kind}" role="status">{wsStatus.message}</span>
      {/if}
    </div>
  </Card>

  <!-- ── Fetch web ── -->
  <Card title={t('config.web_fetch_title')}>
    <p class="hint">{t('config.web_fetch_desc')}</p>

    <div class="add-row">
      <label class="field">
        <span class="field-label">{t('config.mode')}</span>
        <select class="select" bind:value={wfMode}>
          <option value="direct">{t('config.wf_mode_direct')}</option>
          <option value="model">{t('config.ws_mode_model')}</option>
          <option value="direct_then_model">{t('config.wf_mode_direct_model')}</option>
        </select>
      </label>

      {#if showWfModel}
        <label class="field">
          <span class="field-label">{t('config.target_model')}</span>
          <select class="select" bind:value={wfModel}>
            <option value="">{t('config.select_model')}</option>
            {#each modelList as m (m)}
              <option value={m}>{m}</option>
            {/each}
          </select>
        </label>
      {/if}

      <label class="field">
        <span class="field-label">{t('config.max_bytes')}</span>
        <input
          class="input input--narrow"
          type="number"
          min="2000"
          max="50000"
          step="1000"
          bind:value={wfMaxBytes}
        />
      </label>
      <label class="field">
        <span class="field-label">{t('config.timeout_s')}</span>
        <input class="input input--narrow" type="number" min="5" max="30" bind:value={wfTimeout} />
      </label>
      <label class="field">
        <span class="field-label">{t('config.enabled')}</span>
        <select class="select" bind:value={wfEnabled}>
          <option value="true">{t('logs.yes')}</option>
          <option value="false">{t('logs.no')}</option>
        </select>
      </label>
      <label class="field">
        <span class="field-label">{t('config.via_vpn')}</span>
        <select class="select" bind:value={wfViaVpn}>
          <option value="false">{t('config.egress_direct')}</option>
          <option value="true">{t('logs.yes')}</option>
        </select>
      </label>
    </div>

    <div class="actions actions--spaced">
      <button class="btn btn--primary" onclick={saveWebFetch}>{t('common.save')}</button>
      {#if wfStatus}
        <span class="save-status save-status--{wfStatus.kind}" role="status">{wfStatus.message}</span>
      {/if}
    </div>
  </Card>

  <!-- ── Clés API ── -->
  <Card title={t('config.api_keys')}>
    <p class="hint">{t('config.api_keys_desc')}</p>

    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{t('config.ak_alias')}</th>
            <th>{t('config.ak_key')}</th>
            <th>{t('config.ak_workspace')}</th>
            <th>{t('config.ak_cookie')}</th>
            <th class="center">{t('config.ak_enabled')}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#if keyRows.length === 0}
            <tr><td colspan="6" class="muted">{t('config.ak_no_keys')}</td></tr>
          {:else}
            {#each keyRows as row, i (i)}
              <tr>
                <td><input class="input" type="text" bind:value={row.alias} /></td>
                <td>
                  <div class="secret">
                    <input
                      class="input mono"
                      type={revealed[i] ? 'text' : 'password'}
                      placeholder={row.api_key_masked || '****'}
                      bind:value={row.api_key}
                    />
                    <button
                      class="btn btn--sm"
                      aria-label={t('show_hide')}
                      onclick={() => (revealed = { ...revealed, [i]: !revealed[i] })}
                    >
                      👁
                    </button>
                  </div>
                </td>
                <td>
                  <input
                    class="input mono"
                    type="text"
                    placeholder={row.go_workspace_id_masked || ''}
                    bind:value={row.go_workspace_id}
                  />
                </td>
                <td>
                  <div class="secret">
                    <input
                      class="input mono"
                      type={revealed[`c${i}`] ? 'text' : 'password'}
                      placeholder={row.go_auth_cookie_masked || '****'}
                      bind:value={row.go_auth_cookie}
                    />
                    <button
                      class="btn btn--sm"
                      aria-label={t('show_hide')}
                      onclick={() => (revealed = { ...revealed, [`c${i}`]: !revealed[`c${i}`] })}
                    >
                      👁
                    </button>
                  </div>
                </td>
                <td class="center">
                  <input type="checkbox" bind:checked={row.enabled} />
                </td>
                <td>
                  <button class="btn btn--sm btn--danger" onclick={() => removeKeyRow(i)}>
                    {t('config.ak_delete')}
                  </button>
                </td>
              </tr>
            {/each}
          {/if}
        </tbody>
      </table>
    </div>

    <div class="actions actions--spaced">
      <button class="btn" onclick={addKeyRow}>{t('config.ak_add')}</button>
      <button class="btn btn--primary" onclick={saveKeys}>{t('config.ak_save')}</button>
      {#if keyStatus}
        <span class="save-status save-status--{keyStatus.kind}" role="status">{keyStatus.message}</span>
      {/if}
    </div>
  </Card>

  <!-- ── Modèles disponibles ── -->
  <Card title={t('config.models')}>
    {#if ids.length === 0}
      <Empty title={t('config.no_models')} />
    {:else}
      <div class="tier-bar">
        <div class="tier-chips" role="group" aria-label={t('config.tier_filter_label')}>
          {#each filters as f (f.value)}
            <button
              type="button"
              class="chip"
              class:chip--active={tierFilter === f.value}
              onclick={() => setTierFilter(f.value)}
            >
              {f.label}
            </button>
          {/each}
        </div>
        {#if counts}
          <span class="tier-summary">
            {t('config.models_summary', {
              free: counts.free,
              paid: counts.paid,
              unproven: counts.paid_unproven ?? 0,
              fallback: counts.paid_free_fallback,
              ignored: counts.paid_free_ignored
            })}
          </span>
        {/if}
      </div>

      {#if visibleIds.length === 0}
        <Empty title={t('config.no_models_filter')} />
      {:else}
        <div class="table-wrap">
          <table class="table">
            <thead>
              <tr>
                <th>{t('config.model_id')}</th>
                <th>{t('config.tier')}</th>
                <th>{t('config.leg')}</th>
                <th>{t('config.free_target')}</th>
                <th>{t('config.capabilities')}</th>
                <th>{t('config.endpoint')}</th>
                <th class="num">{t('config.limit_5h')}</th>
                <th class="num">{t('config.limit_weekly')}</th>
                <th class="num">{t('config.limit_monthly')}</th>
              </tr>
            </thead>
            <tbody>
              {#each modelGroups as group (group.key)}
                <tr class="group-row">
                  <td colspan="9">{group.label} · {group.ids.length}</td>
                </tr>
                {#each group.ids as id (id)}
                  {@const info = models[id]}
                  {@const lim = limits[id] ?? []}
                  {@const modelCaps = caps[id] ?? []}
                  {@const state = tierState(info)}
                  <tr>
                    <td class="mono">
                      {id}
                      {#if info?.source === 'upstream'}
                        <span class="badge badge--new">{t('config.new_badge')}</span>
                      {/if}
                      {#if info?.route_mismatch}
                        <span
                          class="badge badge--warn"
                          title={t('config.route_mismatch_hint', {
                            tier: tierLabel(info),
                            leg: legLabel(info?.route_leg)
                          })}
                        >⚠</span>
                      {:else if info?.catalog_mismatch}
                        <span class="badge" title={t('config.catalog_mismatch_hint')}>ⓘ</span>
                      {/if}
                    </td>
                    <td>
                      <span class="badge badge--{state}">{tierLabel(info)}</span>
                    </td>
                    <td class="mono">
                      {legLabel(info?.route_leg)}
                      {#if info?.protocol}<span class="muted"> · {info.protocol}</span>{/if}
                    </td>
                    <td class="mono">
                      {#if info?.free_target}
                        → {info.free_target}
                      {:else if info?.route_target}
                        <span class="muted">{t('config.route_alias')} {info.route_target}</span>
                      {:else if info?.free_mapping && !info.free_mapping_active}
                        <span class="muted" title={t('config.mapping_ignored_hint')}>
                          {info.free_mapping} ({t('config.mapping_ignored')})
                        </span>
                      {:else}
                        <span class="muted">—</span>
                      {/if}
                    </td>
                    <td>
                      {#if modelCaps.length === 0}
                        <span class="muted">-</span>
                      {:else}
                        {#each modelCaps as c (c)}
                          <span class="badge">{t(CAP_LABEL[c] ?? c)}</span>
                        {/each}
                      {/if}
                    </td>
                    <td class="mono">{info?.endpoint ?? '-'}</td>
                    <td class="num mono">{lim[0] ? String(lim[0]) : '-'}</td>
                    <td class="num mono">{lim[1] ? String(lim[1]) : '-'}</td>
                    <td class="num mono">{lim[2] ? String(lim[2]) : '-'}</td>
                  </tr>
                {/each}
              {/each}
            </tbody>
          </table>
        </div>
        <p class="hint">
          {t('config.models_hint')}
          <a href="https://opencode.ai/docs/fr/go/#usage-limits" target="_blank" rel="noreferrer">
            {t('config.see_docs')}
          </a>
        </p>
      {/if}
    {/if}
  </Card>
{/if}

<style>
  .status-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--sp-4);
    flex-wrap: wrap;
    margin-bottom: var(--sp-4);
  }

  .status {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    flex-wrap: wrap;
    font-size: var(--fs-sm);
  }

  .dot {
    width: 10px;
    height:  10px;
    border-radius: 50%;
    background: var(--text-faint);
    flex: none;
  }

  .dot--running {
    background: var(--success);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--success) 25%, transparent);
  }

  .dot--stopped {
    background: var(--danger);
  }

  .addr {
    background: none;
    border: 0;
    padding: 0;
    color: var(--text-dim);
    font-family: var(--font-mono);
    font-size: var(--fs-sm);
    cursor: copy;
    text-decoration: underline dotted;
  }

  .addr:hover {
    color: var(--accent);
  }

  .actions {
    display: flex;
    gap: var(--sp-2);
    flex-wrap: wrap;
    align-items: center;
  }

  .actions--spaced {
    margin-top: var(--sp-4);
  }

  .notice {
    margin: 0 0 var(--sp-4);
    padding: var(--sp-3);
    border: 1px solid var(--warn);
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--warn) 12%, transparent);
    color: var(--text);
    font-size: var(--fs-sm);
  }

  .grid {
    display: grid;
    gap: var(--sp-3);
  }

  .grid--3 {
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  }

  .grid--dimmed {
    opacity: 0.45;
  }

  .field {
    display: grid;
    gap: var(--sp-1);
    min-width: 0;
  }

  .field-label {
    font-size: var(--fs-xs);
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .save-status {
    font-size: var(--fs-sm);
  }

  .save-status--ok {
    color: var(--success);
  }

  .save-status--error {
    color: var(--danger);
  }

  .debug-row {
    display: flex;
    align-items: center;
    gap: var(--sp-4);
    margin-top: var(--sp-5);
    padding-top: var(--sp-4);
    border-top: 1px solid var(--border);
  }

  .debug-text {
    font-size: var(--fs-sm);
    color: var(--text-dim);
  }

  .debug-text--on {
    color: var(--success);
  }

  .check {
    display: inline-flex;
    align-items: center;
    gap: var(--sp-2);
    font-size: var(--fs-sm);
    cursor: pointer;
    user-select: none;
  }

  .check--block {
    margin-bottom: var(--sp-2);
  }

  .hint {
    margin: 0 0 var(--sp-4);
    color: var(--text-dim);
    font-size: var(--fs-sm);
  }

  .table-wrap {
    overflow-x: auto;
  }

  .center {
    text-align: center;
  }

  .num {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  .muted {
    color: var(--text-dim);
  }

  .mono {
    font-family: var(--font-mono);
    font-size: var(--fs-sm);
  }

  .add-row {
    display: flex;
    gap: var(--sp-3);
    flex-wrap: wrap;
    align-items: flex-end;
    margin-top: var(--sp-4);
  }

  .add-row .field {
    flex: 0 1 160px;
  }

  .add-row .input,
  .add-row .select {
    min-width: 0;
  }

  .input--narrow {
    width: 96px;
  }

  .secret {
    display: flex;
    gap: var(--sp-1);
    align-items: center;
  }

  .secret .input {
    flex: 1;
    min-width: 90px;
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

  .badge--new {
    background: color-mix(in srgb, var(--success) 20%, transparent);
    border-color: color-mix(in srgb, var(--success) 45%, transparent);
    color: var(--text);
  }

  .badge--free {
    background: color-mix(in srgb, var(--success) 20%, transparent);
    border-color: color-mix(in srgb, var(--success) 45%, transparent);
    color: var(--text);
  }

  .badge--paid {
    background: var(--surface-3);
  }

  .badge--fallback {
    background: color-mix(in srgb, var(--warn) 20%, transparent);
    border-color: color-mix(in srgb, var(--warn) 45%, transparent);
    color: var(--text);
  }

  .badge--unproven {
    background: color-mix(in srgb, var(--warn) 8%, transparent);
    border-color: color-mix(in srgb, var(--warn) 30%, transparent);
    border-style: dashed;
    color: var(--text-dim);
  }

  .badge--ignored {
    background: color-mix(in srgb, var(--warn) 10%, transparent);
    border-style: dashed;
    color: var(--text-dim);
  }

  .badge--unknown {
    background: var(--surface-3);
    color: var(--text-faint);
  }

  .badge--warn {
    background: var(--warn-bg);
    border-color: color-mix(in srgb, var(--warn) 45%, transparent);
    color: var(--text);
  }

  .tier-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--sp-3);
    flex-wrap: wrap;
    margin-bottom: var(--sp-3);
  }

  .tier-chips {
    display: flex;
    gap: var(--sp-2);
    flex-wrap: wrap;
  }

  .chip {
    font: inherit;
    font-size: var(--fs-sm);
    padding: 3px 10px;
    border-radius: var(--radius-full);
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text-dim);
    cursor: pointer;
  }

  .chip:hover {
    color: var(--text);
    border-color: var(--accent);
  }

  .chip--active {
    background: color-mix(in srgb, var(--accent) 18%, transparent);
    border-color: var(--accent);
    color: var(--text);
  }

  .tier-summary {
    font-size: var(--fs-sm);
    color: var(--text-dim);
  }

  .group-row td {
    background: var(--surface-2);
    color: var(--text-dim);
    font-size: var(--fs-sm);
    font-weight: 600;
    letter-spacing: 0.02em;
  }
</style>
