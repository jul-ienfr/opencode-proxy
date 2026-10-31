<script lang="ts">
  /**
   * Visionneuse des logs debug.
   *
   * Le backend renvoie 200 lignes par page (`total` = nombre total de
   * lignes), les plus récentes en bas. L'onglet n'est proposé que quand le
   * mode debug est actif (voir App.svelte) — cet état vide couvre donc le cas
   * d'un lien direct vers `#debug` alors que le mode est éteint.
   */

  import { apiDelete, apiGet } from '$lib/api';
  import { debugQuery } from '$lib/debug.svelte';
  import { t } from '$lib/i18n.svelte';
  import { createQuery } from '$lib/query.svelte';
  import { getErrorDetails } from '$lib/format';
  import Card from '$lib/components/Card.svelte';
  import Empty from '$lib/components/Empty.svelte';
  import Pagination from '$lib/components/Pagination.svelte';
  import Spinner from '$lib/components/Spinner.svelte';
  import Toggle from '$lib/components/Toggle.svelte';

  const PAGE_SIZE = 200;
  /** Rafraîchissement automatique : identique à l'ancien tableau de bord. */
  const AUTO_REFRESH_MS = 3000;

  interface DebugLogs {
    logs: string[];
    total: number;
    has_more?: boolean;
  }

  let page = $state(1);
  let autoRefresh = $state(false);
  let status = $state<{ kind: 'ok' | 'error'; message: string } | null>(null);
  let viewport: HTMLDivElement | undefined = $state();

  const logsQ = createQuery<DebugLogs>({
    load: (signal) =>
      apiGet<DebugLogs>(
        `/api/debug/logs?limit=${PAGE_SIZE}&offset=${(page - 1) * PAGE_SIZE}`,
        signal
      ),
    // Les logs changent quand la config change (mode debug) : même tag.
    tags: () => ['config'],
    deps: () => page,
    initial: { logs: [], total: 0 }
  });

  const enabled = $derived(debugQuery.data?.enabled ?? false);
  const lines = $derived(logsQ.data?.logs ?? []);

  /**
   * Auto-refresh : l'onglet n'existe que lorsqu'il est monté, donc l'ancien
   * test « onglet actif » de l'implémentation historique est inutile ici.
   */
  $effect(() => {
    if (!autoRefresh || !enabled) return;
    const id = setInterval(() => {
      void logsQ.refresh(true);
    }, AUTO_REFRESH_MS);
    return () => clearInterval(id);
  });

  /**
   * Coller le bas du viewport sur la dernière ligne, mais seulement si
   * l'utilisateur est déjà en bas (ou change de page) : sinon l'auto-refresh
   * lui volerait le défilement, ce que faisait l'ancienne implémentation.
   */
  let lastPage = 0;
  $effect(() => {
    const el = viewport;
    const data = logsQ.data;
    const current = page;
    if (!el || !data) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    const pageChanged = current !== lastPage;
    lastPage = current;
    if (pageChanged || nearBottom) {
      requestAnimationFrame(() => {
        el.scrollTop = el.scrollHeight;
      });
    }
  });

  async function clearLogs(): Promise<void> {
    try {
      await apiDelete<{ status?: string }>('/api/debug/logs');
      status = { kind: 'ok', message: t('debug.cleared') };
      page = 1;
      await logsQ.refresh();
      window.setTimeout(() => {
        status = null;
      }, 3000);
    } catch (e) {
      const details = getErrorDetails(e instanceof Error ? e.message : String(e));
      status = {
        kind: 'error',
        message: details.explanation || details.raw || t('status.error')
      };
    }
  }
</script>

<Card title={t('debug.title')}>
  {#if !enabled}
    <Empty kind="empty" title={t('debug.no_data')} hint={t('config.debug_desc')} />
  {:else}
    <div class="actions">
      <button class="btn btn--danger btn--sm" onclick={clearLogs}>
        {t('debug.clear')}
      </button>
      <Toggle
        label={t('debug.auto_refresh')}
        checked={autoRefresh}
        onchange={(next) => (autoRefresh = next)}
      />
      {#if status}
        <span class="status status--{status.kind}" role="status">{status.message}</span>
      {/if}
    </div>

    <div class="viewport" bind:this={viewport}>
      {#if logsQ.loading}
        <Spinner />
      {:else if lines.length === 0}
        <p class="empty">{t('debug.no_data')}</p>
      {:else}
        {#each lines as line, i (i)}
          <div class="line">{line}</div>
        {/each}
      {/if}
    </div>

    <Pagination
      page={page}
      total={logsQ.data?.total ?? 0}
      pageSize={PAGE_SIZE}
      onchange={(p) => (page = p)}
    />
  {/if}
</Card>

<style>
  .actions {
    display: flex;
    align-items: center;
    gap: var(--sp-4);
    flex-wrap: wrap;
    margin-bottom: var(--sp-3);
  }

  .status {
    font-size: var(--fs-sm);
  }

  .status--ok {
    color: var(--success);
  }

  .status--error {
    color: var(--danger);
  }

  /* Bloc de logs : monospace, retour à la ligne forcé, hauteur bornée —
     identique au rendu historique pour rester lisible en scroll. */
  .viewport {
    background: var(--surface-3);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: var(--sp-3);
    max-height: 600px;
    overflow-y: auto;
    font-family: var(--font-mono);
    font-size: var(--fs-sm);
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-all;
  }

  .line + .line {
    margin-top: 0;
  }

  .empty {
    margin: 0;
    color: var(--text-dim);
  }
</style>
