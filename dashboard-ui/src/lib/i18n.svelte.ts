/**
 * État de langue du dashboard (runes Svelte 5).
 *
 * Le dictionnaire vient de `locales.ts` (porté de l'ancien `app.js`).
 * Règle : toute chaîne visible passe par `t()`, dans les deux locales.
 * Interpolation : `t('log.deleted_before', { d: '2026-01-01' })`.
 *
 * Motif « objet mutable » : Svelte 5 refuse d'exporter un `$state` qu'on
 * réassigne (`state_invalid_export`). On expose donc une cellule réactive
 * `{ current }` — même convention que `theme.svelte.ts`.
 */

import { LOCALE, LOCALES, type Locale } from './locales';

const STORAGE_KEY = 'lang';

function readStored(): Locale | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return LOCALES.includes(v as Locale) ? (v as Locale) : null;
  } catch {
    return null;
  }
}

function detectInitial(): Locale {
  // L'ancien dashboard partait sur `en` par défaut : on garde ce
  // comportement pour ne pas basculer la langue des utilisateurs existants.
  return readStored() ?? 'en';
}

/** Cellule réactive : `lang.current` dans les templates. */
export const lang = $state<{ current: Locale }>({ current: detectInitial() });

/** Applique la langue courante au document (lang="..."). */
function applyDocument(): void {
  try {
    document.documentElement.lang = lang.current;
  } catch {
    // hors navigateur (tests) : rien à faire
  }
}

/** Change de langue, persiste le choix, et le renvoie au serveur. */
export async function setLocale(next: Locale): Promise<void> {
  lang.current = next;
  applyDocument();
  try {
    localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // stockage indisponible
  }
  // Le back-end garde la préférence côté serveur (reprise après redémarrage).
  try {
    await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lang: next })
    });
  } catch {
    // best-effort : la langue locale reste appliquée
  }
}

/**
 * Reprise de la langue serveur quand le navigateur n'a pas de préférence
 * locale (autre origine localhost/127.0.0.1, stockage vidé, nouvel appareil).
 * La préférence locale reste prioritaire.
 */
export async function syncLocaleFromServer(): Promise<void> {
  if (readStored()) return;
  try {
    const response = await fetch('/api/config', { headers: { Accept: 'application/json' } });
    if (!response.ok) return;
    const data = await response.json();
    const server = String((data as { lang?: unknown }).lang ?? '');
    if (!LOCALES.includes(server as Locale)) return;
    if ((server as Locale) === lang.current) return;
    lang.current = server as Locale;
    applyDocument();
    try {
      localStorage.setItem(STORAGE_KEY, server);
    } catch {
      // stockage indisponible
    }
  } catch {
    // serveur indisponible : garder la langue locale
  }
}

/** Traduit `key` dans la langue courante, avec repli sur `en` puis la clé. */
export function t(key: string, params?: Record<string, string | number>): string {
  const table = LOCALE[lang.current] || LOCALE.en;
  let text = table[key] ?? LOCALE.en[key] ?? key;
  if (params) {
    for (const [name, value] of Object.entries(params)) {
      text = text.replaceAll(`{${name}}`, String(value));
    }
  }
  return text;
}

// Applique la langue dès le chargement du module.
applyDocument();
// Puis tente la préférence serveur si aucune préférence locale n'existe.
void syncLocaleFromServer();
