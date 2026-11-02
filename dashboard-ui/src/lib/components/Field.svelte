<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    label,
    hint,
    inline = false,
    children
  }: {
    label: string;
    hint?: string;
    /** Variant inline : label à gauche du contrôle (barre d'outils). */
    inline?: boolean;
    children?: Snippet;
  } = $props();
</script>

<div class="field" class:field--inline={inline}>
  <span class="field-label">{label}</span>
  <div class="field-control">
    {#if children}{@render children()}{/if}
    {#if hint}<span class="field-hint">{hint}</span>{/if}
  </div>
</div>

<style>
  .field {
    display: grid;
    gap: var(--sp-2);
    min-width: 0;
  }

  .field--inline {
    grid-template-columns: auto 1fr;
    align-items: center;
    gap: var(--sp-3);
  }

  .field-label {
    font-size: var(--fs-sm);
    font-weight: 500;
    color: var(--text-dim);
  }

  .field-control {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    min-width: 0;
  }

  .field-hint {
    font-size: var(--fs-sm);
    color: var(--text-faint);
  }
</style>
