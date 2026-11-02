/**
 * Coordinateur temps réel — un SEUL `EventSource`, un SEUL coordonnateur.
 *
 * Ce module remplace l'empilement de l'ancien `app.js` (SSE + timer de secours
 * + watchdog de silence + 3 timers de debounce + pollings géo/catalogue), qui
 * laissait plusieurs compteurs tourner en parallèle. Ici :
 *
 *  - `connected` ouvre, `ping` prouve la vitalité (le serveur envoie un
 *    `ping` toutes les 30 s d'inactivité) ;
 *  - un watchdog déclare le flux mort après 45 s de silence (le `onerror`
 *    ne se déclenche pas sur un socket figé) ;
 *  - reconnexion en back-off exponentiel 1 s → 30 s ;
 *  - le polling de secours (5 s) ne tourne QUE quand le SSE est absent ;
 *  - onglet masqué → fermeture ; onglet de retour → invalidation complète ;
 *  - chaque événement invalide par TAG. Les `Query` n'existent que pendant le
 *    montage d'une page : invalider « history » ne recharge donc que si la
 *    page Requêtes est réellement à l'écran. C'est ça, « ne recharger que
 *    l'utile ».
 */

import { invalidateAll, invalidateTag } from './query.svelte';
import { showToast } from './toast.svelte';

export type RealtimeStatus = 'connecting' | 'open' | 'closed';

/** Délai au-delà duquel on considère le flux mort (le serveur ping à 30 s). */
const SILENCE_MS = 45_000;
/** Fréquence du contrôle de silence. */
const WATCHDOG_MS = 15_000;
const MIN_RETRY_MS = 1_000;
const MAX_RETRY_MS = 30_000;
const FALLBACK_POLL_MS = 5_000;

/** Délais de debounce observés côté client historiquement. */
const DEBOUNCE_STATS_MS = 200;
const DEBOUNCE_CONFIG_MS = 300;
const DEBOUNCE_VPN_MS = 50;

type EventName =
  | 'ping'
  | 'connected'
  | 'stats_updated'
  | 'quotas_updated'
  | 'models_updated'
  | 'free_models_updated'
  | 'vpn_event'
  | 'geo_warning';

type Listener = (payload: unknown) => void;

export const realtime = $state<{
  status: RealtimeStatus;
  lastEventAt: number;
  reconnects: number;
}>({
  status: 'closed',
  lastEventAt: 0,
  reconnects: 0
});

const listeners = new Map<EventName, Set<Listener>>();
const debounceTimers = new Map<string, ReturnType<typeof setTimeout>>();

let source: EventSource | null = null;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let fallbackTimer: ReturnType<typeof setInterval> | null = null;
let watchdogTimer: ReturnType<typeof setInterval> | null = null;
let retryDelayMs = MIN_RETRY_MS;
let started = false;

/**
 * Abonne un composant à un événement SSE brut.
 * Retourne la fonction de désabonnement (à utiliser dans `$effect`).
 */
export function onEvent(name: EventName, listener: Listener): () => void {
  let set = listeners.get(name);
  if (!set) {
    set = new Set();
    listeners.set(name, set);
  }
  set.add(listener);
  return () => {
    set.delete(listener);
  };
}

/** Debounce nommé : les rafales (kill/restart de station) ne coûtent qu'un refresh. */
function debounce(key: string, ms: number, fn: () => void): void {
  const existing = debounceTimers.get(key);
  if (existing) clearTimeout(existing);
  debounceTimers.set(
    key,
    setTimeout(() => {
      debounceTimers.delete(key);
      fn();
    }, ms)
  );
}

function emit(name: EventName, payload: unknown): void {
  const set = listeners.get(name);
  if (set) for (const fn of set) fn(payload);
}

function parse(ev: MessageEvent): unknown {
  if (!ev.data) return null;
  try {
    return JSON.parse(ev.data);
  } catch {
    return ev.data;
  }
}

function markAlive(): void {
  realtime.lastEventAt = Date.now();
}

function stopFallback(): void {
  if (fallbackTimer) {
    clearInterval(fallbackTimer);
    fallbackTimer = null;
  }
}

