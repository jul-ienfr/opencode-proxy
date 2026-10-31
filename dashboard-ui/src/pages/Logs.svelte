<script lang="ts">
  /**
   * Historique des requêtes avec filtres, pagination et fiche détaillée.
   *
   * Contrairement à l'ancienne interface (état en mémoire, perdu au
   * rafraîchissement), tous les filtres et la page vivent dans l'URL :
   * un partage de lien ou un F5 retrouve exactement la même vue.
   */

  import { apiGet } from '$lib/api';
  import { formatDateTime, formatNumber, getErrorDetails } from '$lib/format';
  import { t } from '$lib/i18n.svelte';
  import { createQuery } from '$lib/query.svelte';
  import { param, setParams } from '$lib/router.svelte';
  import Card from '$lib/components/Card.svelte';
  import Empty from '$lib/components/Empty.svelte';
  import FilterBar from '$lib/components/FilterBar.svelte';
  import Pagination from '$lib/components/Pagination.svelte';
  import Spinner from '$lib/components/Spinner.svelte';

  const PAGE_SIZE = 20;
  /** Nombre de badges d'outils affichés avant de replier en « +N ». */
  const MAX_TOOL_BADGES = 5;

  interface LogRow {
    id: string;
    timestamp: string;
    account_alias?: string;
    is_free_model?: boolean;
    free_ip?: string;
    original_model?: string;
    model?: string;
    tokens_input: number;
    tokens_output: number;
    tokens_cache: number;
    thinking?: string;
    effort?: string;
    tools_used?: string[];
    tools?: string[];
    client_ip?: string;
    duration_ms?: number;
    success: boolean;
    error?: string;
    protocol?: string;
    geo_via_vpn?: boolean;
    geo_allowed?: string;
    geo_direct_country?: string;
    geo_direct_ip?: string;
    geo_country?: string;
  }

  interface HistoryResponse {
    logs: LogRow[];
    total: number;
  }

  interface HistoryFilters {
    models?: string[];
    original_models?: string[];
    accounts?: string[];
    tools_used?: string[];
  }

  interface RequestDetail {
    id: string;
    success: boolean;
    error?: string;
    timestamp: string;
    account_alias?: string;
    client_ip?: string;
    client_user_agent?: string;
    original_model?: string;
    model?: string;
    protocol?: string;
    is_stream?: boolean;
    duration_ms?: number;
    tokens_input: number;
    tokens_output: number;
    tokens_cache: number;
    thinking?: string;
    effort?: string;
    tools_used?: string[];
    tools?: string[];
    request_body?: unknown;
    response_body?: unknown;
  }

  // ── Filtres : brouillon (appliqué au clic, comme l'original) ──
  let draft = $state({
    status: param('status', ''),
    model: param('model', ''),
    original_model: param('original_model', ''),
    account: param('account', ''),
    tool: param('tool', ''),
    search: param('search', '')
  });

  let selected = $state<LogRow | null>(null);
  let detail = $state<RequestDetail | null>(null);
  let detailState = $state<'loading' | 'ok' | 'error'>('loading');
  let detailError = $state('');
  /** Sections JSON repliables de la fiche (corps de réponse replié par défaut). */
  let openJson = $state<Record<string, boolean>>({ 'request-body': true, 'response-body': false });

  const from = $derived(param('from', ''));
  const to = $derived(param('to', ''));
  const page = $derived(Number(param('page', '1')) || 1);
  const periodKey = $derived(`${from}|${to}`);

  // Valeurs APPLIQUÉES (lues dans l'URL) : c'est elles qui déclenchent le
  // fetch — le brouillon du formulaire, lui, ne bouge qu'à la validation.
  const status = $derived(param('status', ''));
  const model = $derived(param('model', ''));
  const originalModel = $derived(param('original_model', ''));
  const account = $derived(param('account', ''));
  const tool = $derived(param('tool', ''));
  const search = $derived(param('search', ''));

  const historyQ = createQuery<HistoryResponse>({
    load: (signal) => {
      const offset = (page - 1) * PAGE_SIZE;
      const parts = [`limit=${PAGE_SIZE}`, `offset=${offset}`];
      if (from) parts.push(`from_date=${encodeURIComponent(from)}`);
      if (to) parts.push(`to_date=${encodeURIComponent(to)}`);
      if (status) parts.push(`status=${encodeURIComponent(status)}`);
      if (model) parts.push(`model=${encodeURIComponent(model)}`);
      if (originalModel) parts.push(`original_model=${encodeURIComponent(originalModel)}`);
      if (account) parts.push(`account=${encodeURIComponent(account)}`);
      if (tool) parts.push(`tool=${encodeURIComponent(tool)}`);
      if (search) parts.push(`search=${encodeURIComponent(search)}`);
      return apiGet<HistoryResponse>(`/api/history?${parts.join('&')}`, signal);
    },
    tags: () => ['history'],
    deps: () => `${periodKey}|${status}|${model}|${originalModel}|${account}|${tool}|${search}|${page}`,
    initial: { logs: [], total: 0 }
  });

  const filtersQ = createQuery<HistoryFilters>({
    load: (signal) => apiGet<HistoryFilters>('/api/history/filters', signal),
    tags: () => ['history'],
    initial: {}
  });

  const rows = $derived(historyQ.data?.logs ?? []);
  const totalPages = $derived(Math.max(1, Math.ceil((historyQ.data?.total ?? 0) / PAGE_SIZE)));

  function applyFilters(): void {
    setParams({
      status: draft.status || null,
      model: draft.model || null,
      original_model: draft.original_model || null,
      account: draft.account || null,
      tool: draft.tool || null,
      search: draft.search || null,
      page: null
    });
  }

  function resetFilters(): void {
    draft = {
      status: '',
      model: '',
      original_model: '',
      account: '',
      tool: '',
      search: ''
    };
    setParams({
      status: null,
      model: null,
      original_model: null,
      account: null,
      tool: null,
      search: null,
      page: null
    });
  }

  /** Outils uniquement utilisés (badge plein) vs déclarés (badge creux). */
  function unique(list: string[] | undefined): string[] {
    return list ? [...new Set(list)] : [];
  }

  function toolTitle(log: LogRow): string {
    const used = unique(log.tools_used);
    const declared = unique(log.tools);
    if (used.length && declared.length) {
      return `Déclarés: ${declared.join(', ')}\nUtilisés: ${used.join(', ')}`;
    }
    return used.length ? `Utilisés: ${used.join(', ')}` : declared.join(', ');
  }

  function openDetail(row: LogRow): void {
    selected = row;
    detail = null;
    detailState = 'loading';
    detailError = '';
    void apiGet<RequestDetail>(`/api/requests/${encodeURIComponent(row.id)}`)
      .then((data) => {
        detail = data;
        detailState = 'ok';
      })
      .catch((e: unknown) => {
        detailState = 'error';
        detailError = e instanceof Error ? e.message : String(e);
      });
  }

  function closeDetail(): void {
    selected = null;
    detail = null;
  }

  /** Fermeture par Échap — le focus reste géré par le rôle `dialog`. */
  function onKeydown(e: KeyboardEvent): void {
    if (e.key === 'Escape' && selected) closeDetail();
  }

  function jsonSize(data: unknown): string {
    const size = JSON.stringify(data ?? null).length;
    return size > 1024 ? `${(size / 1024).toFixed(1)} KB` : `${size} B`;
  }

  function jsonText(data: unknown): string {
    return JSON.stringify(data, null, 2);
  }
