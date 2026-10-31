<script lang="ts">
  import type { Component } from 'svelte';
  import { navigate, route, TABS, type TabId } from '$lib/router.svelte';
  import { lang, setLocale, t } from '$lib/i18n.svelte';
  import type { Locale } from '$lib/locales';
  import { theme, toggleTheme } from '$lib/theme.svelte';
  import { realtime } from '$lib/realtime.svelte';
  import { config } from '$lib/config.svelte';
  import { debugQuery } from '$lib/debug.svelte';
  import { PAGE_LOADERS } from './pages';
  import { lastRefresh } from '$lib/query.svelte';
  import { formatDateTime } from '$lib/format';
  import Toasts from '$lib/components/Toasts.svelte';
  import DeleteHistory from '$lib/components/DeleteHistory.svelte';

  /** Onglets visibles : `debug` n'apparaît que si le mode debug est actif. */
  const visibleTabs = $derived(
    TABS.filter((tab) => tab !== 'debug' || (debugQuery.data?.enabled ?? false)) as readonly TabId[]
  );

  // Capitalisé : Svelte 5 rend un composant via un identifiant en majuscule.
  let Current = $state<Component<Record<string, never>> | null>(null);
  let loadError = $state<string | null>(null);

  // Chargement paresseux : une vue à la fois, réexécuté à chaque changement
  // d'onglet. Le cleanup invalide la promesse pour qu'un onglet cliqué puis
  // quitté très vite n'installe jamais le composant périmé.
  $effect(() => {
    const wanted = route.current.tab;
    let alive = true;
    Current = null;
    loadError = null;

    PAGE_LOADERS[wanted]().then(
      (mod) => {
        if (alive && route.current.tab === wanted) Current = mod.default;
      },
      (err) => {
        if (alive) loadError = String(err);
      }
    );

    return () => {
      alive = false;
    };
  });

  // Navigation au clavier entre onglets (flèches gauche/droite).
  function onTabKeydown(event: KeyboardEvent, index: number): void {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const delta = event.key === 'ArrowRight' ? 1 : -1;
    const next = visibleTabs[(index + delta + visibleTabs.length) % visibleTabs.length];
    navigate(next);
    document.querySelector<HTMLButtonElement>(`[data-tab="${next}"]`)?.focus();
  }

  /** Badge d'état du flux temps réel. */
  const realtimeKind = $derived(
    realtime.status === 'open' ? 'ok' : realtime.status === 'connecting' ? 'warn' : 'down'
  );

  // Adresse locale affichée dans le header (copiable), issue de la config.
  const localUrl = $derived(
    config.data?.port ? `http://localhost:${config.data.port}` : ''
  );

  let copied = $state(false);
  async function copyLocalUrl(): Promise<void> {
    if (!localUrl) return;
    try {
      await navigator.clipboard.writeText(localUrl);
      copied = true;
      setTimeout(() => (copied = false), 1500);
    } catch {
      // presse-papiers refusé : on ignore
    }
  }
</script>

