/**
 * Helpers de formatage purs — sans état, sans DOM, testables tels quels.
 *
 * Portage fidèle des fonctions utilitaires de l'ancien `app.js`, avec deux
 * suppressions utiles :
 *  - `escHtml` disparaît : Svelte échappe toute interpolation de texte,
 *    ce qui neutralise la classe de failles XSS que l'ancien code devait
 *    gérer à la main (110 appels, parfois oubliés) ;
 *  - les formats n'embarquent plus `t()` : le texte traduit est décidé par
 *    l'appelant, ce qui évite des chaînes figées en français.
 */

/** Explications des codes HTTP utiles au proxy (tooltips d'erreur). */
export const HTTP_STATUS_EXPLANATIONS: Record<number, string> = {
  400: 'Requête incorrecte — la requête est mal formée',
  401: 'Non autorisé — clé API invalide ou manquante',
  403: 'Interdit — accès refusé',
  404: 'Non trouvé — ressource demandée indisponible',
  408: 'Délai dépassé — le serveur a expiré en attendant la requête',
  413: 'Charge trop volumineuse — requête dépasse la taille maximale autorisée',
  429: 'Limite dépassée — trop de requêtes, veuillez ralentir',
  500: 'Erreur interne — le serveur a rencontré une erreur',
  502: 'Mauvaise passerelle — le serveur amont a renvoyé une réponse invalide',
  503: 'Service indisponible — service temporairement indisponible',
  504: 'Délai passerelle dépassé — le serveur amont a expiré',
  520: "Cloudflare 520 — le serveur d'origine a renvoyé une réponse vide ou inconnue",
  521: 'Cloudflare 521 — le serveur d\'origine est hors ligne ou a refusé la connexion',
  522: 'Cloudflare 522 — délai de connexion au serveur d\'origine dépassé',
  523: 'Cloudflare 523 — le serveur d\'origine est injoignable',
  524: 'Cloudflare 524 — délai de connexion dépassé (origine trop lente)',
  525: 'Cloudflare 525 — échec handshake SSL entre Cloudflare et l\'origine',
  526: 'Cloudflare 526 — certificat SSL invalide sur le serveur d\'origine',
  530: 'Cloudflare 530 — erreur DNS d\'origine (domaine non résolu)'
};

export interface ErrorDetails {
  /** Message d'erreur brut (ce que le serveur a renvoyé). */
  raw: string;
  /** Explication humaine du code HTTP si on a pu en déduire un, sinon ''. */
  explanation: string;
}

/** Extrait un code HTTP dans un message d'erreur et propose une explication. */
export function getErrorDetails(errorStr: string | null | undefined): ErrorDetails {
  const raw = errorStr || '';
  if (!raw) return { raw, explanation: '' };
  const match = raw.match(/HTTP\s+(\d{3})/i);
  if (match) {
    const code = parseInt(match[1], 10);
    return { raw, explanation: HTTP_STATUS_EXPLANATIONS[code] || `HTTP status code ${code}` };
  }
  return { raw, explanation: '' };
}

/** Nombre localisé (ex : 1 234 567). */
export function formatNumber(num: number | null | undefined): string {
  if (num === null || num === undefined || Number.isNaN(num)) return '0';
  return num.toLocaleString();
}

/** Heure courte locale (ex : 14:32:05). */
export function formatTime(d: Date = new Date()): string {
  return d.toLocaleTimeString();
}

/** Horodatage ISO → date+heure locale, ou '-' si absent/illisible. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '-';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? String(iso) : date.toLocaleString();
}

/** Date locale au format `YYYY-MM-DD` (compatible `from_date`/`to_date`). */
export function toDateInput(d: Date = new Date()): string {
  return d.toLocaleDateString('en-CA');
}

/** Date d'il y a `n` jours, au format `YYYY-MM-DD`. */
export function daysAgo(n: number, from: Date = new Date()): string {
  const d = new Date(from);
  d.setDate(d.getDate() - n);
  return toDateInput(d);
}

/**
 * Durée humaine pour un compte à rebours (`reset_in_sec`).
 * Retourne '' si `seconds <= 0` (le compteur est déjà expiré).
 */
export function formatCountdown(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

/** Durée en millisecondes → « 840ms » / « 1.25s ». */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '-';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

/** Octets → taille lisible (0 B / 512 B / 3.2 KB / 1.4 MB). */
export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes || bytes <= 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Pourcentage sûr : borne à [0,100] et gère l'absence de valeur. */
export function formatPercent(
  value: number | null | undefined,
  digits = 1,
  fallback = '-'
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return fallback;
  return `${value.toFixed(digits)}%`;
}

/** Couleur d'état d'un quota selon son taux d'usage (0–100). */
export function quotaLevel(pct: number): 'ok' | 'warning' | 'danger' {
  const v = Math.min(100, Math.max(0, pct || 0));
  if (v >= 85) return 'danger';
  if (v >= 60) return 'warning';
  return 'ok';
}
