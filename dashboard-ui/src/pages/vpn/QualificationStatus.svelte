<script lang="ts">
  /**
   * Qualification des IP NordVPN : couverture mesurée, raisons de skip,
   * époques de balayage et pause/reprise d'un candidat.
   */

  import { apiGet, apiPost } from '$lib/api';
  import { createQuery } from '$lib/query.svelte';
  import Card from '$lib/components/Card.svelte';

  interface Summary {
    enabled?: boolean;
    total?: number;
    tested?: number;
    tests?: number;
    usable?: number;
    quota?: number;
    pending?: number;
    dead?: number;
    skipped?: number;
    infra?: number;
    paused?: number;
    dedicated?: number;
    denylisted?: number;
    unsupported_transport?: number;
    by_status?: Record<string, number>;
    by_skipped_reason?: Record<string, number>;
    last_attempt_at?: number | null;
  }

  interface CountryCoverage {
    country: string;
    total: number;
    usable: number;
    pending: number;
    quota: number;
    dead: number;
    skipped: number;
  }

  interface TransportCoverage {
    transport: string;
    total: number;
    supported: number;
  }

  interface Coverage {
    countries?: CountryCoverage[];
    transports?: TransportCoverage[];
  }

  interface Epoch {
    epoch_id: string;
    status: string;
    total: number;
    tested: number;
    usable: number;
    quota: number;
    infra: number;
    dead: number;
    skipped: number;
    started_at: number;
    finished_at: number | null;
    duration_s: number | null;
    reason: string;
  }

  interface Candidate {
    hostname: string;
    country: string;
    city: string;
    load: number;
    status: string;
    transports: string[];
    potential_score: number;
    measured_score: number;
    rtt_ms: number | null;
    quota_remaining: number | null;
    attempts: number;
    last_attempt_at: number | null;
    last_result: string;
    last_egress_ip: string;
    last_transport: string;
    skipped_reason: string;
    next_eligible_at: number;
  }

  interface Queue {
    items?: Candidate[];
    total?: number;
  }

  interface Slo {
    enabled?: boolean;
    target_pct?: number;
    stations_total?: number;
    stations_usable?: number;
    fleet_ratio_pct?: number;
    six_of_six?: boolean;
    availability_1h_pct?: number;
    error_budget_pct?: number;
    heartbeat_at?: number | null;
    heartbeat_status?: string;
    heartbeat_age_s?: number | null;
    open_incidents?: number;
    oldest_incident_age_s?: number;
    repairs_finished?: number;
    mttr_s?: number | null;
  }

  interface TransportHealth {
    transport: string;
    total: number;
    supported: number;
    attempts: number;
    successes: number;
    success_rate: number;
    quota: number;
    infra: number;
    avg_rtt_ms: number | null;
    latency_p50_ms: number | null;
    health: string;
    degraded: boolean;
    last_error: string;
    last_attempt_at: number | null;
  }

  interface Transports {
    items?: TransportHealth[];
    openvpn_diagnostics?: TransportHealth[];
  }

  interface QualifierSlot {
    station: number;
    state: string;
    running: boolean;
    last_error: string;
    last_epoch: string;
    last_claimed: number;
    last_promoted: number;
    last_finished_at: number | null;
    last_duration_s: number | null;
  }

  interface Qualifiers {
    enabled?: boolean;
    count?: number;
    desired_count?: number;
    max_count?: number;
    recommended_max?: number;
    station_base?: number;
    transport?: string;
    max_hosts_per_epoch?: number;
    interval_s?: number;
    manager?: boolean;
    warning?: string;
    slots?: QualifierSlot[];
  }

  let statusFilter = $state('');
  let selectedEpoch = $state('');
  let busyHost = $state('');
  let actionError = $state('');
  let launchMode = $state('auto');
  let launchStation = $state(1);
  let launchMax = $state(100);
  let launching = $state(false);
  let launchInfo = $state('');
  let launchError = $state('');
  let qualifierEnabled = $state(false);
  let qualifierCount = $state(0);
  let qualifierBusy = $state(false);
  let qualifierError = $state('');
  let qualifierInfo = $state('');

  const summaryQ = createQuery<Summary>({
    load: (signal) => apiGet<Summary>('/api/egress/qualification/summary', signal),
    tags: () => ['qualification'],
    initial: {}
  });

  const coverageQ = createQuery<Coverage>({
    load: (signal) => apiGet<Coverage>('/api/egress/qualification/coverage', signal),
    tags: () => ['qualification'],
    initial: {}
  });

  const epochsQ = createQuery<{ items?: Epoch[] }>({
    load: (signal) =>
      apiGet<{ items?: Epoch[] }>('/api/egress/qualification/epochs?limit=20', signal),
    tags: () => ['qualification'],
    initial: { items: [] }
  });

  const queueQ = createQuery<Queue>({
    load: (signal) =>
      apiGet<Queue>(
        `/api/egress/qualification/queue?limit=50${statusFilter ? `&status=${statusFilter}` : ''}`,
        signal
      ),
    tags: () => ['qualification'],
    initial: { items: [] }
  });

  const epochQ = createQuery<Epoch>({
    load: (signal) =>
      apiGet<Epoch>(
        `/api/egress/qualification/epochs/${encodeURIComponent(selectedEpoch)}`,
        signal
      ),
    tags: () => ['qualification'],
    enabled: () => false
  });

  const sloQ = createQuery<Slo>({
    load: (signal) =>
      apiGet<Slo>('/api/egress/qualification/slo?target_pct=99', signal),
    tags: () => ['qualification'],
    initial: {}
  });

  const transportsQ = createQuery<Transports>({
    load: (signal) =>
      apiGet<Transports>('/api/egress/qualification/transports', signal),
    tags: () => ['qualification'],
    initial: { items: [] }
  });

  const qualifiersQ = createQuery<Qualifiers>({
    load: (signal) =>
      apiGet<Qualifiers>('/api/egress/qualification/qualifiers', signal),
    tags: () => ['qualification'],
    initial: {}
  });

  const summary = $derived(summaryQ.data ?? {});
  const coverage = $derived(coverageQ.data ?? {});
  const countries = $derived(coverage.countries ?? []);
  const transports = $derived(coverage.transports ?? []);
  const epochs = $derived(epochsQ.data?.items ?? []);
  const queue = $derived(queueQ.data?.items ?? []);
  const slo = $derived(sloQ.data ?? {});
  const transportHealth = $derived(transportsQ.data?.items ?? []);
  const qualifiers = $derived(qualifiersQ.data ?? {});
  const qualifierSlots = $derived(qualifiers.slots ?? []);
  const degradedTransports = $derived(
    transportHealth.filter((item) => item.degraded)
  );

  $effect(() => {
    if (qualifierBusy) return;
    qualifierEnabled = Boolean(qualifiers.enabled);
    qualifierCount = Number(qualifiers.count ?? 0);
  });

  const coveragePct = $derived.by(() => {
    const total = Number(summary.total ?? 0);
    return total > 0 ? Math.round((Number(summary.tested ?? 0) / total) * 1000) / 10 : 0;
  });

  async function reload(): Promise<void> {
    actionError = '';
    await Promise.all([
      summaryQ.refresh(true),
      coverageQ.refresh(true),
      epochsQ.refresh(true),
      queueQ.refresh(true),
      sloQ.refresh(true),
      transportsQ.refresh(true),
      qualifiersQ.refresh(true)
    ]);
  }

  async function filterByStatus(value: string): Promise<void> {
    statusFilter = value;
    await queueQ.refresh(true);
  }

  async function openEpoch(epochId: string): Promise<void> {
    selectedEpoch = epochId;
    await epochQ.refresh();
  }

  async function launchEpoch(): Promise<void> {
    launching = true;
    launchError = '';
    launchInfo = '';
    try {
      const payload = await apiPost<{ pid?: number; mode?: string }>(
        '/api/egress/qualification/epochs',
        { station: Number(launchStation), mode: launchMode, max_hosts: Number(launchMax) }
      );
      launchInfo = `époque lancée en ${payload.mode ?? launchMode} (pid ${payload.pid ?? '?'})`;
      await epochsQ.refresh(true);
    } catch (error) {
      launchError = error instanceof Error ? error.message : String(error);
    } finally {
      launching = false;
    }
  }

  async function applyQualifiers(): Promise<void> {
    qualifierBusy = true;
    qualifierError = '';
    qualifierInfo = '';
    try {
      const desired = qualifierEnabled
        ? Math.max(1, Number(qualifierCount) || 1)
        : Math.max(0, Number(qualifierCount) || 0);
      const payload = await apiPost<
        Qualifiers & { ok?: boolean; restart_required?: boolean }
      >('/api/egress/qualification/qualifiers', {
        enabled: qualifierEnabled,
        count: desired
      });
      qualifierCount = desired;
      qualifierInfo = payload.restart_required
        ? 'enregistré — redémarrage du proxy requis pour démarrer le manager'
        : `appliqué : ${fmtInt(payload.count)} qualifieur(s) actif(s)`;
      await qualifiersQ.refresh(true);
    } catch (error) {
      qualifierError = error instanceof Error ? error.message : String(error);
    } finally {
      qualifierBusy = false;
    }
  }

  async function stopQualifiers(): Promise<void> {
    qualifierEnabled = false;
    qualifierCount = 0;
    await applyQualifiers();
  }

  async function togglePause(candidate: Candidate): Promise<void> {
    const paused =
      candidate.status === 'skipped' && candidate.skipped_reason === 'operator-paused';
    busyHost = candidate.hostname;
    actionError = '';
    try {
      await apiPost(
        `/api/egress/qualification/candidates/${encodeURIComponent(candidate.hostname)}/${paused ? 'resume' : 'pause'}`,
        {}
      );
      await reload();
    } catch (error) {
      actionError = error instanceof Error ? error.message : String(error);
    } finally {
      busyHost = '';
    }
  }

  function isPaused(candidate: Candidate): boolean {
    return candidate.status === 'skipped' && candidate.skipped_reason === 'operator-paused';
  }

  function canPause(candidate: Candidate): boolean {
    return ['pending', 'quota', 'claimed'].includes(candidate.status) || isPaused(candidate);
  }

  function statusClass(value: string): string {
    if (value === 'usable') return 'ok';
    if (value === 'quota') return 'warn';
    if (value === 'dead' || value === 'skipped') return 'bad';
    return '';
  }

  function fmtInt(value: number | null | undefined): string {
    return new Intl.NumberFormat('fr-FR').format(Number(value ?? 0));
  }

  function fmtScore(value: number | null | undefined): string {
    return Number(value ?? 0).toFixed(1);
  }

  function fmtRtt(value: number | null | undefined): string {
    return value === null || value === undefined ? '—' : `${Math.round(Number(value))} ms`;
  }

  function fmtDuration(seconds: number | null | undefined): string {
    if (seconds === null || seconds === undefined) return '—';
    const total = Math.max(0, Math.round(Number(seconds)));
    if (total < 60) return `${total} s`;
    const minutes = Math.floor(total / 60);
    if (minutes < 60) return `${minutes} min ${total % 60} s`;
    return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
  }

  function fmtWhen(epochSeconds: number | null | undefined): string {
    if (!epochSeconds) return '—';
    return new Date(Number(epochSeconds) * 1000).toLocaleString('fr-FR');
  }

  function fmtAge(epochSeconds: number | null | undefined): string {
    if (!epochSeconds) return 'jamais testé';
    const seconds = Math.max(0, Date.now() / 1000 - Number(epochSeconds));
    return `il y a ${fmtDuration(seconds)}`;
  }
