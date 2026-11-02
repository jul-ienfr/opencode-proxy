<script lang="ts">
  import { dismissToast, toasts, type ToastKind } from '$lib/toast.svelte';
  import { t } from '$lib/i18n.svelte';

  const ICON: Record<ToastKind, string> = {
    success: '✓',
    error: '✕',
    warning: '⚠',
    info: 'ℹ'
  };
</script>

<div class="toasts" role="status" aria-live="polite">
  {#each toasts.items as toast (toast.id)}
    <div class="toast toast--{toast.kind}">
      <span class="toast-icon" aria-hidden="true">{ICON[toast.kind]}</span>
      <span class="toast-msg">{toast.message}</span>
      <button
        class="toast-close"
        onclick={() => dismissToast(toast.id)}
        aria-label={t('cancel')}>×</button
      >
    </div>
  {/each}
</div>

<style>
  .toasts {
    position: fixed;
    right: var(--sp-4);
    bottom: var(--sp-4);
    z-index: var(--z-toast);
    display: flex;
    flex-direction: column;
    gap: var(--sp-2);
    max-width: min(420px, calc(100vw - var(--sp-8)));
  }

  .toast {
    display: flex;
    align-items: flex-start;
    gap: var(--sp-2);
    padding: var(--sp-3);
    border-radius: var(--radius);
    border: 1px solid var(--border-strong);
    background: var(--surface-2);
    box-shadow: var(--shadow-md);
    font-size: var(--fs-md);
    animation: toast-in 0.18s ease-out;
  }

  .toast-icon {
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

  .toast--success .toast-icon {
    background: var(--success);
  }
  .toast--error .toast-icon {
    background: var(--danger);
  }
  .toast--warning .toast-icon {
    background: var(--warn);
  }

  .toast-msg {
    flex: 1;
    word-break: break-word;
    color: var(--text);
  }

  .toast-close {
    flex: none;
    border: none;
    background: none;
    color: var(--text-faint);
    font-size: var(--fs-lg);
    line-height: 1;
    padding: 0 var(--sp-1);
    cursor: pointer;
  }
  .toast-close:hover {
    color: var(--text);
  }

  @keyframes toast-in {
    from {
      opacity: 0;
      transform: translateY(6px);
    }
    to {
      opacity: 1;
      transform: none;
    }
  }
</style>
