<script lang="ts">
  /**
   * Carte Capacité (feature 010) : combien d'endpoints par provider, combien
   * sont utilisables, et combien suffisent pour les six stations cibles.
   *
   * Lecture seule : le calcul vient de /api/egress/capacity. Aucun routage
   * n'est modifié ici.
   */

  import { apiGet, apiPost } from '$lib/api';
  import { lang, t } from '$lib/i18n.svelte';
  import { createQuery, invalidateTag } from '$lib/query.svelte';
  import { showToast } from '$lib/toast.svelte';

  type ProviderName = 'nordvpn' | 'warp' | 'proxies' | 'direct';

  interface ProviderCapacity {
    name: ProviderName;
    configured: number;
    enabled: number;
    usable: number;
    selected: boolean;
    weight: number | null;
    expected_share: number | null;
    per_endpoint: number | null;
    observed: number;
    observed_share: number | null;
    required: number | null;
    deficit: number;
    status: string;
  }

  interface Message {
    code: string;
    severity: string;
    fr: string;
    en: string;
  }

  interface CapacityReport {
    target_slots: number;
    strategy: 'fallback' | 'balance';
    reserve_policy: string;
    requested_policy: string;
    providers: Record<ProviderName, ProviderCapacity>;
    selected: string[];
    total_usable: number;
    total_required: number;
    observed_requests: number;
    deficit: number;
    verdict: 'ok' | 'degraded' | 'insufficient';
    warnings: Message[];
    recommendations: Message[];
  }

  const cap = createQuery<CapacityReport>({
    load: (signal) => apiGet<CapacityReport>('/api/egress/capacity', signal),
    tags: () => ['vpn']
  });

  const report = $derived(cap.data);
  const rows = $derived(
    report
      ? (['nordvpn', 'warp', 'proxies', 'direct'] as const)
          .map((name) => report.providers[name])
          .filter((provider): provider is ProviderCapacity => !!provider)
      : []
  );
  const showWeights = $derived(report?.strategy === 'balance');
  const showRequired = $derived(rows.some((provider) => provider.required !== null));
  const denom = $derived(Math.max(report?.target_slots ?? 6, report?.total_usable ?? 0, 1));
  const segments = $derived(
    rows
      .filter((provider) => provider.selected && provider.usable > 0)
      .map((provider) => ({
        ...provider,
        widthPct: (provider.usable / denom) * 100
      }))
  );
  const markerPct = $derived(((report?.target_slots ?? 6) / denom) * 100);

  let targetDraft = $state(6);
  let policyDraft = $state('full');
  let initialized = $state(false);
  let saving = $state(false);

  $effect(() => {
    if (!report || initialized) return;
    targetDraft = report.target_slots;
    policyDraft = report.requested_policy;
    initialized = true;
  });

  async function saveCapacity(): Promise<void> {
    const target = Math.max(1, Math.min(10, Number(targetDraft) || 6));
    targetDraft = target;
    saving = true;
    try {
      const res = await apiPost<{ ok?: boolean; error?: string }>(
        '/api/egress/capacity',
        { target_slots: target, reserve_policy: policyDraft }
      );
      if (res?.error) throw new Error(res.error);
      await cap.refresh(true);
      invalidateTag('vpn');
      showToast(t('free_models.capacity_saved'), 'success');
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : t('free_models.capacity_save_error'),
        'error'
      );
    } finally {
      saving = false;
    }
  }

  const PROVIDER_KEYS: Record<ProviderName, string> = {
    nordvpn: 'free_models.mode_vpn',
    warp: 'free_models.mode_socks5',
    proxies: 'free_models.mode_proxies',
    direct: 'free_models.mode_direct'
  };

  function providerLabel(name: ProviderName): string {
    return t(PROVIDER_KEYS[name] ?? '');
  }

  function verdictLabel(verdict?: string): string {
    if (verdict === 'ok') return t('free_models.capacity_verdict_ok');
    if (verdict === 'degraded') return t('free_models.capacity_verdict_degraded');
    return t('free_models.capacity_verdict_insufficient');
  }

  function statusLabel(status: string): string {
    if (status === 'ok') return t('free_models.capacity_verdict_ok');
    if (status === 'deficit') return t('free_models.capacity_deficit');
    if (status === 'unavailable') return t('free_models.capacity_unavailable');
    return t('free_models.capacity_unselected');
  }

  function pct(value: number | null): string {
    return value === null ? '—' : `${value.toFixed(1)} %`;
  }

  function messageText(message: Message): string {
    return lang.current === 'fr' ? message.fr : message.en;
  }
</script>