</script>

<Card title="Qualification des IP NordVPN">
  <div class="head">
    <span class="badge" class:on={summary.enabled}>
      {summary.enabled ? 'registre actif' : 'registre absent'}
    </span>
    <span class="badge">{fmtInt(summary.total)} hosts</span>
    <span class="badge">testés {fmtInt(summary.tested)} · {coveragePct} %</span>
    <span class="badge">{fmtInt(summary.tests)} tentatives</span>
    <span class="badge ok">{fmtInt(summary.usable)} utilisables</span>
    <span class="badge warn">{fmtInt(summary.quota)} quota</span>
    <span class="badge">{fmtInt(summary.pending)} en attente</span>
    <span class="badge bad">{fmtInt(summary.dead)} morts</span>
    <span class="badge">{fmtInt(summary.skipped)} skippés</span>
    {#if summary.paused}
      <span class="badge warn">{fmtInt(summary.paused)} en pause opérateur</span>
    {/if}
    {#if summary.infra}
      <span class="badge bad">{fmtInt(summary.infra)} erreurs infra</span>
    {/if}
    <button type="button" class="ghost" onclick={() => void reload()}>Rafraîchir</button>
    <a
      class="ghost-link"
      href="/api/egress/qualification/attempts?format=csv"
      target="_blank"
      rel="noreferrer">Export CSV</a
    >
    <a
      class="ghost-link"
      href="/api/egress/qualification/export"
      target="_blank"
      rel="noreferrer">Export JSON</a
    >
  </div>

  <p class="hint">
    Toutes les IP ne sont pas testées : les dédiées, les transports non supportés et
    la denylist sont écartés avant le balayage. Les compteurs ci-dessous sont mesurés
    dans le registre, pas estimés.
  </p>

  <div class="grid">
    <div class="panel">
      <h3>Raisons d’exclusion</h3>
      {#if Object.keys(summary.by_skipped_reason ?? {}).length === 0}
        <p class="muted">Aucune exclusion enregistrée.</p>
      {:else}
        <table>
          <thead>
            <tr><th>raison</th><th>hosts</th></tr>
          </thead>
          <tbody>
            {#each Object.entries(summary.by_skipped_reason ?? {}) as [reason, count] (reason)}
              <tr><td>{reason}</td><td class="mono">{fmtInt(Number(count))}</td></tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>

    <div class="panel">
      <h3>Transports</h3>
      {#if transports.length === 0}
        <p class="muted">Aucun transport catalogué.</p>
      {:else}
        <table>
          <thead>
            <tr><th>transport</th><th>total</th><th>supportés</th></tr>
          </thead>
          <tbody>
            {#each transports as item (item.transport)}
              <tr>
                <td>{item.transport}</td>
                <td class="mono">{fmtInt(item.total)}</td>
                <td class="mono">{fmtInt(item.supported)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>
  </div>

  <h3>Couverture par pays</h3>
  {#if countries.length === 0}
    <p class="muted">Aucune couverture par pays enregistrée.</p>
  {:else}
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>pays</th><th>total</th><th>utilisables</th><th>en attente</th>
            <th>quota</th><th>morts</th><th>skippés</th>
          </tr>
        </thead>
        <tbody>
          {#each countries as country (country.country)}
            <tr>
              <td>{country.country}</td>
              <td class="mono">{fmtInt(country.total)}</td>
              <td class="mono">{fmtInt(country.usable)}</td>
              <td class="mono">{fmtInt(country.pending)}</td>
              <td class="mono">{fmtInt(country.quota)}</td>
              <td class="mono">{fmtInt(country.dead)}</td>
              <td class="mono">{fmtInt(country.skipped)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  <h3>Époques de balayage</h3>
  <div class="launch">
    <label>
      station
      <input type="number" min="1" max="10" bind:value={launchStation} />
    </label>
    <label>
      mode
      <select bind:value={launchMode}>
        <option value="auto">auto (config)</option>
        <option value="shadow">shadow</option>
        <option value="live">live</option>
      </select>
    </label>
    <label>
      max hosts
      <input type="number" min="0" max="10000" bind:value={launchMax} />
    </label>
    <button type="button" class="launch-btn" disabled={launching} onclick={() => void launchEpoch()}>
      {launching ? 'Lancement…' : 'Lancer une époque'}
    </button>
    {#if launchInfo}<span class="badge ok">{launchInfo}</span>{/if}
  </div>
  {#if launchError}
    <p class="error">Lancement refusé : {launchError}</p>
  {/if}
  {#if epochs.length === 0}
    <p class="muted">Aucune époque enregistrée.</p>
  {:else}
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>époque</th><th>statut</th><th>testés</th><th>utilisables</th>
            <th>quota</th><th>morts</th><th>durée</th><th>démarrée</th><th></th>
          </tr>
        </thead>
        <tbody>
          {#each epochs as epoch (epoch.epoch_id)}
            <tr class:selected={selectedEpoch === epoch.epoch_id}>
              <td class="mono">{epoch.epoch_id.slice(0, 12)}</td>
              <td>{epoch.status}</td>
              <td class="mono">{fmtInt(epoch.tested)}/{fmtInt(epoch.total)}</td>
              <td class="mono">{fmtInt(epoch.usable)}</td>
              <td class="mono">{fmtInt(epoch.quota)}</td>
              <td class="mono">{fmtInt(epoch.dead)}</td>
              <td>{fmtDuration(epoch.duration_s)}</td>
              <td>{fmtWhen(epoch.started_at)}</td>
              <td>
                <button type="button" class="ghost" onclick={() => void openEpoch(epoch.epoch_id)}>
                  Détail
                </button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  {#if selectedEpoch}
    <div class="epoch-detail">
      <div class="head">
        <h3>Époque {selectedEpoch.slice(0, 12)}</h3>
        <button type="button" class="ghost" onclick={() => (selectedEpoch = '')}>Fermer</button>
      </div>
      {#if epochQ.error}
        <p class="error">{epochQ.error.message}</p>
      {:else if epochQ.data}
        <div class="head">
          <span class="badge">{epochQ.data.status}</span>
          <span class="badge">{fmtInt(epochQ.data.tested)}/{fmtInt(epochQ.data.total)} testés</span>
          <span class="badge ok">{fmtInt(epochQ.data.usable)} utilisables</span>
          <span class="badge warn">{fmtInt(epochQ.data.quota)} quota</span>
          <span class="badge bad">{fmtInt(epochQ.data.infra)} infra</span>
          <span class="badge bad">{fmtInt(epochQ.data.dead)} morts</span>
          <span class="badge">{fmtInt(epochQ.data.skipped)} skippés</span>
          <span class="badge">{fmtDuration(epochQ.data.duration_s)}</span>
        </div>
        {#if epochQ.data.reason}
          <p class="muted">raison : {epochQ.data.reason}</p>
        {/if}
      {:else}
        <p class="muted">Chargement…</p>
      {/if}
    </div>
  {/if}

  <div class="queue-head">
    <h3>File de qualification</h3>
    <label>
      statut
      <select value={statusFilter} onchange={(event) => void filterByStatus(event.currentTarget.value)}>
        <option value="">tous</option>
        <option value="pending">en attente</option>
        <option value="quota">quota</option>
        <option value="usable">utilisable</option>
        <option value="dead">mort</option>
        <option value="skipped">skippé</option>
      </select>
    </label>
    <span class="muted">{fmtInt(queueQ.data?.total)} candidats</span>
  </div>

  {#if actionError}
    <p class="error">Action impossible : {actionError}</p>
  {/if}

  {#if queue.length === 0}
    <p class="muted">Aucun candidat pour ce filtre.</p>
  {:else}
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>host</th><th>pays</th><th>statut</th><th>score</th><th>RTT</th>
            <th>quota</th><th>transports</th><th>essais</th><th>dernier test</th>
            <th>dernier</th><th></th>
          </tr>
        </thead>
        <tbody>
          {#each queue as candidate (candidate.hostname)}
            <tr>
              <td class="mono">{candidate.hostname}</td>
              <td>{candidate.country}{candidate.city ? ` · ${candidate.city}` : ''}</td>
              <td>
                <span class="badge {statusClass(candidate.status)}">{candidate.status}</span>
                {#if isPaused(candidate)}
                  <span class="badge warn">pause</span>
                {:else if candidate.skipped_reason}
                  <span class="muted">{candidate.skipped_reason}</span>
                {/if}
              </td>
              <td class="mono">{fmtScore(candidate.measured_score || candidate.potential_score)}</td>
              <td class="mono">{fmtRtt(candidate.rtt_ms)}</td>
              <td class="mono">{candidate.quota_remaining === null ? '—' : fmtInt(candidate.quota_remaining)}</td>
              <td class="mono">{candidate.transports?.join(', ') || '—'}</td>
              <td class="mono">{fmtInt(candidate.attempts)}</td>
              <td class="muted">{fmtAge(candidate.last_attempt_at)}</td>
              <td class="muted">
                {candidate.last_result || '—'}
                {#if candidate.last_egress_ip}<div class="mono">{candidate.last_egress_ip}</div>{/if}
              </td>
              <td>
                {#if canPause(candidate)}
                  <button
                    type="button"
                    class="ghost"
                    disabled={busyHost === candidate.hostname}
                    onclick={() => void togglePause(candidate)}
                  >
                    {isPaused(candidate) ? 'Reprendre' : 'Pause'}
                  </button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  {#if summary.last_attempt_at}
    <p class="muted">Dernière tentative : {fmtWhen(summary.last_attempt_at)}</p>
  {/if}
</Card>

<Card title="Qualifieurs dédiés">
  <div class="head">
    <span class="badge" class:ok={qualifiers.enabled && Number(qualifiers.count ?? 0) > 0}>
      {qualifiers.manager ? 'manager actif' : 'manager absent (redémarrage requis)'}
    </span>
    <span class="badge">{fmtInt(qualifiers.count)} / {fmtInt(qualifiers.max_count ?? 4)} qualifieur(s)</span>
    <span class="badge">transport {qualifiers.transport ?? 'wireguard'}</span>
    <span class="badge">
      stations {qualifiers.station_base ?? 7}–{(qualifiers.station_base ?? 7) + (qualifiers.max_count ?? 4) - 1}
    </span>
    <span class="badge">{fmtInt(qualifiers.max_hosts_per_epoch ?? 40)} hosts/époque</span>
    <span class="badge">boucle {fmtDuration(qualifiers.interval_s ?? 30)}</span>
  </div>
  <p class="hint">
    Ces slots utilisent les services Compose 7–10 (profil multi-station) en WireGuard et ne
    touchent jamais aux six stations de production. Un slot qualifie environ 220–280 hosts
    par heure, deux 440–560 ; au-delà de deux, les sessions NordVPN supplémentaires peuvent
    provoquer des AUTH_FAILED sur les stations de production.
  </p>
  <div class="launch">
    <label class="inline">
      <input type="checkbox" bind:checked={qualifierEnabled} /> activer
    </label>
    <label>
      nombre
      <input
        type="number"
        min="0"
        max={qualifiers.max_count ?? 4}
        bind:value={qualifierCount}
      />
    </label>
    <button
      type="button"
      class="launch-btn"
      disabled={qualifierBusy}
      onclick={() => void applyQualifiers()}
    >
      {qualifierBusy ? 'Application…' : 'Appliquer'}
    </button>
    <button
      type="button"
      class="ghost"
      disabled={qualifierBusy}
      onclick={() => void stopQualifiers()}
    >
      Tout arrêter
    </button>
    {#if qualifierInfo}<span class="badge ok">{qualifierInfo}</span>{/if}
  </div>
  {#if qualifierError}
    <p class="error">Application refusée : {qualifierError}</p>
  {/if}
  {#if qualifiers.warning}
    <p class="error">{qualifiers.warning}</p>
  {/if}
  {#if qualifierSlots.length === 0}
    <p class="muted">Aucun slot actif : le qualifieur dédié est arrêté.</p>
  {:else}
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>station</th><th>état</th><th>dernière époque</th><th>testés</th>
            <th>promus</th><th>durée</th><th>fin</th><th>erreur</th>
          </tr>
        </thead>
        <tbody>
          {#each qualifierSlots as slot (slot.station)}
            <tr>
              <td class="mono">{slot.station}</td>
              <td>
                <span
                  class="badge"
                  class:ok={slot.state === 'running'}
                  class:warn={slot.state === 'starting'}
                  class:bad={slot.state === 'error'}
                >{slot.state}</span>
              </td>
              <td class="mono">{slot.last_epoch || '—'}</td>
              <td class="mono">{fmtInt(slot.last_claimed)}</td>
              <td class="mono">{fmtInt(slot.last_promoted)}</td>
              <td>{fmtDuration(slot.last_duration_s)}</td>
              <td>{fmtWhen(slot.last_finished_at)}</td>
              <td class="muted">{slot.last_error || '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</Card>

<Card title="SLO flotte">
  <div class="head">
    <span class="badge" class:ok={slo.six_of_six}>
      {fmtInt(slo.stations_usable)}/{fmtInt(slo.stations_total)} stations
    </span>
    <span class="badge" class:warn={!slo.six_of_six}>6/6 : {slo.six_of_six ? 'oui' : 'non'}</span>
    <span class="badge">cible {slo.target_pct ?? 99} %</span>
    <span class="badge" class:ok={(slo.availability_1h_pct ?? 0) >= (slo.target_pct ?? 99)}>
      dispo 1 h {slo.availability_1h_pct ?? 0} %
    </span>
    <span class="badge" class:warn={(slo.error_budget_pct ?? 0) <= 0}>
      error budget {slo.error_budget_pct ?? 0} %
    </span>
    {#if slo.heartbeat_status}
      <span class="badge">heartbeat {slo.heartbeat_status}</span>
    {/if}
    {#if slo.heartbeat_age_s !== null && slo.heartbeat_age_s !== undefined}
      <span class="badge">âge heartbeat {fmtDuration(slo.heartbeat_age_s)}</span>
    {/if}
    {#if slo.open_incidents}
      <span class="badge bad">{fmtInt(slo.open_incidents)} incident(s)</span>
    {/if}
    {#if slo.mttr_s !== null && slo.mttr_s !== undefined}
      <span class="badge">MTTR {fmtDuration(slo.mttr_s)}</span>
    {/if}
    {#if slo.repairs_finished}
      <span class="badge">{fmtInt(slo.repairs_finished)} réparations</span>
    {/if}
    {#if degradedTransports.length}
      <span class="badge bad">{fmtInt(degradedTransports.length)} transport(s) dégradé(s)</span>
    {/if}
  </div>
  {#if transportHealth.length}
    <h3>Santé des transports</h3>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>transport</th><th>santé</th><th>essais</th><th>succès</th>
            <th>taux</th><th>quota</th><th>infra</th><th>RTT p50</th>
            <th>dernier test</th><th>erreur</th>
          </tr>
        </thead>
        <tbody>
          {#each transportHealth as item (item.transport)}
            <tr>
              <td>{item.transport}</td>
              <td>
                <span
                  class="badge"
                  class:ok={item.health === 'healthy'}
                  class:warn={item.health === 'quota' || item.health === 'unproven'}
                  class:bad={item.degraded || item.health === 'infra' || item.health === 'failing'}
                >{item.health}</span>
              </td>
              <td class="mono">{fmtInt(item.attempts)}</td>
              <td class="mono">{fmtInt(item.successes)}</td>
              <td class="mono">{(Number(item.success_rate ?? 0) * 100).toFixed(1)} %</td>
              <td class="mono">{fmtInt(item.quota)}</td>
              <td class="mono">{fmtInt(item.infra)}</td>
              <td class="mono">{fmtRtt(item.latency_p50_ms ?? item.avg_rtt_ms)}</td>
              <td class="muted">{fmtAge(item.last_attempt_at)}</td>
              <td class="muted">{item.last_error || '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
  {#if !slo.enabled}
    <p class="muted">Aucune station ni heartbeat enregistré pour le moment.</p>
  {/if}
</Card>

<style>
  .head,
  .queue-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
  }
  .head {
    margin-bottom: 0.65rem;
  }
  .badge {
    border: 1px solid var(--border, #333);
    border-radius: 999px;
    padding: 0.15rem 0.55rem;
    font-size: 0.78rem;
    color: var(--muted, #999);
    white-space: nowrap;
  }
  .badge.on,
  .badge.ok {
    color: var(--ok, #38bdf8);
    border-color: currentColor;
  }
  .badge.warn {
    color: var(--warn, #f59e0b);
    border-color: currentColor;
  }
  .badge.bad {
    color: var(--danger, #ef4444);
    border-color: currentColor;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 0.75rem;
    margin-bottom: 0.75rem;
  }
  .panel {
    border: 1px solid var(--border, #2a2a2a);
    border-radius: 8px;
    padding: 0.6rem 0.75rem;
  }
  h3 {
    font-size: 0.95rem;
    margin: 0.9rem 0 0.45rem;
  }
  .panel h3 {
    margin-top: 0;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82rem;
  }
  th,
  td {
    text-align: left;
    padding: 0.32rem 0.45rem;
    border-bottom: 1px solid var(--border, #2a2a2a);
  }
  th {
    color: var(--muted, #999);
    font-weight: 500;
    white-space: nowrap;
  }
  tr.selected {
    background: color-mix(in srgb, var(--accent, #38bdf8) 10%, transparent);
  }
  .scroll {
    max-height: 22rem;
    overflow: auto;
  }
  .mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .muted,
  .hint {
    color: var(--muted, #999);
    font-size: 0.82rem;
  }
  .hint {
    max-width: 70rem;
  }
  .error {
    color: var(--danger, #ef4444);
    font-size: 0.85rem;
  }
  .queue-head {
    justify-content: space-between;
    margin-top: 1rem;
  }
  .launch {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
    margin-bottom: 0.55rem;
  }
  .launch input {
    width: 4rem;
  }
  .launch label.inline {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    cursor: pointer;
  }
  .launch label.inline input {
    width: auto;
    margin-left: 0;
  }
  .launch input,
  .launch select {
    margin-left: 0.35rem;
    background: var(--surface, transparent);
    color: inherit;
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 0.15rem 0.35rem;
  }
  button.launch-btn {
    border: 1px solid var(--accent, #38bdf8);
    border-radius: 6px;
    background: transparent;
    color: var(--accent, #38bdf8);
    padding: 0.2rem 0.6rem;
    font-size: 0.8rem;
    cursor: pointer;
  }
  button.launch-btn:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .queue-head h3 {
    margin: 0;
  }
  .epoch-detail {
    border: 1px solid var(--border, #2a2a2a);
    border-radius: 8px;
    padding: 0.65rem 0.8rem;
    margin-top: 0.75rem;
  }
  .epoch-detail h3 {
    margin: 0;
  }
  button.ghost {
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    background: transparent;
    color: inherit;
    padding: 0.15rem 0.5rem;
    font-size: 0.78rem;
    cursor: pointer;
  }
  button.ghost:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .ghost-link {
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    color: inherit;
    padding: 0.15rem 0.5rem;
    font-size: 0.78rem;
    text-decoration: none;
  }
  select {
    margin-left: 0.35rem;
    background: var(--surface, transparent);
    color: inherit;
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 0.15rem 0.35rem;
  }
</style>
