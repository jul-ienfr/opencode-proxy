<script lang="ts">
  /**
   * Pagination de tableau (précédent / suivant + position).
   *
   * Affichée UNIQUEMENT quand le contenu dépasse une page : un tableau court
   * ne montre pas de commandes inutiles. Elle sert ici pour le tableau des
   * comptes, qui peut contenir des milliers d'adresses IP de sortie.
   */

  import { t } from '$lib/i18n.svelte';

  let {
    page = 1,
    total = 0,
    pageSize = 25,
    onchange
  }: {
    page?: number;
    total?: number;
    pageSize?: number;
    onchange?: (next: number) => void;
  } = $props();

  const pages = $derived(Math.max(1, Math.ceil(total / pageSize)));
  const visible = $derived(total > pageSize);
</script>

{#if visible}
  <div class="pager">
    <button
      class="btn btn--sm"
      disabled={page <= 1}
      onclick={() => onchange?.(page - 1)}
    >
      {t('page.prev')}
    </button>
    <span class="pager-info">{t('page.of', { c: page, t: pages })}</span>
    <button
      class="btn btn--sm"
      disabled={page >= pages}
      onclick={() => onchange?.(page + 1)}
    >
      {t('page.next')}
    </button>
  </div>
{/if}

<style>
  .pager {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--sp-3);
    padding-top: var(--sp-3);
  }

  .pager-info {
    font-size: var(--fs-sm);
    color: var(--text-dim);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
</style>
