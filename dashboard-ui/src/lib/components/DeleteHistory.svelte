<script lang="ts">
  /**
   * Suppression de l'historique des requêtes (tout, ou avant une date),
   * présente dans le pied de page comme dans l'ancienne interface.
   */

  import { apiDelete } from '$lib/api';
  import { t } from '$lib/i18n.svelte';
  import { invalidateTag } from '$lib/query.svelte';
  import { showToast } from '$lib/toast.svelte';

  let menuOpen = $state(false);
  let modalOpen = $state(false);
  let date = $state('');
  let busy = $state(false);

  function today(): string {
    return new Date().toISOString().slice(0, 10);
  }

  /** Referme le menu dès qu'on clique ailleurs. */
  $effect(() => {
    if (!menuOpen) return;
    const close = (e: MouseEvent) => {
      if (!(e.currentTarget as HTMLElement | null)?.contains(e.target as Node)) menuOpen = false;
    };
    window.addEventListener('click', close, true);
    return () => window.removeEventListener('click', close, true);
  });

  function openModal(): void {
    menuOpen = false;
    date = today();
    modalOpen = true;
  }

  function onKeydown(e: KeyboardEvent): void {
    if (e.key === 'Escape') {
      menuOpen = false;
      modalOpen = false;
    }
  }

  async function remove(before: string | null): Promise<void> {
    if (busy) return;
    busy = true;
    try {
      await apiDelete(`/api/history?${before ? `before=${encodeURIComponent(before)}` : 'all=true'}`);
      modalOpen = false;
      // Rafraîchit l'onglet Requêtes s'il est monté (le reste est inert).
      invalidateTag('history');
      showToast(t('delete.done'), 'success');
    } catch (e) {
      showToast(e instanceof Error ? e.message : String(e), 'error');
    } finally {
      busy = false;
    }
  }
</script>

<svelte:window onkeydown={onKeydown} />

<div class="delete">
  <div class="menu-anchor">
    <button
      class="btn btn--sm btn--danger"
      aria-haspopup="menu"
      aria-expanded={menuOpen}
      onclick={() => (menuOpen = !menuOpen)}
    >
      {t('delete.history')}
    </button>

    {#if menuOpen}
      <div class="menu" role="menu">
        <button class="menu-item" role="menuitem" onclick={() => remove(null)}>
          {t('delete.all')}
        </button>
        <button class="menu-item" role="menuitem" onclick={openModal}>
          {t('delete.before_date')}
        </button>
      </div>
    {/if}
  </div>
</div>

{#if modalOpen}
  <div class="backdrop" role="presentation" onclick={() => (modalOpen = false)} onkeydown={onKeydown}>
    <div
      class="modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="delete-title"
      tabindex="-1"
      onclick={(e) => e.stopPropagation()}
      onkeydown={onKeydown}
    >
      <h3 id="delete-title">{t('delete.modal_title')}</h3>
      <input class="input" type="date" bind:value={date} max={today()} />
      <div class="modal-buttons">
        <button class="btn btn--danger" disabled={busy || !date} onclick={() => remove(date)}>
          {t('delete.all')}
        </button>
        <button class="btn" onclick={() => (modalOpen = false)}>{t('cancel')}</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .delete {
    display: inline-flex;
  }

  .menu-anchor {
    position: relative;
  }

  .menu {
    position: absolute;
    right: 0;
    bottom: calc(100% + 6px);
    display: grid;
    min-width: 200px;
    padding: var(--sp-1);
    background: var(--surface-2);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-lg);
    z-index: var(--z-dropdown, 50);
  }

  .menu-item {
    background: none;
    border: 0;
    border-radius: var(--radius-sm);
    padding: var(--sp-2) var(--sp-3);
    text-align: left;
    font-size: var(--fs-sm);
    color: var(--text);
    cursor: pointer;
  }

  .menu-item:hover {
    background: var(--surface-3);
  }

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
    width: min(420px, 100%);
    padding: var(--sp-5);
    display: grid;
    gap: var(--sp-4);
    box-shadow: var(--shadow-lg);
  }

  .modal h3 {
    margin: 0;
    font-size: var(--fs-lg);
  }

  .modal-buttons {
    display: flex;
    justify-content: flex-end;
    gap: var(--sp-2);
  }
</style>
