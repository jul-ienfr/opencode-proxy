<script lang="ts">
  /**
   * Quotas d'abonnement OpenCode Go, par workspace.
   *
   * `GET /api/quotas` renvoie un objet par workspace, chacun avec trois
   * périodes (glissante 5 h, hebdo, mois) et un `reset_in_sec` relatif à la
   * réponse : l'horloge de compte à rebours est donc recalculée à chaque
   * rechargement de données, exactement comme le faisait le tableau de bord
   * historique.
   */

  import { apiGet } from '$lib/api';
  import { config } from '$lib/config.svelte';
  import { formatCountdown, formatDateTime } from '$lib/format';
  import { t } from '$lib/i18n.svelte';
  import { createQuery } from '$lib/query.svelte';
  import Card from '$lib/components/Card.svelte';
  import Empty from '$lib/components/Empty.svelte';
  import Spinner from '$lib/components/Spinner.svelte';

  type Period = 'rolling' | 'weekly' | 'monthly';

  const PERIODS: Period[] = ['rolling', 'weekly', 'monthly'];

  // Clés i18n explicites plutôt qu'un littéral calqué : `t()` est typé sur les
  // clés réelles du dictionnaire.
  const PERIOD_LABEL: Record<Period, string> = {
    rolling: 'quotas.rolling',
    weekly: 'quotas.weekly',
    monthly: 'quotas.monthly'
  };

  interface QuotaPeriod {
    usage_percent?: number;
    reset_in_sec?: number;
  }

  interface WorkspaceQuotas {
    status?: string;
    error?: string;
    quotas?: Partial<Record<Period, QuotaPeriod>>;
    fetched_at?: string | null;
  }

  type QuotasResponse = Record<string, WorkspaceQuotas>;

  /** Seuils de couleur identiques à l'implémentation historique. */
  const DANGER_AT = 85;
  const WARNING_AT = 60;
  /** Grâce accordée à « Réinitialisation… » avant de basculer sur « — ». */
  const RESET_GRACE_SEC = 10;
  const TICK_MS = 1000;

  let nowSec = $state(Math.floor(Date.now() / 1000));

  const quotasQ = createQuery<QuotasResponse>({
    load: (signal) => apiGet<QuotasResponse>('/api/quotas', signal),
    tags: () => ['quotas'],
    initial: {}
  });

  const workspaces = $derived(Object.entries(quotasQ.data ?? {}));
  const showHeaders = $derived(workspaces.length > 1);

  /** Alias lisible (API Keys) → id de workspace, pour les en-têtes. */
  const aliases = $derived.by(() => {
    const map = new Map<string, string>();
    for (const key of config.data?.api_keys ?? []) {
      if (key.go_workspace_id && key.alias) map.set(key.go_workspace_id, key.alias);
    }
    return map;
  });

  /**
   * Cible absolue (epoch s) de chaque compte à rebours, figée au moment où
   * les données arrivent : `reset_in_sec` est relatif à la réponse serveur.
   */
  const targets = $derived.by(() => {
    const receivedAt = Math.floor(Date.now() / 1000);
    const out: Record<string, Partial<Record<Period, number>>> = {};
    for (const [wsId, ws] of workspaces) {
      for (const period of PERIODS) {
        const sec = ws.quotas?.[period]?.reset_in_sec ?? 0;
        if (sec > 0) (out[wsId] ??= {})[period] = receivedAt + sec;
      }
    }
    return out;
  });

  const hasCountdown = $derived(Object.keys(targets).length > 0);

  /** Horloge partagée : une seule intervalle pour tous les compteurs. */
  $effect(() => {
    if (!hasCountdown) return;
    const id = setInterval(() => {
      nowSec = Math.floor(Date.now() / 1000);
    }, TICK_MS);
    return () => clearInterval(id);
  });

  /**
   * Libellé du compte à rebours, avec la borne de sécurité de l'ancienne
   * interface : le quota peut déjà être réinitialisé côté serveur, le prochain
   * rafraîchissement (≤ 5 min) donnera une nouvelle cible — sans quoi le
   * compteur afficherait « Réinitialisation… » pendant des minutes.
   */
  function resetLabel(target: number): string {
    const remaining = target - nowSec;
    if (remaining > 0) return t('quotas.resets_in') + formatCountdown(remaining);
    if (nowSec - target > RESET_GRACE_SEC) return '—';
    return t('quotas.resetting');
  }

  function barLevel(pct: number): 'ok' | 'warning' | 'danger' {
    if (pct >= DANGER_AT) return 'danger';
    if (pct >= WARNING_AT) return 'warning';
    return 'ok';
  }

  function percent(q: QuotaPeriod | undefined): number {
    return Math.min(100, Math.max(0, q?.usage_percent ?? 0));
  }

  function displayName(wsId: string): string {
    return aliases.get(wsId) || wsId;
  }