</script>

<svelte:window onkeydown={onKeydown} />

<Card title={t('logs.title')}>
  <FilterBar />

  <div class="filters">
    <label class="field">
      <span class="field-label">{t('logs.status')}</span>
      <select class="select" bind:value={draft.status}>
        <option value="">{t('filter.all')}</option>
        <option value="success">{t('stats.success')}</option>
        <option value="error">{t('status.error')}</option>
      </select>
    </label>

    <label class="field">
      <span class="field-label">{t('stats.model')}</span>
      <select class="select" bind:value={draft.model}>
        <option value="">{t('filter.all')}</option>
        {#each filtersQ.data?.models ?? [] as m (m)}
          <option value={m}>{m}</option>
        {/each}
      </select>
    </label>

    <label class="field">
      <span class="field-label">{t('logs.original_model')}</span>
      <select class="select" bind:value={draft.original_model}>
        <option value="">{t('filter.all')}</option>
        {#each filtersQ.data?.original_models ?? [] as m (m)}
          <option value={m}>{m}</option>
        {/each}
      </select>
    </label>

    <label class="field">
      <span class="field-label">{t('stats.account')}</span>
      <select class="select" bind:value={draft.account}>
        <option value="">{t('filter.all')}</option>
        {#each filtersQ.data?.accounts ?? [] as a (a)}
          <option value={a}>{a}</option>
        {/each}
      </select>
    </label>

    <label class="field">
      <span class="field-label">{t('logs.tools')}</span>
      <select class="select" bind:value={draft.tool}>
        <option value="">{t('filter.all')}</option>
        {#each filtersQ.data?.tools_used ?? [] as tool (tool)}
          <option value={tool}>{tool}</option>
        {/each}
      </select>
    </label>

    <label class="field field--search">
      <span class="field-label">{t('logs.search')}</span>
      <input
        class="input"
        type="search"
        placeholder={t('logs.search_placeholder')}
        bind:value={draft.search}
        onkeydown={(e) => {
          if (e.key === 'Enter') applyFilters();
        }}
      />
    </label>

    <div class="field-actions">
      <button class="btn btn--primary btn--sm" onclick={applyFilters}>{t('filter.apply')}</button>
      <button class="btn btn--sm" onclick={resetFilters}>{t('filter.reset')}</button>
    </div>
  </div>

  {#if historyQ.loading && rows.length === 0}
    <Spinner />
  {:else if historyQ.error && rows.length === 0}
    <Empty kind="error" title={t('status.error')} />
  {:else if rows.length === 0}
    <Empty title={t('logs.no_data')} />
  {:else}
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{t('logs.account')}</th>
            <th>{t('logs.time')}</th>
            <th>{t('logs.original_model')}</th>
            <th>{t('logs.mapped_model')}</th>
            <th class="num">{t('stats.input')}</th>
            <th class="num">{t('stats.output')}</th>
            <th class="num">{t('stats.cache')}</th>
            <th>{t('logs.thinking')}</th>
            <th>{t('logs.effort')}</th>
            <th>{t('logs.tools')}</th>
            <th>IP</th>
            <th class="num">{t('logs.duration')}</th>
            <th>{t('logs.status')}</th>
          </tr>
        </thead>
        <tbody>
          {#each rows as log (log.id)}
            {@const used = unique(log.tools_used)}
            {@const declared = unique(log.tools)}
            <tr class="row" onclick={() => openDetail(log)}>
              <td>
                {#if log.is_free_model}
                  <span class="free" title={t('logs.free_model')}>
                    {log.free_ip || t('vpn.ip_free')}
                  </span>
                {:else}
                  {log.account_alias || '-'}
                {/if}
              </td>
              <td class="nowrap">{formatDateTime(log.timestamp)}</td>
              <td>{log.original_model || '-'}</td>
              <td>
                {log.model || '-'}
                {#if log.geo_via_vpn}
                  <br /><span class="badge badge--vpn" title={t('geo.direct_via_vpn', {
                    station: '?',
                    vpnCountry: log.geo_country ?? '?',
                    vpnIp: log.free_ip || log.geo_country || '',
                    model: log.model || '',
                    allowed: (log.geo_allowed ?? '')
                      .split(',')
                      .map((s) => s.trim())
                      .filter(Boolean)
                      .join(', '),
                    directIp: log.geo_direct_ip ?? '?',
                    directCountry: log.geo_direct_country ?? '?'
                  })}>{t('geo.badge_vpn')} {log.geo_direct_country ?? '?'}→{log.geo_country ?? '?'}</span>
                {:else if log.geo_direct_country}
                  <br /><span class="badge badge--direct" title="{log.geo_direct_country} {log.geo_direct_ip ?? ''}">
                    {t('geo.badge_direct')} {log.geo_direct_country}
                  </span>
                {/if}
              </td>
              <td class="num mono">{formatNumber(log.tokens_input)}</td>
              <td class="num mono">{formatNumber(log.tokens_output)}</td>
              <td class="num mono">{formatNumber(log.tokens_cache)}</td>
              <td>{log.thinking || '-'}</td>
              <td>{log.effort || '-'}</td>
              <td class="tools">
                {#if used.length}
                  <span title={toolTitle(log)}>
                    {#each used.slice(0, MAX_TOOL_BADGES) as tool (tool)}
                      <span class="badge badge--used">{tool}</span>
                    {/each}
                    {#if used.length > MAX_TOOL_BADGES}
                      <span class="badge badge--declared">+{used.length - MAX_TOOL_BADGES}</span>
                    {/if}
                  </span>
                {:else if declared.length}
                  <span title={declared.join(', ')}>
                    <span class="badge badge--declared">
                      {t('logs.tools_declared_count', { n: declared.length })}
                    </span>
                  </span>
                {:else}
                  -
                {/if}
              </td>
              <td class="mono">{log.client_ip || '-'}</td>
              <td class="num mono">
                {log.duration_ms ? formatNumber(log.duration_ms) : '-'}
              </td>
              <td class="status-cell">
                {#if log.success}
                  <span class="ok">✔</span>
                {:else}
                  <span class="fail">✘</span>
                  <span class="error-text">{log.error}</span>
                  <span
                    class="info"
                    title="{t('status.error')}: {log.error}
{getErrorDetails(log.error || '').explanation}
Model: {log.model || '-'} | Protocol: {log.protocol || '-'}">ⓘ</span>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <Pagination
      page={page}
      total={historyQ.data?.total ?? 0}
      pageSize={PAGE_SIZE}
      onchange={(p) => setParams({ page: p > 1 ? String(p) : null })}
    />
  {/if}
</Card>

{#if selected}
  <div
    class="backdrop"
    role="presentation"
    onclick={closeDetail}
    onkeydown={onKeydown}
  >
    <div
      class="modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="req-detail-title"
      tabindex="-1"
      onclick={(e) => e.stopPropagation()}
      onkeydown={onKeydown}
    >
      <div class="modal-head">
        <h3 id="req-detail-title">{t('logs.title')}</h3>
        <button class="btn btn--sm" aria-label={t('common.close')} onclick={closeDetail}>✕</button>
      </div>

      {#if detailState === 'loading'}
        <Spinner />
      {:else if detailState === 'error'}
        <p class="error-text">{t('logs.detail_failed', { msg: detailError })}</p>
      {:else if detail}
        <div class="detail-grid">
          <span class="detail-label">{t('logs.status')}</span>
          <span class="detail-value">
            {#if detail.success}
              <span class="ok">✔ {t('stats.success')}</span>
            {:else}
              <span class="fail">✘ {detail.error || t('status.error')}</span>
            {/if}
          </span>

          <span class="detail-label">ID</span>
          <span class="detail-value mono">{detail.id}</span>

          <span class="detail-label">{t('logs.timestamp')}</span>
          <span class="detail-value">{formatDateTime(detail.timestamp)}</span>

          <span class="detail-label">{t('stats.account')}</span>
          <span class="detail-value">{detail.account_alias || '-'}</span>

          <span class="detail-label">{t('logs.client_ip')}</span>
          <span class="detail-value">{detail.client_ip || '-'}</span>

          <span class="detail-label">{t('logs.client')}</span>
          <span class="detail-value mono">{detail.client_user_agent || '-'}</span>

          <span class="detail-label">{t('logs.original_model')}</span>
          <span class="detail-value">{detail.original_model || '-'}</span>

          <span class="detail-label">{t('logs.mapped_model')}</span>
          <span class="detail-value">{detail.model || '-'}</span>

          <span class="detail-label">{t('logs.protocol')}</span>
          <span class="detail-value">{detail.protocol || '-'}</span>

          <span class="detail-label">{t('logs.stream')}</span>
          <span class="detail-value">{detail.is_stream ? t('logs.yes') : t('logs.no')}</span>

          <span class="detail-label">{t('logs.duration')}</span>
          <span class="detail-value">
            {detail.duration_ms ? `${formatNumber(detail.duration_ms)} ms` : '-'}
          </span>

          <span class="detail-label">{t('stats.input')}</span>
          <span class="detail-value mono">{formatNumber(detail.tokens_input)}</span>

          <span class="detail-label">{t('stats.output')}</span>
          <span class="detail-value mono">{formatNumber(detail.tokens_output)}</span>

          <span class="detail-label">{t('stats.cache')}</span>
          <span class="detail-value mono">{formatNumber(detail.tokens_cache)}</span>

          <span class="detail-label">{t('logs.thinking')}</span>
          <span class="detail-value">{detail.thinking || '-'}</span>

          <span class="detail-label">{t('logs.effort')}</span>
          <span class="detail-value">{detail.effort || '-'}</span>
        </div>

        <div class="section">
          <h4>{t('logs.tools_used', { n: unique(detail.tools_used).length })}</h4>
          <div class="tools-list">
            {#if unique(detail.tools_used).length}
              {#each unique(detail.tools_used) as tool (tool)}
                <span class="badge badge--used">{tool}</span>
              {/each}
            {:else}
              <span class="muted">-</span>
            {/if}
          </div>
        </div>

        <div class="section">
          <h4>{t('logs.tools_declared', { n: unique(detail.tools).length })}</h4>
          <div class="tools-list">
            {#if unique(detail.tools).length}
              {#each unique(detail.tools) as tool (tool)}
                <span class="badge badge--declared">{tool}</span>
              {/each}
            {:else}
              <span class="muted">-</span>
            {/if}
          </div>
        </div>

        {#each [
          { key: 'request-body', title: t('logs.request_body'), data: detail.request_body },
          { key: 'response-body', title: t('logs.response_body'), data: detail.response_body }
        ] as section (section.key)}
          <div class="section">
            <button
              class="json-toggle"
              aria-expanded={openJson[section.key]}
              onclick={() => (openJson[section.key] = !openJson[section.key])}
            >
              {openJson[section.key] ? '▼' : '▶'} {section.title}
              <span class="json-size">({jsonSize(section.data)})</span>
            </button>
            {#if openJson[section.key]}
              <pre class="json-block"><code>{jsonText(section.data)}</code></pre>
            {/if}
          </div>
        {/each}
      {/if}
    </div>
  </div>
{/if}

<style>
  .filters {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--sp-3);
    margin-bottom: var(--sp-4);
  }

  .field {
    display: grid;
    gap: var(--sp-1);
    min-width: 130px;
  }

  .field--search {
    flex: 1 1 160px;
    max-width: 220px;
  }

  .field-label {
    font-size: var(--fs-xs);
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .field-actions {
    display: flex;
    gap: var(--sp-2);
  }

  .table-wrap {
    overflow-x: auto;
  }

  .row {
    cursor: pointer;
  }

  .row:hover {
    background: var(--surface-2);
  }

  .nowrap {
    white-space: nowrap;
  }

  .mono {
    font-family: var(--font-mono);
    font-size: var(--fs-sm);
  }

  .num {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  .free {
    color: var(--success);
    font-weight: 700;
  }

  .badge {
    display: inline-block;
    font-size: var(--fs-xs);
    padding: 1px 5px;
    border-radius: var(--radius-sm);
    margin: 1px 2px 1px 0;
    white-space: nowrap;
  }

  .badge--used {
    background: color-mix(in srgb, var(--accent) 22%, transparent);
    color: var(--text);
    border: 1px solid color-mix(in srgb, var(--accent) 45%, transparent);
  }

  .badge--declared {
    background: var(--surface-3);
    color: var(--text-dim);
    border: 1px solid var(--border);
  }

  .badge--vpn {
    background: var(--warn);
    color: #fff;
  }

  .badge--direct {
    background: var(--surface-3);
    border: 1px solid var(--border);
    color: var(--text-dim);
  }

  .tools {
    max-width: 220px;
  }

  .status-cell {
    white-space: nowrap;
  }

  .ok {
    color: var(--success);
    font-weight: 700;
  }

  .fail {
    color: var(--danger);
    font-weight: 700;
  }

  .error-text {
    color: var(--danger);
    font-size: var(--fs-sm);
  }

  .info {
    color: var(--text-dim);
    cursor: help;
    margin-left: var(--sp-1);
  }

  .muted {
    color: var(--text-dim);
  }

  /* ── Fiche détaillée ── */
  .backdrop {
    position: fixed;
    inset: 0;
    background: rgb(0 0 0 / 55%);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: var(--sp-4);
    z-index: var(--z-modal, 1000);
  }

  .modal {
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-lg);
    width: min(860px, 100%);
    max-height: 86vh;
    overflow-y: auto;
    padding: var(--sp-5);
    box-shadow: var(--shadow-lg);
  }

  .modal-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--sp-3);
    margin-bottom: var(--sp-4);
  }

  .modal-head h3 {
    margin: 0;
    font-size: var(--fs-lg);
  }

  .detail-grid {
    display: grid;
    grid-template-columns: minmax(120px, auto) 1fr;
    gap: var(--sp-2) var(--sp-4);
    margin-bottom: var(--sp-4);
  }

  .detail-label {
    color: var(--text-dim);
    font-size: var(--fs-sm);
  }

  .detail-value {
    color: var(--text);
    font-size: var(--fs-sm);
    word-break: break-word;
  }

  .section {
    margin-top: var(--sp-4);
  }

  .section h4 {
    margin: 0 0 var(--sp-2);
    font-size: var(--fs-md);
    color: var(--text);
  }

  .tools-list {
    display: flex;
    flex-wrap: wrap;
    gap: var(--sp-1);
  }

  .json-toggle {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    width: 100%;
    background: none;
    border: 0;
    padding: var(--sp-2) 0;
    color: var(--text);
    font-size: var(--fs-md);
    font-weight: 600;
    cursor: pointer;
    text-align: left;
  }

  .json-size {
    color: var(--text-faint);
    font-weight: 400;
    font-size: var(--fs-sm);
  }

  .json-block {
    margin: 0;
    padding: var(--sp-3);
    background: var(--surface-3);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    max-height: 320px;
    overflow: auto;
    font-family: var(--font-mono);
    font-size: var(--fs-xs);
    white-space: pre-wrap;
    word-break: break-all;
  }
</style>
