<script lang="ts">
  /**
   * Capture de trafic « façon Wireshark » : chaque requête client sur le fil.
   *
   * L'ancienne interface rafraîchissait les trois sources (`frames`, `stats`,
   * `status`) en parallèle toutes les 3 s quand l'onglet était actif — ici
   * l'onglet n'existe que monté, donc l'intervalle vit dans un `$effect`
   * nettoyé au démontage.
   */

  import { apiGet, apiPost } from '$lib/api';
  import { t } from '$lib/i18n.svelte';
  import { createQuery } from '$lib/query.svelte';
  import { showToast } from '$lib/toast.svelte';
  import { getErrorDetails } from '$lib/format';
  import Card from '$lib/components/Card.svelte';
  import Empty from '$lib/components/Empty.svelte';
  import Spinner from '$lib/components/Spinner.svelte';

  /** Rafraîchissement automatique — identique à l'implémentation historique. */
  const AUTO_REFRESH_MS = 3000;
  /** Anti-rebond du filtre de chemin. */
  const FILTER_DEBOUNCE_MS = 300;
  const FRAMES_LIMIT = 200;

  interface Frame {
    id: string;
    time: string;
    time_ms: number;
    delta_ms: number | null;
    method: string;
    path: string;
    query?: string;
    status: number | null;
    ttfb_ms: number | null;
    duration_ms: number | null;
    body_len: number;
    aborted?: boolean;
    abort_reason?: string;
    truncated?: boolean;
    request_line: string;
    client_ip?: string;
    client_port?: number;
  }

  interface FrameDetail extends Frame {
    headers?: { name: string; value: string }[];
    body_hex?: { offset: string; hex: string; ascii: string }[];
  }

  interface TrafficStats {
    rps?: number;
    tempo_delta_ms?: { p50: number; p95: number };
    ttfb_ms?: { p95: number };
    duration_ms?: { max: number };
    aborted?: number;
    in_flight?: number;
  }

  interface TrafficStatus {
    enabled?: boolean;
    user_enabled?: boolean;
    frames?: number;
    bytes_stored?: number;
  }

  interface TrafficBundle {
    frames: Frame[];
    status?: TrafficStatus;
    stats?: TrafficStats;
  }

  /** Méthodes colorées — repris de la constante `METHOD_COLORS` du legacy. */
  const METHOD_COLORS: Record<string, string> = {
    GET: 'var(--success)',
    POST: 'var(--accent)',
    PUT: 'var(--warn)',
    DELETE: 'var(--danger)',
    PATCH: 'var(--warn)',
    OPTIONS: 'var(--text-dim)'
  };

  let filter = $state('');
  let filterInput = $state('');
  let autoRefresh = $state(true);
  let selectedId = $state<string | null>(null);
  let detail = $state<FrameDetail | null>(null);
  let detailLoading = $state(false);
  let statusMsg = $state<{ kind: 'ok' | 'error'; message: string } | null>(null);
  /** Toggle piloté par l'état serveur : la capture est lazy côté proxy. */
  let captureOn = $state(false);

  const trafficQ = createQuery<TrafficBundle>({
    load: async (signal) => {
      const path = filter ? `&path=${encodeURIComponent(filter)}` : '';
      const [frames, stats, status] = await Promise.all([
        apiGet<{ frames: Frame[] }>(`/api/traffic/frames?limit=${FRAMES_LIMIT}${path}`, signal),
        apiGet<TrafficStats>('/api/traffic/stats?window=60.0', signal),
        apiGet<TrafficStatus>('/api/traffic/status', signal)
      ]);
      return { frames: frames.frames ?? [], stats, status };
    },
    tags: () => ['traffic'],
    deps: () => filter,
    initial: { frames: [] }
  });

  const frames = $derived(trafficQ.data?.frames ?? []);
  const stats = $derived(trafficQ.data?.stats);
  const status = $derived(trafficQ.data?.status);

  /** Intention du toggle : `user_enabled` si présent, sinon l'état effectif. */
  $effect(() => {
    if (!status) return;
    captureOn = status.user_enabled !== undefined ? !!status.user_enabled : !!status.enabled;
  });

  $effect(() => {
    if (!autoRefresh) return;
    const id = setInterval(() => {
      void trafficQ.refresh(true);
    }, AUTO_REFRESH_MS);
    return () => clearInterval(id);
  });

  /** Anti-rebond du filtre : le legacy débounçait aussi à 300 ms. */
  let filterTimer: number | undefined;
  $effect(() => {
    const value = filterInput.trim();
    window.clearTimeout(filterTimer);
    filterTimer = window.setTimeout(() => {
      filter = value;
    }, FILTER_DEBOUNCE_MS);
    return () => window.clearTimeout(filterTimer);
  });

  function flash(message: string, kind: 'ok' | 'error'): void {
    statusMsg = { kind, message };
    window.setTimeout(() => {
      statusMsg = null;
    }, 2500);
  }

  async function setCapture(next: boolean): Promise<void> {
    try {
      await apiPost('/api/traffic/config', { enabled: next });
      flash(t('traffic.settings_changed'), 'ok');
      await trafficQ.refresh(true);
    } catch (e) {
      captureOn = !next;
      flash(t('status.error'), 'error');
      showToast(getErrorDetails(e instanceof Error ? e.message : String(e)).raw, 'error');
    }
  }

  async function clearCapture(): Promise<void> {
    try {
      await apiPost('/api/traffic/clear');
      flash(t('traffic.cleared'), 'ok');
      selectedId = null;
      detail = null;
      await trafficQ.refresh(true);
    } catch (e) {
      flash(t('status.error'), 'error');
      showToast(getErrorDetails(e instanceof Error ? e.message : String(e)).raw, 'error');
    }
  }

  async function openDetail(id: string): Promise<void> {
    selectedId = id;
    detail = null;
    detailLoading = true;
    try {
      detail = await apiGet<FrameDetail>(`/api/traffic/frames/${encodeURIComponent(id)}`);
    } catch {
      flash(t('status.error'), 'error');
    } finally {
      detailLoading = false;
    }
  }

  /** `1234` → « 1.2s », `< 1000` → « 123ms » (reprise de `formatDur`). */
  function formatDur(ms: number | null | undefined): string {
    if (ms === null || ms === undefined || Number.isNaN(ms)) return t('traffic.unknown');
    if (ms < 1000) return `${Math.round(ms)}ms`;
    return `${(ms / 1000).toFixed(2)}s`;
  }

  function methodColor(method: string): string {
    return METHOD_COLORS[method] ?? 'var(--text)';
  }

  function statusColor(statusCode: number | null): string {
    if (statusCode === null) return 'var(--text-dim)';
    if (statusCode >= 500) return 'var(--danger)';
    if (statusCode >= 400) return 'var(--warn)';
    return 'var(--success)';
  }

  function sizeLabel(bytes: number | null | undefined): string {
    return bytes && bytes > 0 ? `${(bytes / 1024).toFixed(1)}KB` : '–';
  }

  /**
   * Sparkline de tempo : delta d'arrivée entre frames, de la plus ancienne à
   * la plus récente (les frames arrivent de la plus récente à la plus
   * ancienne, d'où l'inversion). Un delta < 5 ms est marqué « chaud ».
   */
  const spark = $derived.by(() => {
    if (frames.length <= 1) return { deltas: [] as number[], max: 1 };
    const deltas = [...frames].reverse().map((f) => f.delta_ms || 0);
    return { deltas, max: Math.max(...deltas, 1) };
  });

  function pad3(n: number): string {
    return String(n).padStart(3, '0');
  }