<div class="shell">
  <header class="topbar">
    <div class="brand">
      <h1>OpenCode Proxy</h1>
      <span class="topbar-sub">{t('stats.overview')}</span>
    </div>

    <div class="topbar-right">
      <span class="health" class:health--ok={realtimeKind === 'ok'}
        class:health--warn={realtimeKind === 'warn'} class:health--down={realtimeKind === 'down'}
        title="Temps réel (SSE)">
        <span class="health-dot" aria-hidden="true"></span>
        <span class="health-text">SSE</span>
      </span>

      {#if localUrl}
        <button class="btn btn--ghost btn--sm addr" onclick={copyLocalUrl} title={t('proxy.copy')}>
          {copied ? t('proxy.copied') : localUrl}
        </button>
      {/if}

      <select
        class="select lang"
        aria-label={t('lang.fr')}
        value={lang.current}
        onchange={(e) => setLocale(e.currentTarget.value as Locale)}
      >
        <option value="en">English</option>
        <option value="fr">Français</option>
      </select>

      <button
        class="btn btn--ghost btn--icon"
        onclick={toggleTheme}
        aria-label={t('show_hide')}
        title={t('show_hide')}>
        {theme.current === 'dark' ? '☾' : '☀'}
      </button>
    </div>
  </header>

  <!-- `role="tablist"`/`tabpanel` posés sur des <div> : un <nav>/<main>
       porte un rôle implicite (navigation/landmark) que Svelte signale
       comme conflictuel avec un rôle de tabulation. -->
  <div class="nav" role="tablist" aria-label="Navigation principale">
    {#each visibleTabs as tab, i (tab)}
      <button
        role="tab"
        data-tab={tab}
        id="tab-{tab}"
        aria-selected={route.current.tab === tab}
        aria-controls="view-{tab}"
        class="nav-item"
        class:nav-item--active={route.current.tab === tab}
        onclick={() => navigate(tab)}
        onkeydown={(e) => onTabKeydown(e, i)}
      >
        {t(`nav.${tab === 'free-models' ? 'free_models' : tab === 'debug' ? 'debug_logs' : tab}`)}
      </button>
    {/each}
  </div>

  <main>
    <div
      id="view-{route.current.tab}"
      role="tabpanel"
      aria-labelledby="tab-{route.current.tab}"
      tabindex="-1"
    >
      {#if loadError}
        <div class="boot-error">
          <strong>Échec du chargement de la vue.</strong>
          <code>{loadError}</code>
        </div>
      {:else if Current}
        <Current />
      {/if}
    </div>
  </main>

  <footer class="footer">
    <span class="footer-time">
      {t('last.update')}{lastRefresh.current
        ? formatDateTime(new Date(lastRefresh.current).toISOString())
        : '--'}
    </span>
    <DeleteHistory />
  </footer>
</div>

<Toasts />

<style>
  .shell {
    width: min(1600px, 96%);
    margin: 0 auto;
    padding: var(--sp-5) 0 var(--sp-8);
  }

  /* --- Barre haute --- */
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--sp-4);
    flex-wrap: wrap;
    margin-bottom: var(--sp-4);
  }

  .brand {
    display: flex;
    align-items: baseline;
    gap: var(--sp-3);
    min-width: 0;
  }

  .brand h1 {
    font-size: var(--fs-2xl);
    letter-spacing: -0.02em;
  }

  .topbar-sub {
    font-size: var(--fs-sm);
    color: var(--text-faint);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .topbar-right {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    flex-wrap: wrap;
  }

  .health {
    display: inline-flex;
    align-items: center;
    gap: var(--sp-2);
    padding: 4px 10px;
    border-radius: var(--radius-full);
    border: 1px solid var(--border-strong);
    background: var(--surface-2);
    font-size: var(--fs-xs);
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .health-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-faint);
  }

  .health--ok .health-dot {
    background: var(--success);
    box-shadow: 0 0 0 3px var(--success-bg);
  }
  .health--warn .health-dot {
    background: var(--warn);
    box-shadow: 0 0 0 3px var(--warn-bg);
  }
  .health--down .health-dot {
    background: var(--danger);
    box-shadow: 0 0 0 3px var(--danger-bg);
  }

  .addr {
    font-family: var(--font-mono);
    font-size: var(--fs-sm);
  }

  .lang {
    width: auto;
    padding: 6px 8px;
    font-size: var(--fs-md);
  }

  /* --- Navigation par onglets --- */
  .nav {
    display: flex;
    gap: var(--sp-1);
    border-bottom: 1px solid var(--border);
    margin-bottom: var(--sp-5);
    overflow-x: auto;
    scrollbar-width: thin;
  }

  .nav-item {
    padding: 9px 16px;
    border: none;
    background: none;
    color: var(--text-dim);
    font-family: inherit;
    font-size: var(--fs-base);
    font-weight: 500;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    white-space: nowrap;
    transition:
      color 0.15s ease,
      border-color 0.15s ease;
  }

  .nav-item:hover {
    color: var(--text);
  }

  .nav-item--active {
    color: var(--accent);
    border-bottom-color: var(--accent);
  }

  .boot-error {
    display: grid;
    gap: var(--sp-2);
    padding: var(--sp-5);
    border: 1px solid var(--danger);
    background: var(--danger-bg);
    border-radius: var(--radius);
    font-size: var(--fs-md);
  }

  .boot-error code {
    font-size: var(--fs-sm);
    color: var(--text-dim);
    word-break: break-all;
  }
</style>
