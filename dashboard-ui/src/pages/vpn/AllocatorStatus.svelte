<script lang="ts">
  /**
   * Carte « Réparation des stations » : chaînes actives, dernier test,
   * winner, claims 429 et compte à rebours de reset. Lecture seule.
   */

  import { apiGet } from '$lib/api';
  import { createQuery } from '$lib/query.svelte';
  import Card from '$lib/components/Card.svelte';

  interface Winner {
    host?: string;
    ip?: string;
    model?: string;
  }

  interface Chain {
    station: number;
    pid?: number;
    running?: boolean;
    age_s?: number;
    tested?: number;
    last_status?: string | number | null;
    winner?: Winner | null;
  }

  interface AllocatorStatus {
    generated_at?: string;
    enabled?: boolean;
    allocator?: {
      mode?: string;
      max_parallel_repairs?: number;
      rebind_limit_per_min?: number;
      pause?: {
        paused_until?: number;
        reason?: string | null;
        recent_429?: number;
      };
      chains?: Chain[];
      claims?: {
        exists?: boolean;
        catalog?: number;
        claims?: Record<string, number>;
        next_reset_at?: number | null;
        next_reset_in_s?: number | null;
      };
    };
    db?: {
      assignments_open?: number;
      open_assignments?: Array<{ station: number; egress_ip: string; hostname?: string }>;
    };
  }

  const statusQ = createQuery<AllocatorStatus>({
    load: (signal) => apiGet<AllocatorStatus>('/api/free/allocator/status', signal),
    tags: () => ['vpn'],
    initial: {}
  });

  const data = $derived(statusQ.data ?? {});
  const alloc = $derived(data.allocator ?? {});
  const chains = $derived(alloc.chains ?? []);
  const claims = $derived(alloc.claims ?? {});
  const assignments = $derived(data.db?.open_assignments ?? []);
  const pauseRemaining = $derived.by(() => {
    const until = Number(alloc.pause?.paused_until ?? 0);
    return until > 0 ? Math.max(0, until - Date.now() / 1000) : 0;
  });
  const nextReset = $derived(Number(claims.next_reset_in_s ?? 0));

  function fmtDuration(seconds: number): string {
    if (!seconds || seconds <= 0) return '—';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return h > 0 ? `${h} h ${m} min` : `${m} min`;
  }

  function statusLabel(value: string | number | null | undefined): string {
    if (value === null || value === undefined || value === '') return '—';
    return String(value);
  }
</script>

<Card title="Réparation des stations">
  <div class="alloc-head">
    <span class="badge" class:on={data.enabled}>{data.enabled ? 'registre actif' : 'registre inactif'}</span>
    <span class="badge">{alloc.mode ?? 'off'}</span>
    <span class="badge">{alloc.max_parallel_repairs ?? 0} chaînes max</span>
    <span class="badge">{alloc.rebind_limit_per_min ?? 0} rebinds/min</span>
    {#if pauseRemaining > 0}
      <span class="badge warn">pause {fmtDuration(pauseRemaining)} — {alloc.pause?.reason ?? '429'}</span>
    {/if}
    {#if nextReset > 0}
      <span class="badge">prochain reset 429 : {fmtDuration(nextReset)}</span>
    {/if}
  </div>

  {#if chains.length === 0}
    <p class="hint">Aucune chaîne active. Le superviseur répare les stations dès qu’elles ne sont pas fonctionnelles.</p>
  {:else}
    <table>
      <thead>
        <tr>
          <th>station</th>
          <th>chaîne</th>
          <th>testés</th>
          <th>dernier</th>
          <th>winner</th>
        </tr>
      </thead>
      <tbody>
        {#each chains as chain (chain.station)}
          <tr>
            <td>s{chain.station}</td>
            <td>{chain.running ? 'en cours' : 'arrêtée'}</td>
            <td>{chain.tested ?? 0}</td>
            <td>{statusLabel(chain.last_status)}</td>
            <td>
              {#if chain.winner?.host}
                <strong>{chain.winner.host}</strong>
                {#if chain.winner.ip}<span class="muted"> → {chain.winner.ip}</span>{/if}
                {#if chain.winner.model}<div class="muted">{chain.winner.model}</div>{/if}
              {:else}
                <span class="muted">—</span>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  <div class="alloc-foot">
    <span>catalogue : {claims.catalog ?? 0}</span>
    <span>429 : {claims.claims?.['429'] ?? 0}</span>
    <span>bind_error : {claims.claims?.['bind_error'] ?? 0}</span>
    <span>assignations : {data.db?.assignments_open ?? 0}</span>
  </div>

  {#if assignments.length > 0}
    <div class="assignments">
      {#each assignments as item (item.station)}
        <span class="assignment">s{item.station} · {item.egress_ip}{item.hostname ? ` · ${item.hostname}` : ''}</span>
      {/each}
    </div>
  {/if}
</Card>

<style>
  .alloc-head,
  .alloc-foot,
  .assignments {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
  }
  .alloc-head {
    margin-bottom: 0.75rem;
  }
  .badge {
    border: 1px solid var(--border, #333);
    border-radius: 999px;
    padding: 0.15rem 0.55rem;
    font-size: 0.78rem;
    color: var(--muted, #999);
  }
  .badge.on {
    color: var(--ok, #38bdf8);
    border-color: currentColor;
  }
  .badge.warn {
    color: var(--warn, #f59e0b);
    border-color: currentColor;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.85rem;
  }
  th,
  td {
    text-align: left;
    padding: 0.35rem 0.5rem;
    border-bottom: 1px solid var(--border, #2a2a2a);
  }
  th {
    color: var(--muted, #999);
    font-weight: 500;
  }
  .muted {
    color: var(--muted, #999);
    font-size: 0.8rem;
  }
  .alloc-foot {
    margin-top: 0.75rem;
    font-size: 0.8rem;
    color: var(--muted, #999);
  }
  .assignments {
    margin-top: 0.5rem;
  }
  .assignment {
    font-size: 0.78rem;
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 0.2rem 0.45rem;
  }
  .hint {
    color: var(--muted, #999);
    font-size: 0.85rem;
  }
</style>
