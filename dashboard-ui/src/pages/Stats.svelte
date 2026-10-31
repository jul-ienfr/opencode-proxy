<script lang="ts">
  /**
   * Vue « Statistiques » — KPI, graphiques et tableaux par modèle/compte.
   *
   * Ce qui change par rapport à l'ancien `renderStats()` :
   *  - la période vit dans l'URL (`#stats?from=…&to=…`) : rafraîchir ou
   *    partager un lien reste sur le même écran ;
   *  - graphiques uPlot au lieu de Chart.js chargé depuis un CDN : la vue est
   *    dans le bundle, aucun appel réseau externe, ~45 Ko au lieu de ~200 ;
   *  - la granularité suit la période (heure ≤ 14 j, sinon jour) pour ne pas
   *    envoyer 720 points quand on regarde un mois ;
   *  - Svelte échappe les valeurs : plus d'`escHtml` à rappeler à la main.
   */

  import { apiGet } from '$lib/api';
  import { createQuery } from '$lib/query.svelte';
  import { param } from '$lib/router.svelte';
  import { formatNumber, toDateInput } from '$lib/format';
  import { t } from '$lib/i18n.svelte';
  import {
    CHART_COLORS,
    periodLabel,
    type ChartSeries,
    type DonutSegment
  } from '$lib/chart';
  import Card from '$lib/components/Card.svelte';
  import Donut from '$lib/components/Donut.svelte';
  import Empty from '$lib/components/Empty.svelte';
  import FilterBar from '$lib/components/FilterBar.svelte';
  import Pagination from '$lib/components/Pagination.svelte';
  import Spinner from '$lib/components/Spinner.svelte';
  import UPlot from '$lib/components/UPlot.svelte';

  interface StatRow {
    input: number;
    output: number;
    cache: number;
    total: number;
    count?: number;
    pct?: number;
    cache_hit_rate?: number | null;
    success_rate?: number | null;
    success_count: number;
    fail_count: number;
    avg_duration_ms?: number | null;
  }

  interface Rates {
    rpm_1m?: number | null;
    c1m?: number | null;
    rpm_1h?: number | null;
    c1h?: number | null;
    avg_per_min?: number | null;
  }

  interface StatsResponse {
    totals: StatRow & { count: number };
    models: Record<string, StatRow>;
    accounts?: Record<string, StatRow>;
    rates?: Rates;
  }

  interface TsPoint {
    period: string;
    count: number;
    fail: number;
    success: number;
    input_tokens: number;
    output_tokens: number;
    cache_tokens: number;
    avg_duration_ms?: number | null;
  }

  interface TsResponse {
    series: TsPoint[];
  }

  // ── Période (URL) ──
  const today = $derived(toDateInput(new Date()));
  const from = $derived(param('from', today));
  const to = $derived(param('to', today));
  const periodKey = $derived(`${from}|${to}`);

  /** Au-delà de 14 jours, on agrège par jour (sinon : ~700 points). */
  const granularity = $derived.by(() => {
    const start = Date.parse(`${from}T00:00`);
    const end = Date.parse(`${to}T00:00`);
    if (Number.isNaN(start) || Number.isNaN(end)) return 'hour';
    return (end - start) / 86_400_000 > 14 ? 'day' : 'hour';
  });

  const statsQ = createQuery<StatsResponse>({
    load: (signal) =>
      apiGet<StatsResponse>(
        `/api/stats?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`,
        signal
      ),
    tags: () => ['stats'],
    deps: () => periodKey
  });

  const tsQ = createQuery<TsResponse>({
    load: (signal) =>
      apiGet<TsResponse>(
        `/api/stats/timeseries?from_date=${encodeURIComponent(from)}&to_date=${encodeURIComponent(
          to
        )}&granularity=${granularity}`,
        signal
      ),
    tags: () => ['stats'],
    deps: () => `${periodKey}|${granularity}`
  });

  // ── KPI ──
  const totals = $derived(statsQ.data?.totals);
  const rates = $derived(statsQ.data?.rates ?? {});

  const kpis = $derived.by(() => {
    const tot = totals;
    if (!tot) return [];
    const box = (label: string, value: string, hint?: string) => ({ label, value, hint });
    return [
      box(t('stats.input'), formatNumber(tot.input)),
      box(t('stats.output'), formatNumber(tot.output)),
      box(t('stats.cache'), formatNumber(tot.cache)),
      box(t('stats.total'), formatNumber(tot.total)),
      box(t('stats.requests'), formatNumber(tot.count)),
      box(t('stats.success'), formatNumber(tot.success_count)),
      box(t('stats.failed'), formatNumber(tot.fail_count)),
      box(
        t('stats.avg_duration'),
        tot.avg_duration_ms ? formatNumber(tot.avg_duration_ms) : '—'
      ),
      box(t('stats.cache_hit_rate'), tot.cache_hit_rate != null ? `${tot.cache_hit_rate}%` : '0%'),
      box(t('stats.success_rate'), tot.success_rate != null ? `${tot.success_rate}%` : '—'),
      box(
        t('stats.rpm_1m'),
        rates.rpm_1m != null ? `${rates.rpm_1m.toFixed(1)}/min` : '—',
        rates.c1m != null ? `${rates.c1m}` : ''
      ),
      box(
        t('stats.rpm_1h'),
        rates.rpm_1h != null ? `${rates.rpm_1h.toFixed(2)}/min` : '—',
        rates.c1h != null ? `${rates.c1h}` : ''
      ),
      box(t('stats.avg_period'), rates.avg_per_min != null ? `${rates.avg_per_min.toFixed(2)}/min` : '—')
    ];
  });

  // ── Tableaux ──
  // L'API renvoie aussi les modèles/comptes sans activité sur la période
  // (0 requête, 0 token) : les lister encombre la page pour rien. On les
  // écarte, et on trie par volume décroissant pour lire le tableau du plus
  // significatif au plus marginal.
  const notEmpty = ([, s]: [string, StatRow]): boolean =>
    (s.count ?? 0) > 0 || s.total > 0;

  const modelRows = $derived(
    Object.entries(statsQ.data?.models ?? {})
      .filter(notEmpty)
      .sort((a, b) => b[1].total - a[1].total)
  );

  const accountRows = $derived(
    Object.entries(statsQ.data?.accounts ?? {})
      .filter(notEmpty)
      .sort((a, b) => b[1].total - a[1].total)
  );

  // ── Pagination des tableaux ──
  // « Par compte » liste une IP de sortie par ligne : des milliers d'entrées.
  // On n'en montre qu'une page à la fois (25) — le DOM reste léger et la
  // lecture reste possible.
  const PAGE_SIZE = 25;
  let modelPage = $state(1);
  let accountPage = $state(1);

  const pageModels = $derived(
    modelRows.slice((modelPage - 1) * PAGE_SIZE, modelPage * PAGE_SIZE)
  );
  const pageAccounts = $derived(
    accountRows.slice((accountPage - 1) * PAGE_SIZE, accountPage * PAGE_SIZE)
  );

  // Changer de période repart à la première page (sinon on resterait sur une
  // page qui n'existe plus pour la nouvelle plage de dates).
  $effect(() => {
    void periodKey;
    modelPage = 1;
    accountPage = 1;
  });

  // ── Graphiques circulaires ──
  const tokenSegments = $derived.by<DonutSegment[]>(() => {
    const tot = totals;
    if (!tot) return [];
    return [
      { label: t('stats.input'), value: tot.input, color: CHART_COLORS[0] },
      { label: t('stats.output'), value: tot.output, color: CHART_COLORS[1] },
      { label: t('stats.cache'), value: tot.cache, color: CHART_COLORS[2] }
    ];
  });

  const modelTokenSegments = $derived.by<DonutSegment[]>(() =>
    modelRows.map(([model, s]) => ({ label: model, value: s.total }))
  );

  const modelRequestSegments = $derived.by<DonutSegment[]>(() =>
    modelRows.map(([model, s]) => ({ label: model, value: s.count ?? 0 }))
  );

  // ── Séries temporelles ──
  const points = $derived(tsQ.data?.series ?? []);
  const tsX = $derived(points.map((p) => p.period));
  const col = (pick: (p: TsPoint) => number) => points.map(pick);

  const requestsData = $derived([tsX, col((p) => p.count), col((p) => p.fail)]);
  const requestsSeries = $derived([
    { label: t('stats.total'), stroke: CHART_COLORS[2], bars: true },
    { label: t('stats.failed'), stroke: CHART_COLORS[1], width: 1.5 }
  ] as ChartSeries[]);

  const tokensData = $derived([
    tsX,
    col((p) => p.input_tokens),
    col((p) => p.output_tokens),
    col((p) => p.cache_tokens)
  ]);
  const tokensSeries = $derived([
    { label: t('stats.input'), stroke: CHART_COLORS[0], fill: 'rgba(79,195,247,0.14)' },
    { label: t('stats.output'), stroke: CHART_COLORS[1], fill: 'rgba(255,138,101,0.14)' },
    { label: t('stats.cache'), stroke: CHART_COLORS[2], fill: 'rgba(129,199,132,0.14)' }
  ] as ChartSeries[]);

  const durationData = $derived([tsX, col((p) => p.avg_duration_ms ?? 0)]);
  const durationSeries = $derived([
    { label: t('stats.avg_duration'), stroke: '#ce93d8', fill: 'rgba(206,147,216,0.16)' }
  ] as ChartSeries[]);

  const outcomeData = $derived([tsX, col((p) => p.success), col((p) => p.fail)]);
  // Deux lignes plutôt que des barres groupées : uPlot ne sait pas décaler
  // horizontalement deux séries de barres (align n'accepte que 0/1/-1).
  const outcomeSeries = $derived([
    { label: t('stats.success'), stroke: CHART_COLORS[2] },
    { label: t('stats.failed'), stroke: CHART_COLORS[1], width: 1.5 }
  ] as ChartSeries[]);

  /** Axe des ordonnées compact (1.2M) — le survol reste exact. */
  function compact(v: number): string {
    const abs = Math.abs(v);
    if (abs >= 1e9) return `${(v / 1e9).toFixed(1)}G`;
    if (abs >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
    if (abs >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
    return `${v}`;
  }

  const xLabels = $derived(tsX.map(periodLabel));

  const columnHeaders = $derived([
    t('stats.input'),
    t('stats.output'),
    t('stats.cache'),
    t('stats.total'),
    t('stats.pct'),
    t('stats.cache_hit_rate'),
    t('stats.success_rate'),
    t('stats.success'),
    t('stats.failed'),
    t('stats.avg_duration')
  ]);
</script>

<div class="view">
  <FilterBar />

  <!-- ── Aperçu ── -->
  <Card title={t('stats.overview')}>
    {#if statsQ.loading && !totals}
      <Spinner />
    {:else if statsQ.error && !totals}
      <Empty kind="error" title={t('error.unknown')} hint={statsQ.error.message} />
    {:else}
      <div class="kpis">
        {#each kpis as kpi (kpi.label)}
          <div class="kpi">
            <span class="kpi-label">{kpi.label}</span>
            <span class="kpi-value">{kpi.value}</span>
            {#if kpi.hint}<span class="kpi-hint" title={kpi.hint}>…</span>{/if}
          </div>
        {/each}
      </div>
    {/if}
  </Card>

  <!-- ── Répartitions ── -->
  <div class="charts charts--donuts">
    <Card title={t('chart.token_dist')}>
      <Donut
        segments={tokenSegments}
        centerValue={formatNumber(totals?.total ?? 0)}
        centerLabel={t('stats.total')}
      />
    </Card>
    <Card title={t('chart.token_pct')}>
      <Donut segments={modelTokenSegments} centerValue={formatNumber(totals?.total ?? 0)} />
    </Card>
    <Card title={t('chart.req_pct')}>
      <Donut segments={modelRequestSegments} centerValue={formatNumber(totals?.count ?? 0)} />
    </Card>
  </div>

  <!-- ── Séries temporelles ── -->
  <div class="charts charts--pair">
    <Card title={t('chart.ts_requests')}>
      {#if points.length}
        <UPlot
          data={requestsData}
          series={requestsSeries}
          xType="time"
          {xLabels}
          height={210}
          yFormat={compact}
        />
      {:else}
        <Empty title={t('stats.no_data')} />
      {/if}
    </Card>
    <Card title={t('chart.ts_tokens')}>
      {#if points.length}
        <UPlot
          data={tokensData}
          series={tokensSeries}
          xType="time"
          {xLabels}
          height={210}
          yFormat={compact}
        />
      {:else}
        <Empty title={t('stats.no_data')} />
      {/if}
    </Card>
  </div>

  <div class="charts charts--pair">
    <Card title={t('chart.ts_duration')}>
      {#if points.length}
        <UPlot data={durationData} series={durationSeries} xType="time" {xLabels} height={210} />
      {:else}
        <Empty title={t('stats.no_data')} />
      {/if}
    </Card>
    <Card title={t('chart.ts_success')}>
      {#if points.length}
        <UPlot
          data={outcomeData}
          series={outcomeSeries}
          xType="time"
          {xLabels}
          height={210}
          yFormat={compact}
        />
      {:else}
        <Empty title={t('stats.no_data')} />
      {/if}
    </Card>
  </div>

  <!-- ── Par modèle ── -->
  <Card title={t('stats.by_model')}>
    {#if modelRows.length === 0}
      <Empty title={t('stats.no_data')} />
    {:else}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th class="th-name">{t('stats.model')}</th>
              {#each columnHeaders as header (header)}
                <th class="num">{header}</th>
              {/each}
            </tr>
          </thead>
          <tbody>
            {#each pageModels as [model, s] (model)}
              <tr>
                <td class="name mono">{model}</td>
                <td class="num">{formatNumber(s.input)}</td>
                <td class="num">{formatNumber(s.output)}</td>
                <td class="num">{formatNumber(s.cache)}</td>
                <td class="num">{formatNumber(s.total)}</td>
                <td class="num">{s.pct ?? 0}</td>
                <td class="num">{s.cache_hit_rate != null ? `${s.cache_hit_rate}%` : '0%'}</td>
                <td class="num">{s.success_rate != null ? `${s.success_rate}%` : '—'}</td>
                <td class="num">{formatNumber(s.success_count)}</td>
                <td class="num">{formatNumber(s.fail_count)}</td>
                <td class="num">{s.avg_duration_ms ? formatNumber(s.avg_duration_ms) : '—'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <Pagination
        page={modelPage}
        total={modelRows.length}
        pageSize={PAGE_SIZE}
        onchange={(p) => (modelPage = p)}
      />
    {/if}
  </Card>

  <!-- ── Par compte ── -->
  {#if accountRows.length > 0}
    <Card title={t('stats.by_account')}>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th class="th-name">{t('stats.account')}</th>
              {#each columnHeaders as header (header)}
                <th class="num">{header}</th>
              {/each}
            </tr>
          </thead>
          <tbody>
            {#each pageAccounts as [account, s] (account)}
              <tr>
                <td class="name mono">{account}</td>
                <td class="num">{formatNumber(s.input)}</td>
                <td class="num">{formatNumber(s.output)}</td>
                <td class="num">{formatNumber(s.cache)}</td>
                <td class="num">{formatNumber(s.total)}</td>
                <td class="num">{s.pct ?? 0}</td>
                <td class="num">{s.cache_hit_rate != null ? `${s.cache_hit_rate}%` : '0%'}</td>
                <td class="num">{s.success_rate != null ? `${s.success_rate}%` : '—'}</td>
                <td class="num">{formatNumber(s.success_count)}</td>
                <td class="num">{formatNumber(s.fail_count)}</td>
                <td class="num">{s.avg_duration_ms ? formatNumber(s.avg_duration_ms) : '—'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <Pagination
        page={accountPage}
        total={accountRows.length}
        pageSize={PAGE_SIZE}
        onchange={(p) => (accountPage = p)}
      />
    </Card>
  {/if}
</div>

<style>
  .view {
    display: grid;
    gap: var(--sp-4);
  }

  /* --- KPI --- */
  .kpis {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(148px, 1fr));
    gap: var(--sp-3);
  }

  .kpi {
    display: grid;
    gap: 2px;
    padding: var(--sp-3);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--surface-2);
    min-width: 0;
  }

  .kpi-label {
    font-size: var(--fs-xs);
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .kpi-value {
    font-size: var(--fs-xl);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .kpi-hint {
    font-size: var(--fs-xs);
    color: var(--text-faint);
  }

  /* --- Graphiques --- */
  .charts {
    display: grid;
    gap: var(--sp-4);
  }

  .charts--donuts {
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  }

  .charts--pair {
    grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  }

  /* --- Tableaux --- */
  .table-wrap {
    overflow-x: auto;
    scrollbar-width: thin;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--fs-md);
  }

  th,
  td {
    padding: 7px 10px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }

  thead th {
    position: sticky;
    top: 0;
    background: var(--surface);
    color: var(--text-dim);
    font-size: var(--fs-sm);
    font-weight: 600;
    text-align: left;
    z-index: 1;
  }

  .num {
    text-align: right;
    font-variant-numeric: tabular-nums;
    font-family: var(--font-mono);
    font-size: var(--fs-sm);
  }

  .name {
    font-size: var(--fs-sm);
    color: var(--text);
  }

  tbody tr:hover {
    background: var(--surface-2);
  }

  @media (max-width: 640px) {
    .charts--pair,
    .charts--donuts {
      grid-template-columns: 1fr;
    }
  }
</style>
