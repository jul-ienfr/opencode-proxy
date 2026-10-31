<script lang="ts">
  /**
   * Blocs « Configuration VPN », « Identifiants NordVPN » et « Serveurs VPN »
   * de la page Modèles Gratuits.
   *
   * `GET /api/vpn-config` est un objet plat : on le prend tel quel et on ne
   * renvoie que les clés réellement persistées (`vpn_proxy_port`, pas
   * `proxy_port` — l'ancien code envoyait une clé ignorée par le backend).
   */

  import { apiGet, apiPost } from '$lib/api';
  import { t } from '$lib/i18n.svelte';
  import { createQuery } from '$lib/query.svelte';
  import { showToast } from '$lib/toast.svelte';
  import Card from '$lib/components/Card.svelte';

  type VpnMode = 'auto' | 'wsl2' | 'docker' | 'native' | 'nordvpn-app';

  interface VpnServer {
    name?: string;
    config?: string;
  }

  interface VpnConfig {
    mode?: VpnMode;
    vpn_proxy_port?: number;
    switch_delay?: number;
    servers?: VpnServer[];
    [key: string]: unknown;
  }

  interface Credentials {
    exists?: boolean;
    username_preview?: string;
  }

  const MODES: VpnMode[] = ['auto', 'wsl2', 'docker', 'native', 'nordvpn-app'];

  const cfgQ = createQuery<VpnConfig>({
    load: (signal) => apiGet<VpnConfig>('/api/vpn-config', signal),
    tags: () => ['vpn'],
    initial: {}
  });

  const credQ = createQuery<Credentials>({
    load: (signal) => apiGet<Credentials>('/api/vpn/credentials', signal),
    tags: () => ['vpn'],
    initial: {}
  });

  const cfg = $derived(cfgQ.data);
  const servers = $derived(cfg?.servers ?? []);
  const creds = $derived(credQ.data ?? {});

  let busy = $state<string | null>(null);
  let username = $state('');
  let password = $state('');
  let serverName = $state('');
  let selectedFile: File | null = null;
  let selectedLabel = $state('');

  async function run(field: string, fn: () => Promise<unknown>, done?: () => void): Promise<void> {
    if (busy) return;
    busy = field;
    try {
      await fn();
      done?.();
      await cfgQ.refresh(true);
    } catch (e) {
      showToast(e instanceof Error ? e.message : String(e), 'error');
    } finally {
      busy = null;
    }
  }

  /** Persiste la configuration VPN (les champs avancés repartent de l'état lu). */
  function saveConfig(patch: Partial<VpnConfig>): void {
    void run('config', () =>
      apiPost('/api/vpn-config', {
        mode: cfg?.mode ?? 'auto',
        vpn_proxy_port: Number(cfg?.vpn_proxy_port) || 8888,
        switch_delay: Number(cfg?.switch_delay) || 5,
        ...patch
      })
    );
  }

  function saveCredentials(): void {
    if (!username || !password) {
      showToast(t('vpn.credentials_missing'), 'warning');
      return;
    }
    void run(
      'credentials',
      () => apiPost('/api/vpn-config', { credentials: { username, password } }),
      () => {
        username = '';
        password = '';
        showToast(t('vpn.credentials_saved'), 'success');
      }
    );
  }

  function connect(): void {
    void run('connect', () => apiPost('/api/vpn/connect'));
  }

  function nextIp(): void {
    void run('next', () => apiPost('/api/vpn/next'));
  }

  function disconnect(): void {
    void run('disconnect', () => apiPost('/api/vpn/disconnect'));
  }

  function onFileChange(e: Event): void {
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files?.[0] ?? null;
    selectedFile = file;
    selectedLabel = file ? file.name : '';
    if (!serverName && file) serverName = file.name.replace(/\.ovpn$/i, '');
  }

  function addServer(): void {
    if (!selectedFile) {
      showToast(t('vpn.select_ovpn'), 'warning');
      return;
    }
    void run(
      'add-server',
      async () => {
        const body = new FormData();
        body.append('name', serverName || selectedFile!.name);
        body.append('config', selectedFile!);
        const res = await apiPost<{ error?: string }>('/api/vpn/upload-config', body);
        if (res?.error) throw new Error(res.error);
      },
      () => {
        serverName = '';
        selectedFile = null;
        selectedLabel = '';
      }
    );
  }

  function removeServer(name: string): void {
    void run('remove-server', () => apiPost('/api/vpn-config', { remove_server: name }));
  }

  /** Infobulle du mode : WSL2 léger / Docker reproductible. */
  const modeHint = $derived(
    cfg?.mode === 'docker' ? t('vpn.mode_hint_docker') : t('vpn.mode_hint_wsl2')
  );
