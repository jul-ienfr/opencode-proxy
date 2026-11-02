/**
 * Mini couche de données réactive, adossée aux runes Svelte 5.
 *
 * Pourquoi un outil maison plutôt qu'une librairie ?
 *  - le besoin est étroit : GET JSON + refetch + invalidation depuis le SSE ;
 *  - zéro dépendance = bundle plus léger (objectif « moins gourmand ») ;
 *  - les runes donnent une réactivité fine : seules les vues dépendantes se
 *    repeignent, sans VDOM.
 *
 * Comportements :
 *  - *stale-while-revalidate* : on garde les données précédentes pendant le
 *    refetch (pas de clignotement à chaque ping SSE) ;
 *  - `deps` réactif : quand les entrées changent (période, filtre, onglet),
 *    la requête se rejoue toute seule ;
 *  - tags : le SSE invalide par domaine (`stats`, `history`, `vpn`, ...)
 *    sans recharger l'intégralité du tableau de bord ;
 *  - annulation : le signal de la requête précédente est envoyé au démontage,
 *    fini les réponses obsolètes qui écrivent sur un état neuf.
 */

import { onDestroy } from 'svelte';

export type QueryStatus = 'idle' | 'loading' | 'success' | 'error';

export interface QueryOptions<T> {
  /** Charge les données. Reçoit un `AbortSignal` lié à la requête. */
  load: (signal: AbortSignal) => Promise<T>;
  /**
   * Dépendances réactives : si leur valeur change, la requête se rejoue.
   * Comparaison `Object.is` sur le résultat (pensez à renvoyer une clé stable,
   * par exemple un string de concaténation).
   */
  deps?: () => unknown;
  /** Tags d'invalidation : le SSE appelle `invalidateTag('stats')`. */
  tags?: () => string[];
  /** Désactive l'exécution automatique (ex : onglet caché). */
  enabled?: () => boolean;
  /** Valeur initiale (évite un état vide au premier rendu). */
  initial?: T;
}

interface Registrable {
  tags: () => string[];
  refresh: (silent?: boolean) => Promise<void>;
}

const registry = new Set<Registrable>();

/** Rafraîchit toutes les requêtes portant ce tag. */
export function invalidateTag(tag: string): void {
  for (const q of registry) {
    if (q.tags().includes(tag)) void q.refresh(true);
  }
}

/** Rafraîchit tout (reconnexion SSE, retour d'onglet). */
export function invalidateAll(): void {
  for (const q of registry) void q.refresh(true);
}

export class Query<T> {
  data = $state<T | undefined>(undefined);
  error = $state<Error | undefined>(undefined);
  loading = $state(false);
  fetching = $state(false);
  updatedAt = $state(0);

  readonly #load: (signal: AbortSignal) => Promise<T>;
  readonly #tags: () => string[];
  readonly #enabled: () => boolean;

  #ctrl: AbortController | null = null;
  #mounted = true;

  constructor(options: QueryOptions<T>) {
    this.#load = options.load;
    this.#tags = options.tags ?? (() => []);
    this.#enabled = options.enabled ?? (() => true);

    if (options.initial !== undefined) this.data = options.initial;
    if (this.#enabled()) this.loading = true;

    const registrable: Registrable = {
      tags: this.#tags,
      refresh: (silent?: boolean) => this.refresh(silent)
    };
    registry.add(registrable);

    // `onDestroy`/`$effect` ne sont légaux que pendant l'init d'un composant :
    // un Query créé au niveau module reste utilisable (méthodes explicites).
    safeOnDestroy(() => {
      this.#mounted = false;
      this.#ctrl?.abort();
      // Retrait du registre : sinon les timers SSE garderaient des zombies.
      registry.delete(registrable);
    });

    // Premier chargement.
    if (this.#enabled()) void this.refresh();

    // Refetch automatique quand les entrées changent.
    if (options.deps) {
      let previous: unknown;
      let initialized = false;
      safeEffect(() => {
        const next = options.deps?.();
        if (!initialized) {
          initialized = true;
          previous = next;
          return; // le premier tour est déjà géré par refresh()
        }
        if (Object.is(previous, next)) return;
        previous = next;
        if (this.#enabled()) void this.refresh(true);
      });
    }
  }

  get status(): QueryStatus {
    if (this.error) return 'error';
    if (this.loading) return 'loading';
    if (this.data !== undefined) return 'success';
    return 'idle';
  }

  get isEmpty(): boolean {
    return this.data !== undefined && !this.error;
  }

  /** Rejoue la requête. `silent = true` → pas d'état de chargement (SWR). */
  async refresh(silent = false): Promise<void> {
    if (!this.#mounted) return;

    this.#ctrl?.abort();
    const ctrl = new AbortController();
    this.#ctrl = ctrl;

    if (silent) this.fetching = true;
    else this.loading = true;

    try {
      const data = await this.#load(ctrl.signal);
      if (!this.#mounted || ctrl.signal.aborted) return;
      this.data = data;
      this.error = undefined;
      this.updatedAt = Date.now();
      lastRefresh.current = this.updatedAt;
    } catch (err) {
      if (!this.#mounted || ctrl.signal.aborted) return;
      // Une annulation n'est pas une erreur : on garde l'état précédent.
      if (isAbort(err)) return;
      this.error = toError(err);
    } finally {
      if (this.#mounted && !ctrl.signal.aborted) {
        this.loading = false;
        this.fetching = false;
      }
    }
  }

  /** Invalide le cache puis rejoue (changement de filtre utilisateur). */
  async invalidate(): Promise<void> {
    await this.refresh(true);
  }
}

/**
 * Horodatage (ms) de la dernière requête réussie, toutes pages confondues.
 * Alimente le « Dernière mise à jour » du pied de page.
 */
export const lastRefresh = $state<{ current: number }>({ current: 0 });

export function createQuery<T>(options: QueryOptions<T>): Query<T> {
  return new Query<T>(options);
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

function toError(err: unknown): Error {
  if (err instanceof Error) return err;
  return new Error(String(err));
}

/** `onDestroy` tolérant : no-op en dehors de l'init d'un composant. */
function safeOnDestroy(fn: () => void): void {
  try {
    onDestroy(fn);
  } catch {
    // pas de cycle de vie Svelte (création module) : on n'a rien à nettoyer
  }
}

/** `$effect` tolérant : no-op si aucun contexte réactif n'est actif. */
function safeEffect(fn: () => void): void {
  try {
    $effect(fn);
  } catch {
    // pas de contexte réactif : le refetch devra être appelé explicitement
  }
}