function startFallback(): void {
  if (fallbackTimer) return;
  // Polling de secours : uniquement quand le flux temps réel est absent.
  fallbackTimer = setInterval(() => invalidateAll(), FALLBACK_POLL_MS);
}

function scheduleReconnect(): void {
  if (retryTimer) return;
  retryTimer = setTimeout(() => {
    retryTimer = null;
    connect();
  }, retryDelayMs);
  retryDelayMs = Math.min(retryDelayMs * 2, MAX_RETRY_MS);
}

function closeSource(): void {
  if (source) {
    source.close();
    source = null;
  }
}

function connect(): void {
  if (typeof EventSource === 'undefined') return;
  if (retryTimer) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
  closeSource();

  realtime.status = 'connecting';
  const es = new EventSource('/api/events');
  source = es;
  markAlive();

  es.addEventListener('open', () => {
    realtime.status = 'open';
  });

  // Vitalité. Le serveur émet un `ping` toutes les 30 s d'inactivité.
  es.addEventListener('ping', () => markAlive());

  es.addEventListener('connected', () => {
    markAlive();
    realtime.status = 'open';
    retryDelayMs = MIN_RETRY_MS;
    stopFallback();
    emit('connected', null);
    // On repart d'une base fraîche à la (re)connexion.
    invalidateAll();
  });

  es.addEventListener('stats_updated', () => {
    markAlive();
    // Rafales d'événements : un seul couple de refresh pour tout le lot.
    debounce('stats', DEBOUNCE_STATS_MS, () => {
      invalidateTag('stats');
      invalidateTag('history');
    });
    emit('stats_updated', null);
  });

  es.addEventListener('quotas_updated', () => {
    markAlive();
    invalidateTag('quotas');
    emit('quotas_updated', null);
  });

  es.addEventListener('models_updated', (ev) => {
    markAlive();
    const payload = parse(ev as MessageEvent);
    // Les polls quota publient `{count}` : pas de notification pour ça.
    if (payload && typeof payload === 'object') emit('models_updated', payload);
    invalidateTag('config');
    debounce('config', DEBOUNCE_CONFIG_MS, () => {
      invalidateTag('stats');
      invalidateTag('history');
    });
  });

  es.addEventListener('free_models_updated', (ev) => {
    markAlive();
    const payload = parse(ev as MessageEvent);
    if (payload && typeof payload === 'object') emit('free_models_updated', payload);
    invalidateTag('config');
    invalidateTag('free_models');
  });

  es.addEventListener('vpn_event', (ev) => {
    markAlive();
    emit('vpn_event', parse(ev as MessageEvent));
    // Rafales de kill/restart : un seul refresh pour toute la rafale.
    debounce('vpn', DEBOUNCE_VPN_MS, () => invalidateTag('vpn'));
  });

  es.addEventListener('geo_warning', (ev) => {
    markAlive();
    const payload = parse(ev as MessageEvent);
    if (payload && typeof payload === 'object' && 'message' in payload) {
      const message = (payload as { message: unknown }).message;
      if (typeof message === 'string' && message) showToast(message, 'warning');
    }
    invalidateTag('geo');
    emit('geo_warning', payload);
  });

  es.addEventListener('stats_refresh', () => {
    markAlive();
    invalidateTag('stats');
    invalidateTag('history');
  });

  es.onerror = () => {
    if (es !== source) return; // flux obsolète
    realtime.status = 'closed';
    closeSource();
    startFallback();
    scheduleReconnect();
  };
}

/** Démarre le coordinateur (appelé une fois par `main.ts`). */
export function startRealtime(): void {
  if (started) return;
  started = true;

  connect();

  if (!source) startFallback();

  // Watchdog : un flux ouvert mais figé ne déclenche jamais `onerror`.
  watchdogTimer = setInterval(() => {
    if (!source) return;
    if (Date.now() - realtime.lastEventAt > SILENCE_MS) {
      realtime.status = 'closed';
      closeSource();
      startFallback();
      scheduleReconnect();
    }
  }, WATCHDOG_MS);

  // Onglet masqué : on ne paie rien. Onglet revenu : tout est potentiellement
  // périmé, on invalide et on rouvre le flux si besoin.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      closeSource();
      stopFallback();
    } else {
      invalidateAll();
      if (!source) connect();
      if (!source && !fallbackTimer) startFallback();
    }
  });
}