</script>

<Card title={t('traffic.title')}>
  <p class="hint">{t('traffic.hint')}</p>

  <div class="controls">
    <label class="check">
      <input
        type="checkbox"
        checked={captureOn}
        onchange={(e) => setCapture(e.currentTarget.checked)}
      />
      <span>{t('traffic.capture')}</span>
    </label>

    <button class="btn btn--sm" onclick={clearCapture}>{t('traffic.clear')}</button>

    <label class="check">
      <input type="checkbox" bind:checked={autoRefresh} />
      <span>{t('traffic.auto_refresh')}</span>
    </label>

    <input
      class="input filter"
      type="search"
      placeholder={t('traffic.filter_placeholder')}
      aria-label={t('traffic.filter_placeholder')}
      bind:value={filterInput}
    />

    {#if statusMsg}
      <span class="status status--{statusMsg.kind}" role="status">{statusMsg.message}</span>
    {/if}
  </div>

  {#if trafficQ.loading && frames.length === 0}
    <Spinner />
  {:else if trafficQ.error && frames.length === 0}
    <Empty kind="error" title={t('status.error')} />
  {:else}
    <div class="stats-bar">
      {#if status}
        <span><b>{status.frames ?? 0}</b> <strong>{t('traffic.frames')}</strong></span>
        <span class="sep">·</span>
        <span>
          <b>{status.bytes_stored ? `${(status.bytes_stored / 1024).toFixed(0)}KB` : '0B'}</b> cap
        </span>
        <span class="sep">·</span>
      {/if}
      {#if stats}
        {#if stats.rps !== undefined}
          <span><b>{stats.rps}</b> <strong>{t('traffic.rps')}</strong></span>
          <span class="sep">·</span>
        {/if}
        {#if stats.tempo_delta_ms}
          <span>
            {t('traffic.avg_delta')} <b>{stats.tempo_delta_ms.p50}ms</b> / p95
            <b>{stats.tempo_delta_ms.p95}ms</b>
          </span>
          <span class="sep">·</span>
        {/if}
        {#if stats.ttfb_ms}
          <span>{t('traffic.p95_ttfb')} <b>{formatDur(stats.ttfb_ms.p95)}</b></span>
          <span class="sep">·</span>
        {/if}
        {#if stats.duration_ms}
          <span>{t('traffic.slowest')} <b>{formatDur(stats.duration_ms.max)}</b></span>
          <span class="sep">·</span>
        {/if}
        {#if stats.aborted}
          <span>
            {t('traffic.aborted')} <b class="danger">{stats.aborted}</b>
          </span>
          <span class="sep">·</span>
        {/if}
        {#if stats.in_flight}
          <span>
            {t('traffic.in_flight')} <b class="warn">{stats.in_flight}</b>
          </span>
        {/if}
      {/if}

      {#if spark.deltas.length > 1}
        <span class="spark" aria-hidden="true">
          {#each spark.deltas as d, i (i)}
            <span
              class="spark-bar"
              class:spark-bar--hot={d > 0 && d < 5}
              style="height: {Math.max(2, Math.round((d / spark.max) * 20))}px"
              title="{d}ms"
            ></span>
          {/each}
        </span>
      {/if}
    </div>

    <div class="table-wrap">
      <table class="table table--traffic">
        <thead>
          <tr>
            <th>{t('traffic.time')}</th>
            <th>{t('traffic.delta')}</th>
            <th>{t('traffic.method')}</th>
            <th>{t('traffic.path')}</th>
            <th>{t('traffic.status')}</th>
            <th>{t('traffic.ttfb')}</th>
            <th>{t('traffic.duration')}</th>
            <th>{t('traffic.size')}</th>
            <th>{t('traffic.flags')}</th>
          </tr>
        </thead>
        <tbody>
          {#if frames.length === 0}
            <tr><td colspan="9" class="muted">{t('traffic.no_data')}</td></tr>
          {:else}
            {#each frames as f (f.id)}
              <tr
                class="row"
                class:row--aborted={f.aborted}
                class:row--selected={selectedId === f.id}
                onclick={() => openDetail(f.id)}
                title="{f.request_line} — {f.client_ip ?? ''}:{f.client_port ?? ''}"
              >
                <td class="nowrap mono">
                  {f.time}<span class="muted">.{pad3(f.time_ms)}</span>
                </td>
                <td class="nowrap mono num">
                  {f.delta_ms !== null && f.delta_ms !== undefined ? f.delta_ms.toFixed(1) : '-'}
                </td>
                <td class="method" style="color: {methodColor(f.method)}">{f.method}</td>
                <td class="path mono">{f.query ? `${f.path}?${f.query}` : f.path}</td>
                <td class="nowrap" style="color: {statusColor(f.status)}">
                  {f.status ?? '…'}
                </td>
                <td class="nowrap mono num">
                  {f.ttfb_ms !== null && f.ttfb_ms !== undefined ? f.ttfb_ms.toFixed(0) : '-'}
                </td>
                <td class="nowrap mono num">
                  {f.duration_ms !== null && f.duration_ms !== undefined
                    ? f.duration_ms.toFixed(0)
                    : '-'}
                </td>
                <td class="nowrap mono num">{sizeLabel(f.body_len)}</td>
                <td class="nowrap flags">
                  {#if f.aborted}
                    <span class="flag flag--abort" title={f.abort_reason ?? ''}>ABORT</span>
                  {/if}
                  {#if f.truncated}
                    <span class="flag flag--trunc">TRUNC</span>
                  {/if}
                  {#if f.status === null}
                    <span class="muted">…</span>
                  {/if}
                </td>
              </tr>
            {/each}
          {/if}
        </tbody>
      </table>
    </div>

    {#if detailLoading}
      <div class="detail"><Spinner /></div>
    {:else if detail}
      <div class="detail">
        <div class="detail-head">
          <div class="detail-title">
            {t('traffic.detail_title', {
              id: detail.id,
              method: detail.method,
              path: detail.query ? `${detail.path}?${detail.query}` : detail.path
            })}
          </div>
          <div class="detail-meta">
            <span class="method" style="color: {methodColor(detail.method)}; font-weight: 700">
              {detail.method}
            </span>
            <span class="mono">{detail.request_line}</span>
            {#if detail.aborted}<span class="flag flag--abort">ABORT</span>{/if}
            {#if detail.truncated}<span class="flag flag--trunc">TRUNC</span>{/if}
          </div>
          <div class="detail-meta muted">
            {detail.client_ip ?? '?'}:{detail.client_port ?? '?'} · {detail.time}.{pad3(detail.time_ms)}
            · Δ {detail.delta_ms?.toFixed(1) ?? '-'}ms
            · TTFB {detail.ttfb_ms !== null && detail.ttfb_ms !== undefined
              ? `${detail.ttfb_ms.toFixed(1)}ms`
              : '-'}
            · dur {detail.duration_ms !== null && detail.duration_ms !== undefined
              ? `${detail.duration_ms.toFixed(1)}ms`
              : '-'}
          </div>
          {#if detail.aborted && detail.abort_reason}
            <div class="detail-abort">⚠ {detail.abort_reason}</div>
          {/if}
        </div>

        <div class="detail-block">
          <b>{t('traffic.headers')}</b> ({detail.headers?.length ?? 0})
        </div>
        <div class="headers">
          {#if detail.headers && detail.headers.length > 0}
            {#each detail.headers as h, i (i)}
              <div><span class="muted">{h.name}</span>: <span class="mono">{h.value}</span></div>
            {/each}
          {:else}
            <span class="muted">—</span>
          {/if}
        </div>

        <div class="detail-block">
          <b>{t('traffic.body_hex', { n: detail.body_len ?? 0 })}</b>{#if detail.truncated}{t(
              'traffic.body_trunc'
            )}{/if}
        </div>
        <div class="hexdump">
          {#if detail.body_hex && detail.body_hex.length > 0}
            {#each detail.body_hex as row, i (i)}
              <div class="hexrow">
                <span class="muted offset">{row.offset}</span>
                <span class="hex">{row.hex}</span>
                <span>{row.ascii}</span>
              </div>
            {/each}
          {:else}
            <span class="muted">{t('traffic.no_body')}</span>
          {/if}
        </div>
      </div>
    {/if}
  {/if}
</Card>

<style>
  .hint {
    margin: 0 0 var(--sp-4);
    color: var(--text-dim);
    font-size: var(--fs-md);
  }

  .controls {
    display: flex;
    align-items: center;
    gap: var(--sp-4);
    flex-wrap: wrap;
    margin-bottom: var(--sp-3);
  }

  .check {
    display: inline-flex;
    align-items: center;
    gap: var(--sp-2);
    font-size: var(--fs-sm);
    color: var(--text);
    cursor: pointer;
    user-select: none;
  }

  .filter {
    flex: 1 1 160px;
    max-width: 280px;
  }

  .status {
    font-size: var(--fs-sm);
  }

  .status--ok {
    color: var(--success);
  }

  .status--error {
    color: var(--danger);
  }

  .stats-bar {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    flex-wrap: wrap;
    font-size: var(--fs-sm);
    color: var(--text-dim);
    margin-bottom: var(--sp-3);
    min-height: 22px;
  }

  .stats-bar b {
    color: var(--text);
    font-variant-numeric: tabular-nums;
  }

  .stats-bar .danger {
    color: var(--danger);
  }

  .stats-bar .warn {
    color: var(--warn);
  }

  .sep {
    color: var(--text-faint);
  }

  .spark {
    display: inline-flex;
    align-items: flex-end;
    gap: 1px;
    height: 22px;
    margin-left: var(--sp-2);
  }

  .spark-bar {
    width: 3px;
    background: var(--accent);
    opacity: 0.85;
  }

  .spark-bar--hot {
    background: var(--danger);
  }

  .table-wrap {
    overflow-x: auto;
  }

  .table--traffic {
    font-size: var(--fs-sm);
  }

  .row {
    cursor: pointer;
  }

  .row--aborted {
    background: color-mix(in srgb, var(--danger) 8%, transparent);
  }

  .row--selected {
    outline: 1px solid var(--accent);
    outline-offset: -1px;
    background: color-mix(in srgb, var(--accent) 10%, transparent);
  }

  .nowrap {
    white-space: nowrap;
  }

  .num {
    text-align: right;
  }

  .muted {
    color: var(--text-dim);
  }

  .method {
    font-weight: 600;
  }

  .path {
    max-width: 340px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--fs-sm);
  }

  .flags {
    display: flex;
    gap: var(--sp-1);
  }

  .flag {
    font-size: var(--fs-xs);
    font-weight: 700;
    padding: 1px 4px;
    border-radius: var(--radius-sm);
    border: 1px solid currentColor;
  }

  .flag--abort {
    color: var(--danger);
  }

  .flag--trunc {
    color: var(--warn);
  }

  .detail {
    margin-top: var(--sp-4);
    border-top: 1px solid var(--border);
    padding-top: var(--sp-3);
    font-size: var(--fs-sm);
  }

  .detail-head {
    border-bottom: 1px solid var(--border);
    padding-bottom: var(--sp-2);
    margin-bottom: var(--sp-3);
    display: grid;
    gap: var(--sp-1);
  }

  .detail-title {
    font-weight: 600;
    color: var(--text);
  }

  .detail-meta {
    display: flex;
    gap: var(--sp-2);
    flex-wrap: wrap;
    align-items: baseline;
  }

  .detail-abort {
    color: var(--danger);
  }

  .detail-block {
    margin-bottom: var(--sp-2);
  }

  .headers {
    display: grid;
    gap: 1px;
    max-height: 220px;
    overflow-y: auto;
    margin-bottom: var(--sp-3);
  }

  .hexdump {
    background: var(--surface-3);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: var(--sp-2);
    overflow-x: auto;
    max-height: 360px;
    overflow-y: auto;
  }

  .hexrow {
    display: flex;
    gap: var(--sp-3);
    white-space: pre;
    font-family: var(--font-mono);
    font-size: var(--fs-xs);
  }

  .offset {
    min-width: 48px;
  }

  .hex {
    min-width: 150px;
  }
</style>
