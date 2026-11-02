/**
 * Aides graphiques : couleurs, échelles et conversion des périodes serveur.
 *
 * Tout passe par les tokens CSS de `app.css` : un graphique suive le thème
 * clair/sombre sans réécrire ses couleurs en dur (l'ancien code recalculait
 * `isDark` et du hex à chaque rendu).
 */

import type uPlot from 'uplot';

/** Palette historique (Chart.js) reprise telle quelle pour la continuité. */
export const CHART_COLORS = [
  '#4fc3f7',
  '#ff8a65',
  '#81c784',
  '#ba68c8',
  '#ffd54f',
  '#4db6ac',
  '#f06292',
  '#90a4ae',
  '#64b5f6',
  '#a5d6a7'
];

export interface ChartTheme {
  text: string;
  grid: string;
  tick: string;
}

/** Description d'une série telle que l'attend `UPlot.svelte`. */
export interface ChartSeries {
  label: string;
  stroke?: string;
  fill?: string;
  width?: number;
  /** Force le rendu en barres pour CETTE série (mélange barre+ligne). */
  bars?: boolean;
}

/** Segment d'un graphique circulaire (`Donut.svelte`). */
export interface DonutSegment {
  label: string;
  value: number;
  color?: string;
}

/** Lit les couleurs courantes depuis les variables de thème. */
export function chartTheme(): ChartTheme {
  const cs = getComputedStyle(document.documentElement);
  const read = (name: string, fallback: string): string =>
    cs.getPropertyValue(name).trim() || fallback;
  return {
    text: read('--text-dim', '#94a3b8'),
    grid: read('--border', '#1e293b'),
    tick: read('--text-faint', '#64748b')
  };
}

/**
 * Convertit une période serveur en secondes epoch (heure locale).
 *
 * Formats émis par l'API : `"2026-06-05 14:00"` (granularité heure) et
 * `"2026-06-05"` (granularité jour). Les formes sans fuseau sont interprétées
 * en heure locale par `Date.parse`, ce qui correspond à l'affichage voulu.
 */
export function parsePeriod(period: string): number {
  const iso =
    period.length >= 16
      ? `${period.slice(0, 10)}T${period.slice(11, 16)}`
      : `${period.slice(0, 10)}T00:00`;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? 0 : Math.floor(ms / 1000);
}

/** Libellé d'abscisse pour une période : `14:00` (heure) ou `06-05` (jour). */
export function periodLabel(period: string): string {
  if (period.length >= 16) return period.slice(11, 16);
  if (period.length >= 10) return period.slice(5, 10);
  return period;
}

/**
 * Bornes de l'échelle catégorielle (index `0..n-1`).
 *
 * Retourne un **tuple** : c'est la forme qu'uPlot accepte pour `scale.range`
 * (un objet `{min, max}` est réservé à d'autres types d'échelle).
 */
export function categoryScale(count: number): [number, number] {
  return [0, Math.max(1, count - 1)];
}

/** Transforme des ticks d'index en libellés (axe catégoriel). */
export function categoryValues(labels: string[]): (u: uPlot, splits: number[]) => string[] {
  return (_u, splits) => splits.map((s) => labels[Math.round(s)] ?? '');
}

/** Ligne de base de configuration commune à tous les graphiques uPlot. */
export function baseOptions(height: number): Partial<uPlot.Options> {
  const theme = chartTheme();
  return {
    width: 300,
    height,
    legend: { show: true },
    cursor: { show: true, points: { size: 5 } },
    scales: { x: { time: false } },
    axes: [
      {
        stroke: theme.text,
        ticks: { stroke: theme.grid, width: 1 },
        grid: { stroke: theme.grid, width: 1 },
        font: '11px system-ui, sans-serif',
        space: 40
      },
      {
        stroke: theme.text,
        ticks: { stroke: theme.grid, width: 1 },
        grid: { stroke: theme.grid, width: 1 },
        font: '11px system-ui, sans-serif',
        size: 60
      }
    ],
    series: [
      {},
      {
        label: '',
        stroke: CHART_COLORS[0],
        width: 2,
        points: { show: false }
      }
    ]
  };
}

/** Options d'un graphique de type camembert → remplacé par `Donut.svelte`. */
export function pct(value: number, total: number): string {
  if (!total) return '0%';
  return `${((value / total) * 100).toFixed(1)}%`;
}
