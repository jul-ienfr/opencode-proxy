<script lang="ts">
  /**
   * Barre de sous-onglets accessible (ARIA tablist) pour les pages denses.
   *
   * Le parent garde la source de vérité (`active`) et la navigation ; ce
   * composant ne fait que rendre les boutons et gérer le clavier.
   */
  import { tick } from 'svelte';

  interface SubTab {
    id: string;
    label: string;
    testid?: string;
  }

  let {
    tabs,
    active,
    ariaLabel,
    onSelect
  }: {
    tabs: readonly SubTab[];
    active: string;
    ariaLabel: string;
    onSelect: (id: string) => void;
  } = $props();

  function focusTab(id: string): void {
    void tick().then(() => {
      document
        .querySelector<HTMLButtonElement>(`[data-subtab="${CSS.escape(id)}"]`)
        ?.focus();
    });
  }

  function onKeydown(event: KeyboardEvent, index: number): void {
    let next: number | null = null;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = tabs.length - 1;
    if (next === null) return;
    event.preventDefault();
    const id = tabs[next].id;
    onSelect(id);
    focusTab(id);
  }
</script>

<div class="subtabs" role="tablist" aria-label={ariaLabel}>
  {#each tabs as tab, i (tab.id)}
    <button
      type="button"
      role="tab"
      class="subtab"
      class:subtab--active={active === tab.id}
      data-subtab={tab.id}
      data-testid={tab.testid ?? `tab-${tab.id}`}
      aria-selected={active === tab.id}
      tabindex={active === tab.id ? 0 : -1}
      onclick={() => onSelect(tab.id)}
      onkeydown={(event) => onKeydown(event, i)}
    >
      {tab.label}
    </button>
  {/each}
</div>

<style>
  .subtabs {
    display: flex;
    align-items: stretch;
    gap: 2px;
    overflow-x: auto;
    border-bottom: 1px solid var(--border);
    margin: 0 0 var(--sp-4);
    padding-bottom: 0;
    scrollbar-width: thin;
    -webkit-overflow-scrolling: touch;
  }

  .subtab {
    flex: 0 0 auto;
    appearance: none;
    border: 0;
    border-bottom: 2px solid transparent;
    background: transparent;
    color: var(--text-dim);
    font: inherit;
    font-size: var(--fs-sm);
    padding: 8px 12px;
    cursor: pointer;
    white-space: nowrap;
    border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  }

  .subtab:hover {
    color: var(--text);
    background: var(--surface-2);
  }

  .subtab:focus-visible {
    outline: 2px solid var(--accent, var(--text));
    outline-offset: -2px;
  }

  .subtab--active {
    color: var(--text);
    border-bottom-color: var(--accent, var(--text));
    font-weight: 600;
  }

  @media (max-width: 640px) {
    .subtabs {
      margin-left: calc(-1 * var(--sp-2));
      margin-right: calc(-1 * var(--sp-2));
      padding-left: var(--sp-2);
      padding-right: var(--sp-2);
    }
  }
</style>
