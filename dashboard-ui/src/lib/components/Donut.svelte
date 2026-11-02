<script lang="ts">
  import { CHART_COLORS, pct, type DonutSegment } from '$lib/chart';
  import { t } from '$lib/i18n.svelte';

  let {
    segments = [],
    size = 168,
    thickness = 22,
    maxLegend = 7,
    centerValue = '',
    centerLabel = ''
  }: {
    segments: DonutSegment[];
    size?: number;
    thickness?: number;
    /** Nombre max de segments listés ; le reliquat est agrégé en « Autres ». */
    maxLegend?: number;
    centerValue?: string;
    centerLabel?: string;
  } = $props();

  const total = $derived(segments.reduce((sum, s) => sum + Math.max(0, s.value), 0));

  const radius = $derived((size - thickness) / 2);
  const circumference = $derived(2 * Math.PI * radius);

  /**
   * Segments effectivement dessinés : valeurs strictement positives, triées
   * décroissantes, reliquat agrégé.
   *
   * Sans cette étape, un donut « % par modèle » listerait la quarantaine de
   * modèles connus dont la quasi-totalité vaut 0 % : une légende illisible
   * qui faisait exploser la hauteur de la carte.
   */
  const display = $derived.by<DonutSegment[]>(() => {
    const nonzero = segments.filter((s) => s.value > 0).sort((a, b) => b.value - a.value);
    if (nonzero.length <= maxLegend) return nonzero;
    const head = nonzero.slice(0, maxLegend - 1);
    const rest = nonzero.slice(maxLegend - 1);
    const restValue = rest.reduce((a, s) => a + s.value, 0);
    return [...head, { label: t('chart.others'), value: restValue }];
  });

  /** Couleurs : celles fournies, sinon la palette classée par poids. */
  const styled = $derived(
    display.map((s, i) => ({ ...s, color: s.color ?? CHART_COLORS[i % CHART_COLORS.length] }))
  );

  /**
   * Les arcs se construisent dans le sens horaire : chaque segment prend une
   * longueur proportionnelle dans la circonférence, le décalage suivant est
   * négatif (on avance à gauche du point de départ).
   */
  const arcs = $derived.by(() => {
    const sum = styled.reduce((a, s) => a + s.value, 0) || 1;
    let cursor = 0;
    return styled.map((s) => {
      const length = (s.value / sum) * circumference;
      const arc = { ...s, length, offset: -cursor };
      cursor += length;
      return arc;
    });
  });

  const legend = $derived(styled.map((s) => ({ ...s, share: pct(s.value, total) })));
</script>

<div class="donut" style="--size: {size}px">
  <svg viewBox="0 0 {size} {size}" width={size} height={size} role="img" aria-hidden="true">
    <g transform="rotate(-90 {size / 2} {size / 2})">
      <circle
        class="donut-track"
        cx={size / 2}
        cy={size / 2}
        r={radius}
        stroke-width={thickness}
      />
      {#each arcs as arc (arc.label)}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={arc.color}
          stroke-width={thickness}
          stroke-linecap="butt"
          stroke-dasharray="{arc.length} {circumference - arc.length}"
          stroke-dashoffset={arc.offset}
        />
      {/each}
    </g>
    <text class="donut-center" x="50%" y="50%" dy="0.02em" text-anchor="middle">
      {centerValue}
    </text>
    {#if centerLabel}
      <text class="donut-sub" x="50%" y="50%" dy="1.75em" text-anchor="middle">
        {centerLabel}
      </text>
    {/if}
  </svg>

  {#if legend.length}
    <ul class="donut-legend">
      {#each legend as item (item.label)}
        <li class="donut-item">
          <span class="donut-swatch" style="--sw: {item.color}"></span>
          <span class="donut-name">{item.label}</span>
          <span class="donut-num">{item.share}</span>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .donut {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--sp-5);
    flex-wrap: wrap;
    min-height: var(--size);
  }

  .donut-track {
    fill: none;
    stroke: var(--surface-3);
  }

  .donut-center {
    fill: var(--text);
    font-family: var(--font-sans);
    font-size: 19px;
    font-weight: 600;
  }

  .donut-sub {
    fill: var(--text-faint);
    font-family: var(--font-sans);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .donut-legend {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--sp-2);
    max-width: 34ch;
  }

  .donut-item {
    display: grid;
    grid-template-columns: auto 1fr auto;
    align-items: center;
    gap: var(--sp-2);
    font-size: var(--fs-sm);
  }

  .donut-swatch {
    width: 10px;
    height: 10px;
    border-radius: 3px;
    background: var(--sw);
  }

  .donut-name {
    color: var(--text-dim);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .donut-num {
    color: var(--text);
    font-variant-numeric: tabular-nums;
    font-family: var(--font-mono);
    font-size: var(--fs-xs);
  }
</style>
