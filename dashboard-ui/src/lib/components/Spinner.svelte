<script lang="ts">
  import { t } from '$lib/i18n.svelte';

  let { label = '' }: { label?: string } = $props();

  const text = $derived(label || t('stats.loading'));
</script>

<div class="spinner" role="status" aria-live="polite">
  <span class="spinner-ring" aria-hidden="true"></span>
  <span class="spinner-label">{text}</span>
</div>

<style>
  .spinner {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    padding: var(--sp-5) 0;
    color: var(--text-dim);
    font-size: var(--fs-md);
  }

  .spinner-ring {
    width: 16px;
    height: 16px;
    border-radius: 50%;
    border: 2px solid var(--border-strong);
    border-top-color: var(--accent);
    animation: spin 0.7s linear infinite;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