</script>

<Card title={t('quotas.title')}>
  {#if quotasQ.loading && workspaces.length === 0}
    <Spinner />
  {:else if quotasQ.error}
    <p class="hint hint--error">{t('quotas.error')}</p>
  {:else if workspaces.length === 0}
    <Empty title={t('quotas.not_configured')} />
  {:else}
    {#each workspaces as [wsId, ws] (wsId)}
      {@const wsQuotas = ws.quotas ?? {}}
      {@const hasQuota = ws.status !== 'error' && PERIODS.some((p) => wsQuotas[p])}
      {#if ws.status === 'error' && !hasQuota}
        <p class="hint hint--error">
          {displayName(wsId)}: {t('status.error')} — {ws.error}
        </p>
      {:else}
        <section class="ws">
          {#if showHeaders}
            <h3 class="ws-title" title={aliases.has(wsId) ? wsId : undefined}>
              Workspace: {displayName(wsId)}
            </h3>
          {/if}

          {#if ws.status === 'error'}
            <p class="hint hint--error">
              {t('quotas.last_updated')}{ws.error}.
            </p>
          {/if}

          <div class="grid">
            {#each PERIODS as period (period)}
              {@const q = wsQuotas[period]}
              {@const pct = percent(q)}
              {@const target = targets[wsId]?.[period]}
              <div class="item">
                <div class="item-head">
                  <span class="label">{t(PERIOD_LABEL[period])}</span>
                  {#if target}
                    <span class="reset">{resetLabel(target)}</span>
                  {/if}
                </div>
                <div class="bar">
                  <div class="bar-fill bar-fill--{barLevel(pct)}" style="width: {pct}%"></div>
                </div>
                <div class="stats">
                  <span class="pct">{pct.toFixed(1)}%</span>
                  <span class="remaining">{(100 - pct).toFixed(1)}{t('quotas.remaining')}</span>
                </div>
              </div>
            {/each}
          </div>

          {#if ws.fetched_at}
            <p class="fetched">{t('quotas.last_updated')}{formatDateTime(ws.fetched_at)}</p>
          {/if}
        </section>
      {/if}
    {/each}
  {/if}
</Card>

<style>
  .hint {
    margin: 0 0 var(--sp-4);
    color: var(--text-dim);
    font-size: var(--fs-md);
  }

  .hint--error {
    color: var(--danger);
  }

  .ws + .ws {
    margin-top: var(--sp-6);
    padding-top: var(--sp-5);
    border-top: 1px solid var(--border);
  }

  .ws-title {
    margin: 0 0 var(--sp-4);
    font-size: var(--fs-lg);
    font-weight: 600;
    color: var(--text);
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
    gap: var(--sp-4);
  }

  .item {
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: var(--sp-4);
  }

  .item-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--sp-3);
    margin-bottom: var(--sp-3);
  }

  .label {
    font-weight: 600;
    color: var(--text);
    font-size: var(--fs-md);
  }

  .reset {
    font-size: var(--fs-sm);
    color: var(--text-dim);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .bar {
    height: 8px;
    border-radius: var(--radius-full);
    background: var(--surface-3);
    overflow: hidden;
  }

  .bar-fill {
    height: 100%;
    border-radius: var(--radius-full);
    transition: width 0.3s ease;
  }

  .bar-fill--ok {
    background: var(--success);
  }

  .bar-fill--warning {
    background: var(--warn);
  }

  .bar-fill--danger {
    background: var(--danger);
  }

  .stats {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--sp-3);
    margin-top: var(--sp-3);
    font-size: var(--fs-sm);
  }

  .pct {
    font-family: var(--font-mono);
    font-weight: 600;
    color: var(--text);
    font-variant-numeric: tabular-nums;
  }

  .remaining {
    color: var(--text-dim);
  }

  .fetched {
    margin: var(--sp-4) 0 0;
    font-size: var(--fs-sm);
    color: var(--text-faint);
  }
</style>
