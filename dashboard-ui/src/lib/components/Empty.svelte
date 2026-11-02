<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    kind = 'empty',
    title,
    hint,
    action
  }: {
    kind?: 'empty' | 'error' | 'offline';
    title: string;
    hint?: string;
    action?: Snippet;
  } = $props();
</script>

<div class="state state--{kind}">
  <div class="state-icon" aria-hidden="true">
    {#if kind === 'error'}⚠{:else if kind === 'offline'}⛓{:else}∅{/if}
  </div>
  <p class="state-title">{title}</p>
  {#if hint}<p class="state-hint">{hint}</p>{/if}
  {#if action}<div class="state-action">{@render action()}</div>{/if}
</div>

<style>
  .state {
    display: grid;
    justify-items: center;
    gap: var(--sp-2);
    padding: var(--sp-8) var(--sp-4);
    text-align: center;
  }

  .state-icon {
    font-size: 26px;
    color: var(--text-faint);
    line-height: 1;
  }

  .state--error .state-icon {
    color: var(--danger);
  }

  .state-title {
    margin: 0;
    color: var(--text);
    font-size: var(--fs-base);
    font-weight: 600;
  }

  .state-hint {
    margin: 0;
    color: var(--text-dim);
    font-size: var(--fs-md);
    max-width: 60ch;
  }

  .state-action {
    margin-top: var(--sp-2);
  }
</style>
