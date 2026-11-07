<script lang="ts">
  /**
   * Recherche locale de réglages : ouvre le sous-onglet correspondant.
   * Aucune valeur n'est modifiée ; l'index est statique (`$lib/search`).
   */
  import { t } from '$lib/i18n.svelte';
  import { searchSettings, type SearchGroup } from '$lib/search';

  let {
    group,
    onSelect
  }: {
    group: SearchGroup;
    onSelect: (tab: string, label: string) => void;
  } = $props();

  let query = $state('');
  let open = $state(false);
  let active = $state(0);

  const results = $derived(searchSettings(query, group));

  function choose(index: number): void {
    const entry = results[index];
    if (!entry) return;
    onSelect(entry.tab, entry.label);
    query = '';
    open = false;
    active = 0;
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      open = false;
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      active = results.length ? (active + 1) % results.length : 0;
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      active = results.length ? (active - 1 + results.length) % results.length : 0;
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      choose(active);
    }
  }

  function onFocusOut(event: FocusEvent): void {
    const next = event.relatedTarget;
    if (!(next instanceof Node) || !event.currentTarget) {
      open = false;
      return;
    }
    if (!(event.currentTarget as HTMLElement).contains(next)) open = false;
  }
</script>

<div class="settings-search" onfocusout={onFocusOut}>
  <input
    class="input search-input"
    type="search"
    aria-label={t('search.placeholder')}
    placeholder={t('search.placeholder')}
    data-testid="settings-search"
    bind:value={query}
    onfocus={() => (open = true)}
    oninput={() => {
      open = true;
      active = 0;
    }}
    onkeydown={onKeydown}
  />

  {#if open && query.trim()}
    <div class="results" role="listbox" aria-label={t('search.placeholder')}>
      {#if results.length === 0}
        <div class="empty">{t('search.no_results')}</div>
      {:else}
        {#each results as entry, i (entry.group + entry.tab)}
          <button
            type="button"
            class="result"
            class:result--active={i === active}
            role="option"
            aria-selected={i === active}
            data-search-result={entry.tab}
            onmousedown={(event) => event.preventDefault()}
            onclick={() => choose(i)}
            onmouseenter={() => (active = i)}
          >
            <span>{entry.label}</span>
            <span class="group">
              {entry.group === 'free-models' ? t('nav.free_models') : t('nav.config')}
            </span>
          </button>
        {/each}
      {/if}
    </div>
  {/if}
</div>

<style>
  .settings-search {
    position: relative;
    width: min(360px, 100%);
    margin: 0 0 var(--sp-3);
  }

  .search-input {
    width: 100%;
  }

  .results {
    position: absolute;
    z-index: 20;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    background: var(--surface-1, var(--surface-2));
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    box-shadow: 0 8px 24px rgb(0 0 0 / 18%);
    overflow: hidden;
  }

  .result {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--sp-2);
    width: 100%;
    padding: 8px 10px;
    border: 0;
    background: transparent;
    color: var(--text);
    font: inherit;
    font-size: var(--fs-sm);
    text-align: left;
    cursor: pointer;
  }

  .result:hover,
  .result--active {
    background: var(--surface-2);
  }

  .result .group {
    color: var(--text-faint);
    font-size: var(--fs-xs);
    white-space: nowrap;
  }

  .empty {
    padding: 10px;
    color: var(--text-faint);
    font-size: var(--fs-sm);
  }
</style>
