/**
 * Thème clair/sombre (runes Svelte 5).
 *
 * La classe est posée sur `<html>` via `data-theme`, ce qui est exactement la
 * convention que les tokens de `app.css` attendent. Le choix est persisté et
 * rétabli au démarrage, sans clignotement de thème (appliqué avant le premier
 * rendu par `main.ts`).
 */

const STORAGE_KEY = 'theme';

export type Theme = 'dark' | 'light';

export const theme = $state<{ current: Theme }>({
  current: detectInitialTheme()
});

function detectInitialTheme(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === 'dark' || stored === 'light') return stored;
  } catch {
    // stockage indisponible
  }
  // Parité avec l'ancien dashboard (`localStorage.theme || 'dark'`) : la
  // première visite est en sombre, le choix de l'utilisateur reste maître.
  // On ne suit PAS `prefers-color-scheme` ici pour ne pas changer d'aspect
  // aux utilisateurs existants au premier chargement.
  return 'dark';
}

/**
 * Pose `data-theme` sur `<html>` SANS persister.
 *
 * Séparation voulue : au démarrage on applique la valeur détectée (un simple
 * effet de bord), seule une action utilisateur (`setTheme`/`toggleTheme`)
 * écrit dans `localStorage`. Sans cette distinction, le premier démarrage
 * figerait le thème détecté comme s'il avait été choisi.
 */
export function applyTheme(next: Theme): void {
  theme.current = next;
  try {
    document.documentElement.setAttribute('data-theme', next);
  } catch {
    // document indisponible
  }
}

/** Change de thème et mémorise le choix. */
export function setTheme(next: Theme): void {
  applyTheme(next);
  try {
    localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // stockage indisponible
  }
}

/** Bascule clair <-> sombre (action utilisateur → persistée). */
export function toggleTheme(): void {
  setTheme(theme.current === 'dark' ? 'light' : 'dark');
}