<section class="capacity" aria-label={t('free_models.capacity_title')}>
  <header class="capacity-head">
    <div>
      <span class="capacity-title">{t('free_models.capacity_title')}</span>
      <span class="capacity-hint">{t('free_models.capacity_hint')}</span>
    </div>
    {#if report}
      <span class="verdict verdict--{report.verdict}">{verdictLabel(report.verdict)}</span>
    {/if}
  </header>

  {#if report}
    <div class="capacity-controls">
      <label>
        <span>{t('free_models.capacity_target')}</span>
        <input
          type="number"
          min="1"
          max="10"
          step="1"
          value={targetDraft}
          oninput={(event) => (targetDraft = Number(event.currentTarget.value))}
        />
      </label>
      <label>
        <span>{t('free_models.capacity_policy')}</span>
        <select
          value={policyDraft}
          onchange={(event) => (policyDraft = event.currentTarget.value)}
        >
          <option value="full">{t('free_models.capacity_policy_full')}</option>
          <option value="shared">{t('free_models.capacity_policy_shared')}</option>
          <option value="proportional">
            {t('free_models.capacity_policy_proportional')}
          </option>
        </select>
      </label>
      <button
        type="button"
        class="btn btn--sm"
        disabled={saving}
        onclick={saveCapacity}
      >{t('free_models.capacity_apply')}</button>
    </div>
  {/if}

  {#if cap.loading && !report}
    <p class="muted">{t('free_models.capacity_loading')}</p>
  {:else if cap.error}
    <p class="error">{t('free_models.capacity_error')}</p>
  {:else if report}
    <div class="capacity-summary">
      <span>
        <strong>{report.total_usable}</strong> / {report.target_slots}
        {t('free_models.capacity_stations')}
      </span>
      <span class="target">
        {t('free_models.capacity_required')} : {report.total_required}
        {t('free_models.capacity_endpoints')}
      </span>
      <span>
        {t('free_models.capacity_observed_requests')} : {report.observed_requests}
      </span>
      {#if report.deficit > 0}
        <span class="deficit">
          {t('free_models.capacity_deficit')} : {report.deficit}
        </span>
      {/if}
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>{t('free_models.capacity_provider')}</th>
            <th>{t('free_models.capacity_configured')}</th>
            <th>{t('free_models.capacity_enabled')}</th>
            <th>{t('free_models.capacity_usable')}</th>
            <th>{t('free_models.capacity_observed')}</th>
            {#if showWeights}
              <th>{t('free_models.capacity_weight')}</th>
              <th>{t('free_models.capacity_per_endpoint')}</th>
            {/if}
            {#if showRequired}
              <th>{t('free_models.capacity_required')}</th>
            {/if}
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each rows as provider (provider.name)}
            <tr class:unselected={!provider.selected}>
              <td>{providerLabel(provider.name)}</td>
              <td>{provider.configured}</td>
              <td>{provider.enabled}</td>
              <td class:zero={provider.selected && provider.usable === 0}>
                {provider.usable}
              </td>
              <td>
                {#if provider.observed_share === null}
                  —
                {:else}
                  {provider.observed_share.toFixed(1)} %
                  <span class="observed-count">({provider.observed})</span>
                {/if}
              </td>
              {#if showWeights}
                <td>
                  {#if provider.weight === null}
                    —
                  {:else if provider.weight === 0}
                    <span class="standby">{t('free_models.standby')}</span>
                  {:else}
                    {provider.weight} %
                  {/if}
                </td>
                <td>{pct(provider.per_endpoint)}</td>
              {/if}
              {#if showRequired}
                <td>
                  {provider.required ?? '—'}
                  {#if provider.deficit > 0}
                    <span class="deficit">−{provider.deficit}</span>
                  {/if}
                </td>
              {/if}
              <td class="status status--{provider.status}">
                {statusLabel(provider.status)}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <div class="endpoint-bar">
      <div class="endpoint-track">
        {#each segments as segment (segment.name)}
          <span
            class="endpoint-seg endpoint-seg--{segment.name}"
            style={`width: ${segment.widthPct}%`}
            title={`${providerLabel(segment.name)} : ${segment.usable}`}
          ></span>
        {/each}
        <span class="endpoint-marker" style={`left: ${markerPct}%`}></span>
      </div>
      <div class="endpoint-scale">
        <span>0</span>
        <span>{report.target_slots} {t('free_models.capacity_stations')}</span>
        <span>{denom} {t('free_models.capacity_endpoints')}</span>
      </div>
    </div>

    <p class="direct-note">{t('free_models.capacity_direct_note')}</p>

    {#if report.recommendations.length > 0}
      <div class="recommendations">
        <span class="sub-title">{t('free_models.capacity_recommendations')}</span>
        {#each report.recommendations.slice(0, 2) as recommendation (recommendation.code)}
          <p>{messageText(recommendation)}</p>
        {/each}
      </div>
    {/if}

    {#if report.warnings.length > 0}
      <div class="warnings">
        <span class="sub-title">{t('free_models.capacity_warnings')}</span>
        {#each report.warnings.slice(0, 4) as warning (warning.code)}
          <p class="warning warning--{warning.severity}">{messageText(warning)}</p>
        {/each}
      </div>
    {/if}
  {/if}
</section>

<style>
  .capacity {
    margin-top: var(--sp-3);
    padding: var(--sp-3);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
  }

  .capacity-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--sp-2);
    flex-wrap: wrap;
  }

  .capacity-title {
    font-weight: 600;
    font-size: var(--fs-sm);
    color: var(--text);
    margin-right: var(--sp-2);
  }

  .capacity-hint {
    color: var(--text-dim);
    font-size: var(--fs-xs);
  }

  .verdict {
    padding: 2px 8px;
    border: 1px solid var(--border);
    border-radius: 999px;
    font-size: var(--fs-xs);
    font-weight: 600;
    white-space: nowrap;
  }

  .verdict--ok {
    color: var(--success);
    border-color: var(--success);
  }

  .verdict--degraded {
    color: var(--warn);
    border-color: var(--warn);
  }

  .verdict--insufficient {
    color: var(--danger);
    border-color: var(--danger);
  }

  .capacity-summary {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    flex-wrap: wrap;
    margin-top: var(--sp-2);
    font-size: var(--fs-xs);
    color: var(--text-dim);
  }

  .capacity-controls {
    display: flex;
    align-items: flex-end;
    gap: var(--sp-2);
    flex-wrap: wrap;
    margin-top: var(--sp-2);
  }

  .capacity-controls label {
    display: flex;
    flex-direction: column;
    gap: 2px;
    color: var(--text-dim);
    font-size: var(--fs-xs);
  }

  .capacity-controls input,
  .capacity-controls select {
    min-height: 28px;
    padding: 2px 6px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--surface-2);
    color: var(--text);
    font-size: var(--fs-sm);
  }

  .capacity-controls input {
    width: 4.5rem;
  }

  .capacity-summary strong {
    color: var(--text);
    font-size: var(--fs-sm);
  }

  .deficit {
    color: var(--danger);
    font-weight: 600;
  }

  .table-wrap {
    margin-top: var(--sp-2);
    overflow-x: auto;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--fs-xs);
  }

  th,
  td {
    padding: 4px 6px;
    border-bottom: 1px solid var(--border);
    text-align: right;
    white-space: nowrap;
  }

  th:first-child,
  td:first-child {
    text-align: left;
  }

  th {
    color: var(--text-dim);
    font-weight: 500;
  }

  tr.unselected {
    opacity: 0.5;
  }

  td.zero {
    color: var(--danger);
    font-weight: 600;
  }

  .observed-count {
    color: var(--text-dim);
    font-size: 0.9em;
    margin-left: 2px;
  }

  .standby {
    color: var(--text-dim);
    font-style: italic;
  }

  .status {
    font-weight: 600;
  }

  .status--ok {
    color: var(--success);
  }

  .status--deficit {
    color: var(--warn);
  }

  .status--unavailable {
    color: var(--danger);
  }

  .status--disabled {
    color: var(--text-dim);
  }

  .endpoint-bar {
    margin-top: var(--sp-3);
  }

  .endpoint-track {
    position: relative;
    display: flex;
    height: 12px;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--surface-2);
  }

  .endpoint-seg {
    height: 100%;
  }

  .endpoint-seg--nordvpn {
    background: #4f8cff;
  }

  .endpoint-seg--warp {
    background: #f5a623;
  }

  .endpoint-seg--proxies {
    background: #2ec4b6;
  }

  .endpoint-seg--direct {
    background: #9aa0a6;
  }

  .endpoint-marker {
    position: absolute;
    top: -2px;
    bottom: -2px;
    width: 2px;
    background: var(--text);
    opacity: 0.65;
  }

  .endpoint-scale {
    display: flex;
    justify-content: space-between;
    gap: var(--sp-2);
    margin-top: 2px;
    color: var(--text-dim);
    font-size: var(--fs-xs);
  }

  .direct-note {
    margin: var(--sp-2) 0 0;
    color: var(--text-dim);
    font-size: var(--fs-xs);
  }

  .recommendations,
  .warnings {
    margin-top: var(--sp-2);
  }

  .sub-title {
    color: var(--text-dim);
    font-size: var(--fs-xs);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .recommendations p,
  .warnings p {
    margin: 2px 0 0;
    font-size: var(--fs-xs);
    line-height: 1.45;
  }

  .warning--danger {
    color: var(--danger);
  }

  .warning--warn {
    color: var(--warn);
  }

  .muted {
    margin: var(--sp-2) 0 0;
    color: var(--text-dim);
    font-size: var(--fs-xs);
  }

  .error {
    margin: var(--sp-2) 0 0;
    color: var(--danger);
    font-size: var(--fs-xs);
  }
</style>
