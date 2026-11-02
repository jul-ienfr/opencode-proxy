/**
 * File de messages toast (runes Svelte 5).
 *
 * Remplace les multiples `setTimeout(() => statusEl.textContent = '', 3000)`
 * éparpillés dans l'ancien `app.js` : un seul système, un seul timer, des
 * kinds cohérents (succès / erreur / info) et une file bornée.
 */

export type ToastKind = 'success' | 'error' | 'info' | 'warning';

export interface Toast {
  readonly id: number;
  readonly message: string;
  readonly kind: ToastKind;
}

/** Nombre maximal de toasts affichés à la fois (les plus vieux sont retirés). */
const MAX_VISIBLE = 4;
const DURATION_MS = 5000;

export const toasts = $state<{ items: Toast[] }>({ items: [] });

let nextId = 1;
const timers = new Map<number, ReturnType<typeof setTimeout>>();

export function showToast(message: string, kind: ToastKind = 'info'): void {
  if (!message) return;
  const id = nextId++;
  toasts.items = [...toasts.items, { id, message, kind }].slice(-MAX_VISIBLE);

  timers.set(
    id,
    setTimeout(() => dismissToast(id), DURATION_MS)
  );
}

export function dismissToast(id: number): void {
  toasts.items = toasts.items.filter((x) => x.id !== id);
  const timer = timers.get(id);
  if (timer) {
    clearTimeout(timer);
    timers.delete(id);
  }
}

/** Raccourcis sémantiques. */
export const toastSuccess = (m: string): void => showToast(m, 'success');
export const toastError = (m: string): void => showToast(m, 'error');
