<script lang="ts">
  import uPlot from 'uplot';
  import 'uplot/dist/uPlot.min.css';
  import { theme } from '$lib/theme.svelte';
  import { formatNumber } from '$lib/format';
  import {
    categoryScale,
    categoryValues,
    CHART_COLORS,
    chartTheme,
    parsePeriod,
    type ChartSeries
  } from '$lib/chart';

  let {
    data = [],
    series = [],
    height = 200,
    bars = false,
    xType = 'cat',
    xLabels = [],
    yFormat = (v: number) => `${v}`
  }: {
    /**
     * `[xs, y1, y2, ...]` — même convention qu'uPlot.
     * `xs` peut être des index (abscisse catégorielle) ou des périodes serveur
     * (`"2026-06-05 14:00"`, converties en epoch au montage).
     */
    data: (number | string)[][];
    series: ChartSeries[];
    height?: number;
    /** Barres par défaut pour toutes les série sans `bars` explicite. */
    bars?: boolean;
    xType?: 'cat' | 'time';
    xLabels?: string[];
    yFormat?: (v: number) => string;
  } = $props();

  let host = $state<HTMLDivElement | undefined>(undefined);
  let plot: uPlot | null = null;
  let observer: ResizeObserver | null = null;

  /** Clé de reconstruction : thème + série (libellés/couleurs). Les données
   * seules ne déclenchent pas une reconstruction (on appelle `setData`). */
  const rebuildKey = $derived(
    `${theme.current}|${series.map((s) => `${s.label}:${s.stroke ?? ''}:${s.fill ?? ''}`).join('~')}|${xType}|${xLabels.join(',')}`
  );

  function build(el: HTMLDivElement): void {
    const themeColors = chartTheme();
    const xs = data[0] ?? [];
    const showLegend = series.length > 1;

    const plotSeries: uPlot.Series[] = [
      {
        label: '',
        value: (_u, v) => (v == null ? '' : `${v}`)
      }
    ];

    // Les séries en barres se partagent la largeur disponible. Attention :
    // uPlot n'accepte que `align: 0 | 1 | -1`, il n'y a donc PAS de décalage
    // horizontal possible : plusieurs séries en barres se superposeraient.
    // Une seule série en barres par graphique (l'autre passe en ligne).
    const barSeries = series.filter((s) => s.bars ?? bars);
    const band = 1 / Math.max(1, barSeries.length);

    series.forEach((s, i) => {
      const stroke = s.stroke ?? CHART_COLORS[i % CHART_COLORS.length];
      const asBars = s.bars ?? bars;
      const entry: uPlot.Series = {
        label: s.label,
        stroke,
        width: s.width ?? (asBars ? 0 : 2),
        points: { show: false },
        // Le survol affiche la valeur EXACTE, l'axe peut être compact.
        value: (_u, v) => (v == null ? '' : formatNumber(Number(v)))
      };
      if (asBars) {
        entry.paths = uPlot.paths!.bars!({ size: [0.9 * band, 100], align: 0 });
        // uPlot remplit les barres avec `series.fill` : avec `width: 0` et sans
        // `fill`, la série est dessinée… invisible (voir uPlot.esm.js : le
        // remplissage est `_fill`, pas `_stroke`).
        entry.fill = s.fill ?? stroke;
      }
      if (s.fill) entry.fill = s.fill;
      plotSeries.push(entry);
    });

    const themeColor = themeColors;
    const opts: uPlot.Options = {
      width: el.clientWidth || 480,
      height,
      legend: { show: showLegend, live: false },
      cursor: { drag: { x: false, y: false }, points: { size: 5 } },
      padding: [8, 0, 0, 0],
      scales: {
        x: {
          time: xType === 'time',
          ...(xType === 'cat' ? { distr: 3 as const, range: categoryScale(xs.length) } : {})
        }
      },
      axes: [
        {
          stroke: themeColor.text,
          ticks: { stroke: themeColor.grid, width: 1 },
          grid: { stroke: themeColor.grid, width: 1 },
          font: '11px system-ui, sans-serif',
          space: 56,
          values:
            xType === 'cat'
              ? categoryValues(xLabels)
              : (_u, ticks) =>
                  ticks.map((v) => {
                    const d = new Date(v * 1000);
                    return `${String(d.getDate()).padStart(2, '0')}-${String(
                      d.getMonth() + 1
                    ).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}h`;
                  })
        },
        {
          stroke: themeColor.text,
          ticks: { stroke: themeColor.grid, width: 1 },
          grid: { stroke: themeColor.grid, width: 1 },
          font: '11px system-ui, sans-serif',
          size: 62,
          values: (_u, ticks) => ticks.map((v) => yFormat(v))
        }
      ],
      series: plotSeries
    };

    // Abcisses numériques : catégoriel = index ; temporel = epoch (s).
    const x = xs.map((v, i) => (xType === 'time' ? parsePeriod(String(v)) : i));

    const aligned = [x, ...data.slice(1)] as unknown as uPlot.AlignedData;
    plot = new uPlot(opts, aligned, el);
  }

  $effect(() => {
    const el = host;
    if (!el) return;
    // `rebuildKey` : on recrée à la mise/retire du thème ou au changement de série.
    void rebuildKey;
    plot?.destroy();
    plot = null;
    build(el);

    observer?.disconnect();
    observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? 0;
      if (plot && width > 0 && Math.round(width) !== Math.round(plot.width)) {
        plot.setSize({ width: Math.round(width), height });
      }
    });
    observer.observe(el);

    return () => {
      observer?.disconnect();
      observer = null;
      plot?.destroy();
      plot = null;
    };
  });

  // Mise à jour des données seules (SSE / refresh) : aucun clignotement.
  $effect(() => {
    const next = data;
    if (!plot) return;
    const xs = next[0] ?? [];
    const x = xs.map((_, i) => (xType === 'time' ? parsePeriod(String(xs[i])) : i));
    plot.setData([x, ...next.slice(1)] as unknown as uPlot.AlignedData, false);
  });

  const isEmpty = $derived((data[0]?.length ?? 0) === 0);
</script>

<div class="chart" bind:this={host} style="--chart-h: {height}px">
  {#if isEmpty}
    <div class="chart-empty">—</div>
  {/if}
</div>

<style>
  .chart {
    position: relative;
    width: 100%;
    min-height: var(--chart-h);
  }

  .chart :global(.u-legend) {
    font-family: var(--font-sans);
    font-size: var(--fs-sm);
  }

  .chart :global(.u-legend .u-series) {
    color: var(--text-dim);
  }

  .chart-empty {
    display: grid;
    place-items: center;
    min-height: var(--chart-h);
    color: var(--text-faint);
    font-size: var(--fs-lg);
  }
</style>
