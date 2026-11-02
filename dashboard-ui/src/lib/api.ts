/**
 * Couche HTTP du dashboard.
 *
 * Reprend exactement le contrat de l'ancien `app.js` :
 *  - jeton stocké dans `localStorage.dashboard_token`, envoyé en
 *    `X-Dashboard-Token` (jamais dans l'URL) ;
 *  - sur 401 avec un jeton déjà présent : on redemande le jeton puis on
 *    rejoue UNE fois la requête ;
 *  - toute réponse non-2xx lève `ApiError` portant le statut + le corps
 *    (tronqué) pour que chaque vue puisse afficher un message exploitable.
 *
 * Ajouts qualité : timeout, `AbortSignal` (annulation des requêtes
 * obsolètes), et construction de query-string centralisée.
 */

const TOKEN_KEY = 'dashboard_token';

/** Délai au-delà duquel une requête est considérée comme perdue (ms). */
const DEFAULT_TIMEOUT_MS = 20_000;

export class ApiError extends Error {
  readonly status: number;
  readonly body: string;

  constructor(status: number, body: string) {
    super(`HTTP ${status}: ${body.slice(0, 200)}`);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }

  /** Message court et lisible pour l'affichage utilisateur. */
  get short(): string {
    return this.body ? `HTTP ${this.status}` : `HTTP ${this.status}`;
  }
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token.trim());
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // stockage indisponible : on continue sans jeton
  }
}

/** Construit `?a=1&b=2` en ignorant les valeurs `undefined`/`null`/vide. */
export function qs(params: Record<string, string | number | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const str = search.toString();
  return str ? `?${str}` : '';
}

/** Fetch avec en-tête d'authentification, sans interprétation du corps. */
async function fetchWithToken(url: string, options: RequestInit = {}): Promise<Response> {
  const headers = new Headers(options.headers || {});
  const token = getToken();
  if (token) headers.set('X-Dashboard-Token', token);
  return fetch(url, { ...options, headers });
}

function promptToken(): string | null {
  const token = prompt('Jeton du tableau de bord :');
  if (token) {
    setToken(token);
    return token;
  }
  return null;
}

/**
 * Exécute une requête et renvoie le corps JSON décodé.
 * `signal` permet d'annuler (composant démonté, réponse obsolète).
 */
export async function request<T>(url: string, options: RequestInit = {}): Promise<T> {
  const timeoutMs = DEFAULT_TIMEOUT_MS;
  const timeoutCtrl = new AbortController();
  const timer = setTimeout(() => timeoutCtrl.abort(), timeoutMs);

  // L'annulation appelant et le timeout interne fusionnent en un seul signal.
  const signal = options.signal
    ? anySignal([options.signal as AbortSignal, timeoutCtrl.signal])
    : timeoutCtrl.signal;

  try {
    let resp = await fetchWithToken(url, { ...options, signal });

    if (resp.status === 401 && getToken()) {
      if (promptToken()) {
        resp = await fetchWithToken(url, { ...options, signal });
      }
    }

    if (!resp.ok) {
      const text = await resp.text().catch(() => '');
      throw new ApiError(resp.status, text);
    }

    // 204 / corps vide : on renvoie `undefined` typé.
    const contentType = resp.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
      const text = await resp.text();
      return (text ? JSON.parse(text) : undefined) as T;
    }
    return (await resp.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

/** Fusion de deux `AbortSignal` (passage par un contrôleur en `once`). */
function anySignal(signals: AbortSignal[]): AbortSignal {
  const ctrl = new AbortController();
  const onAbort = () => ctrl.abort();
  for (const s of signals) {
    if (s.aborted) {
      ctrl.abort();
      break;
    }
    s.addEventListener('abort', onAbort, { once: true });
  }
  return ctrl.signal;
}

export function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { signal });
}

export function apiPost<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal
): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal
  });
}

export function apiPut<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal
): Promise<T> {
  return request<T>(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal
  });
}

export function apiDelete<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { method: 'DELETE', signal });
}