</script>

<Card title={t('vpn.config')}>
  <div class="actions">
    <button class="btn btn--primary" disabled={busy === 'connect'} onclick={connect}>
      {t('vpn.connect')}
    </button>
    <button class="btn" disabled={busy === 'next'} onclick={nextIp}>
      {t('vpn.switch_ip')}
    </button>
    <button class="btn btn--danger" disabled={busy === 'disconnect'} onclick={disconnect}>
      {t('vpn.disconnect')}
    </button>
  </div>

  <div class="form">
    <div class="row">
      <span class="label">{t('vpn.mode')}</span>
      <select
        class="select select--mode"
        value={cfg?.mode ?? 'auto'}
        disabled={busy === 'config'}
        onchange={(e) => saveConfig({ mode: e.currentTarget.value as VpnMode })}
      >
        {#each MODES as m (m)}
          <option value={m}>{t(`vpn.mode_${m}`)}</option>
        {/each}
      </select>
      <span class="muted small">{modeHint}</span>
    </div>

    <div class="row">
      <span class="label">{t('vpn.proxy_port')}</span>
      <input
        class="input input--narrow"
        type="number"
        min="1"
        max="65535"
        value={String(cfg?.vpn_proxy_port ?? 8888)}
        disabled={busy === 'config'}
        onchange={(e) => saveConfig({ vpn_proxy_port: Number(e.currentTarget.value) || 8888 })}
      />
    </div>

    <div class="row">
      <span class="label">{t('vpn.switch_delay')}</span>
      <input
        class="input input--narrow"
        type="number"
        min="0"
        value={String(cfg?.switch_delay ?? 5)}
        disabled={busy === 'config'}
        onchange={(e) => saveConfig({ switch_delay: Number(e.currentTarget.value) || 5 })}
      />
    </div>

    <p class="summary">{t('vpn.config_summary')}</p>
  </div>

  <div class="columns">
    <section>
      <h3 class="h3">{t('vpn.credentials')}</h3>
      <p class="hint">{t('vpn.credentials_hint')}</p>

      <p class="cred-status" class:ok={creds.exists} class:missing={!creds.exists}>
        {creds.exists ? `✔ ${t('vpn.credentials_saved')}` : `! ${t('vpn.credentials_missing')}`}
      </p>

      <div class="cred-box">
        <div class="row">
          <span class="label">Username</span>
          <span class="value mono">{creds.exists ? (creds.username_preview ?? '****') : '—'}</span>
        </div>
        <div class="row">
          <span class="label">Password</span>
          <span class="value mono">{creds.exists ? '••••••••' : '—'}</span>
        </div>
        <div class="row">
          <span class="label">{t('vpn.file')}</span>
          <span class="value small">{creds.exists ? 'credentials.env' : '—'}</span>
        </div>
      </div>

      <div class="cred-form">
        <input
          class="input"
          type="text"
          placeholder="Username"
          autocomplete="off"
          bind:value={username}
        />
        <input
          class="input"
          type="password"
          placeholder="Password"
          autocomplete="off"
          bind:value={password}
        />
        <button class="btn btn--primary" disabled={busy === 'credentials'} onclick={saveCredentials}>
          {t('vpn.save_credentials')}
        </button>
      </div>
    </section>

    <section>
      <h3 class="h3">{t('vpn.servers')}</h3>
      <p class="hint">{t('vpn.servers_hint')}</p>

      <div class="server-list">
        {#if servers.length === 0}
          <p class="muted small">{t('vpn.no_servers')}</p>
        {:else}
          {#each servers as srv, i (srv.name ?? i)}
            <div class="server">
              <div class="server-info">
                <strong>{srv.name}</strong>
                <span class="muted small">{(srv.config ?? '').split('/').pop() ?? ''}</span>
              </div>
              <button
                class="btn btn--danger btn--sm"
                aria-label={t('vpn.remove_server')}
                title={t('vpn.remove_server')}
                disabled={busy === 'remove-server'}
                onclick={() => removeServer(srv.name ?? '')}
              >
                ✕
              </button>
            </div>
          {/each}
        {/if}
      </div>

      <div class="cred-form">
        <input
          class="input input--name"
          type="text"
          placeholder={t('vpn.server_name_ph')}
          bind:value={serverName}
        />
        <label class="btn btn--ghost file-label">
          {t('vpn.browse')}
          <input type="file" accept=".ovpn" class="visually-hidden" onchange={onFileChange} />
        </label>
        <button class="btn btn--primary" disabled={busy === 'add-server'} onclick={addServer}>
          {t('vpn.add_server')}
        </button>
      </div>
      {#if selectedLabel}
        <p class="muted small file-name">{selectedLabel}</p>
      {/if}
    </section>
  </div>
</Card>

<style>
  .actions {
    display: flex;
    gap: var(--sp-2);
    flex-wrap: wrap;
    margin-bottom: var(--sp-4);
  }

  .form {
    padding: var(--sp-3);
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
  }

  .row {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    margin-bottom: var(--sp-3);
    flex-wrap: wrap;
  }

  .row:last-child {
    margin-bottom: 0;
  }

  .label {
    min-width: 130px;
    font-size: var(--fs-sm);
    color: var(--text-dim);
  }

  .select--mode {
    width: 160px;
  }

  .input--narrow {
    width: 100px;
  }

  .summary {
    margin: var(--sp-3) 0 0;
    padding-top: var(--sp-2);
    border-top: 1px solid var(--border);
    font-size: var(--fs-xs);
    color: var(--text-faint);
  }

  .columns {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: var(--sp-5);
    margin-top: var(--sp-5);
  }

  .h3 {
    margin: 0 0 var(--sp-1);
    font-size: var(--fs-md);
    color: var(--text);
  }

  .hint {
    margin: 0 0 var(--sp-3);
    font-size: var(--fs-xs);
    color: var(--text-faint);
  }

  .cred-status {
    margin: 0 0 var(--sp-2);
    font-size: var(--fs-sm);
    color: var(--text-faint);
  }

  .cred-status.ok {
    color: var(--success);
  }

  .cred-status.missing {
    color: var(--warn);
  }

  .cred-box {
    padding: var(--sp-2) var(--sp-3);
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    margin-bottom: var(--sp-3);
  }

  .value {
    font-family: var(--font-mono);
    font-size: var(--fs-sm);
    color: var(--text);
  }

  .cred-form {
    display: flex;
    gap: var(--sp-2);
    flex-wrap: wrap;
    align-items: center;
  }

  .input {
    flex: 1 1 120px;
    min-width: 110px;
  }

  .input--name {
    flex: 1 1 120px;
  }

  .file-label {
    cursor: pointer;
  }

  .file-name {
    margin: var(--sp-2) 0 0;
  }

  .server-list {
    max-height: 190px;
    overflow-y: auto;
    margin-bottom: var(--sp-3);
  }

  .server {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--sp-2);
    padding: var(--sp-1) var(--sp-2);
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    margin-bottom: var(--sp-1);
    font-size: var(--fs-sm);
  }

  .server-info {
    display: flex;
    align-items: baseline;
    gap: var(--sp-2);
    min-width: 0;
  }

  .server-info strong {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .muted {
    color: var(--text-faint);
  }

  .small {
    font-size: var(--fs-xs);
  }

  .mono {
    font-family: var(--font-mono);
  }
</style>
