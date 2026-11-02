<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    kind = 'info',
    title = '',
    children
  }: {
    kind?: 'info' | 'success' | 'warning' | 'danger';
    title?: string;
    children?: Snippet;
  } = $props();

  const ICON: Record<string, string> = {
    info: 'ℹ',
    success: '✓',
    warning: '⚠',
    danger: '✕'
  };
</script>

<div class="banner banner--{kind}" role={kind === 'danger' ? 'alert' : 'status'}>
  <span class="banner-icon" aria-hidden="true">{ICON[kind]}</span>
  <div class="banner-body">
    {#if title}<strong>{title}</strong>{/if}
    {#if children}
      <div class="banner-text">{@render children()}</div>
    {/if}
  </div>
</div>

<style>
  .banner {
    display: flex;
    gap: var(--sp-3);
    align-items: flex-start;
    padding: var(--sp-3);
    border-radius: var(--radius);
    border: 1px solid var(--border-strong);
    font-size: var(--fs-md);
    line-height: 1.45;
  }

  .banner-icon {
    flex: none;
    width: 18px;
    height: 18px;
    display: grid;
    place-items: center;
    border-radius: var(--radius-full);
    font-size: var(--fs-xs);
    color: var(--accent-contrast);
    background: var(--info);
  }

  .banner--success {
    background: var(--success-bg);
    border-color: var(--success);
  }
  .banner--success .banner-icon {
    background: var(--success);
  }

  .banner--warning {
    background: var(--warn-bg);
    border-color: var(--warn);
  }
  .banner--warning .banner-icon {
    background: var(--warn);
    color: #1a1200;
  }

  .banner--danger {
    background: var(--danger-bg);
    border-color: var(--danger);
  }
  .banner--danger .banner-icon {
    background: var(--danger);
  }

  .banner--info {
    background: var(--info-bg);
    border-color: var(--info);
  }

  .banner-body {
    display: grid;
    gap: 2px;
    min-width: 0;
    color: var(--text);
  }

  .banner-text {
    color: var(--text-dim);
  }
</style>
