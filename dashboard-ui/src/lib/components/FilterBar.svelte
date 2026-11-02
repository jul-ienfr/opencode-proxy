<script lang="ts">
  /**
   * Barre de filtre de période (Aujourd'hui / 7 j / 30 j / Personnalisé).
   *
   * Corrections apportées à l'ancien système :
   *  - la période vit DANS l'URL (`#stats?from=…&to=…`) : rafraîchir ou
   *    partager un lien garde exactement le même écran (le vieux code perdait
   *    son état à chaque reload) ;
   *  - la barre n'apparaît QUE sur les vues qui l'utilisent (Stats et
   *    Requêtes) — elle était posée au-dessus de tous les onglets, donc
   *    polluait Quotas/Configuration/Trafic sans effet ;
   *  - changer de période remet la pagination à la page 1.
   */

  import { route, setParams } from '$lib/router.svelte';
  import { t } from '$lib/i18n.svelte';
  import { daysAgo, toDateInput } from '$lib/format';

  type Range = 'today' | '7d' | '30d' | 'custom';

  const PRESETS: { id: Exclude<Range, 'custom'>; label: string }[] = [
    { id: 'today', label: 'filter.today' },
    { id: '7d', label: 'filter.7d' },
    { id: '30d', label: 'filter.30d' }
  ];

  const today = $derived(toDateInput(new Date()));
  const from = $derived(route.current.params.from || today);
  const to = $derived(route.current.params.to || today);

  /** Quel bouton est actif : déduit des dates, jamais stocké en double. */
  const active = $derived.by<Range>(() => {
    if (from === today && to === today) return 'today';
    if (to === today && from === daysAgo(7)) return '7d';
    if (to === today && from === daysAgo(30)) return '30d';
    return 'custom';
  });

  let draftFrom = $state('');
  let draftTo = $state('');
  /** Les champs personnalisés restent ouverts une fois révélés (comme l'ancien
   *  bouton « Personnalisé »), jusqu'à ce qu'un préréglage les referme. */
  let showCustom = $state(false);

  function openCustom(): void {
    showCustom = true;
    draftFrom = from;
    draftTo = to;
  }

  // Si l'URL impose déjà une période personnalisée, les champs sont visibles.
  $effect(() => {
    if (active === 'custom') showCustom = true;
  });

  function applyPreset(id: Exclude<Range, 'custom'>): void {
    const day = toDateInput(new Date());
    const start = id === 'today' ? day : daysAgo(id === '7d' ? 7 : 30);
    showCustom = false;
    // `page: null` supprime le paramètre : retour à la première page.
    setParams({ from: start, to: day, page: null });
  }

  function applyCustom(): void {
    setParams({ from: draftFrom || null, to: draftTo || null, page: null });
  }
</script>

<div class="filter-bar">
  <div class="filter-presets" role="group" aria-label={t('filter.apply')}>
    {#each PRESETS as preset (preset.id)}
      <button
        class="btn btn--sm preset"
        class:preset--active={active === preset.id}
        onclick={() => applyPreset(preset.id)}
      >
        {t(preset.label)}
      </button>
    {/each}
    <button
      class="btn btn--sm preset"
      class:preset--active={active === 'custom'}
      onclick={openCustom}
    >
      {t('filter.custom')}
    </button>
  </div>

  {#if showCustom}
    <div class="filter-range">
      <input
        type="date"
        class="input input--inline date"
        aria-label={t('filter.custom')}
        bind:value={draftFrom}
      />
      <span class="filter-sep">{t('filter.to')}</span>
      <input
        type="date"
        class="input input--inline date"
        aria-label={t('filter.to')}
        bind:value={draftTo}
      />
      <button class="btn btn--sm btn--primary" onclick={applyCustom}>
        {t('filter.apply')}
      </button>
    </div>
  {/if}
</div>

<style>
  .filter-bar {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    flex-wrap: wrap;
    margin-bottom: var(--sp-4);
  }

  .filter-presets {
    display: flex;
    gap: var(--sp-1);
    flex-wrap: wrap;
  }

  .preset--active {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--accent-contrast);
  }

  .filter-range {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    flex-wrap: wrap;
  }

  .date {
    font-family: var(--font-mono);
    font-size: var(--fs-sm);
  }

  .filter-sep {
    color: var(--text-faint);
    font-size: var(--fs-sm);
  }
</style>
