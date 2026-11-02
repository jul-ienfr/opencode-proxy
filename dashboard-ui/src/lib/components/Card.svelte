<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    title,
    hint,
    actions,
    children,
    compact = false
  }: {
    title?: string;
    hint?: string;
    actions?: Snippet;
    children?: Snippet;
    compact?: boolean;
  } = $props();
</script>

<section class="card" class:card--compact={compact}>
  {#if title || actions}
    <header class="card-head">
      <div class="card-title">
        {#if title}<h2>{title}</h2>{/if}
        {#if hint}<p class="card-hint">{hint}</p>{/if}
      </div>
      {#if actions}
        <div class="card-actions">{@render actions()}</div>
      {/if}
    </header>
  {/if}

  {#if children}
    <div class="card-body">
      {@render children()}
    </div>
  {/if}
</section>

<style>
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: var(--sp-5);
    box-shadow: var(--shadow-sm);
  }

  .card--compact {
    padding: var(--sp-3) var(--sp-4);
  }

  .card-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--sp-4);
    margin-bottom: var(--sp-4);
  }

  .card-head:empty {
    margin-bottom: 0;
  }

  .card-title h2 {
    font-size: var(--fs-lg);
    font-weight: 600;
  }

  .card-hint {
    margin: var(--sp-1) 0 0;
    font-size: var(--fs-sm);
    color: var(--text-dim);
    max-width: 78ch;
  }

  .card-actions {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    flex: none;
  }

  .card-body {
    /* contenus au trapu : la mise en page reste à la vue */
    min-width: 0;
  }
</style>
