# OpenCode-Proxy

A Python proxy for using [OpenCode Go](https://opencode.ai/docs/go/) subscription with [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Features a real-time web dashboard with token stats, subscription quota tracking, and full configuration management.

## Requirements

- Python 3.11+

## Installation

```bash
pip install -r requirements.txt
```

### GUI Mode (default)

The proxy launches with a system tray icon and dashboard window **by default**. Required dependencies:

```bash
pip install pystray Pillow pywebview
```

If these are missing, the proxy falls back to terminal mode with a warning. To force terminal mode:

```bash
python opencode.py --no-gui
```

`--gui` is still accepted for backward compatibility.

## Configuration

Copy `.env.example` to `.env` and edit:

```bash
cp .env.example .env
```

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENCODE_API_KEY` | API key for OpenCode | — |
| `OPENCODE_PROXY` | HTTP proxy server | — |
| `OPUS_MAP_MODEL` | Model for opus route | `kimi-k2.6` |
| `SONNET_MAP_MODEL` | Model for sonnet route | `glm-5.1` |
| `HAIKU_MAP_MODEL` | Model for haiku route | `minimax-m2.5` |
| `OPENCODE_HOST` | Bind address | `0.0.0.0` |
| `OPENCODE_PORT` | API + dashboard port | `4000` |
| `API_KEY_ROUTING` | Multi-key strategy: `round-robin` or `failover` | `round-robin` |
| `DISABLE_MAPPING` | Pass model names through unchanged (skip mapping) | `false` |
| `PROXY_LANG` | Dashboard UI language (`en` \| `fr`) | `en` |
| `DASHBOARD_TOKEN` | Auth token for sensitive API endpoints (`X-Dashboard-Token` header) | — (open) |

### OpenCode Go Quotas

To enable subscription quota tracking in the dashboard:

| Variable | Description |
|----------|-------------|
| `OPENCODE_GO_WORKSPACE_ID` | Your workspace ID (`wrk_...`) |
| `OPENCODE_GO_AUTH_COOKIE` | Auth cookie from opencode.ai (`Fe26.2...`) |

You can also configure these directly from the dashboard Configuration tab.

## Claude Code Configuration

```bash
export ANTHROPIC_API_KEY="fake-key"
export ANTHROPIC_AUTH_TOKEN="fake"
export ANTHROPIC_BASE_URL="http://localhost:4000"
```

Or in your `.env`:

```env
ANTHROPIC_API_KEY=fake-key
ANTHROPIC_AUTH_TOKEN=fake
ANTHROPIC_BASE_URL=http://localhost:4000
```

**Note:** The proxy must be running (`python opencode.py`) before using Claude Code.

## Running

```bash
python opencode.py
```

Server starts (API + dashboard on the same port):
- **API & Web Dashboard**: http://localhost:4000

A mono-instance file lock (`logs/opencode.lock`) makes a second `python opencode.py` exit immediately with `FATAL: another opencode-proxy instance is already running` instead of corrupting state.

## VPN & IP Rotation (Free Model Quota)

The proxy multiplies free model quotas by rotating the egress IP through a [gluetun](https://github.com/qdm12/gluetun-wiki) container (declared in `docker-compose.yml`, managed automatically by the proxy). Each egress IP gets its own **official OpenCode client identity**: the free wire replays the real client (`opencode/<version> … runtime/bun/…` UA + per-IP `project`/`ses_`/`msg_`), and the TLS layer replays the Bun fingerprint. Identity profiles no longer describe a browser (no `sec-ch-*`/`Accept-Language`): `impersonate` is only the `curl_cffi` TLS carrier. No paid-account artifact (API key, client UA, cookies) ever reaches the free endpoint.

### Setup

1. **Docker** with the compose plugin — the proxy starts/stops/updates the tunnel itself via `docker compose`.
2. **NordVPN credentials** in `credentials.env` at the project root (the compose service's `env_file` — keep it out of git). The dashboard VPN & IP tab writes the same file via `POST /api/vpn-config`.
3. **Configuration** in `config.yaml` (also editable from the dashboard, VPN & IP tab):

```yaml
ip_rotation:
  enabled: true
  proxy_mode: vpn
  quota_per_ip: 300              # requests per IP before proactive rotation
  switch_delay: 5                # seconds between rotation steps
  docker_container: opencode-vpn # gluetun container name
  docker_compose_file: docker-compose.yml
  vpn_proxy_port: 8888           # gluetun HTTP proxy
  socks5_proxy_port: 1080        # gluetun SOCKS5 proxy
  credentials_file: vpn_configs/credentials.txt
  server_countries: Germany      # NordVPN region for the tunnel
  circuit_breaker_threshold: 3   # rotation failures before the circuit opens
  circuit_breaker_recovery: 300
  backoff_max_delay: 60
  ip_check_url: https://api.ipify.org
  identity_rotation: true        # rotate the official client face with each IP change
  identity_diversity: true       # expand to the official client-name grid (cli/desktop)
  identity_profiles:             # official-client faces; UA derived from `client`
    - client: cli                # opencode/prod/2.0.28/cli
    - client: desktop            # opencode/prod/2.0.28/desktop
                                 # optional: impersonate: <curl_cffi carrier> (TLS only)
  update_enabled: true           # auto-apply gluetun image updates
  update_check_interval: 21600   # check every 6 h
  update_apply_window: 03:00-05:00
  update_apply_idle_minutes: 15  # apply only after this much idle time
  rotation_wait_timeout: 5       # max s a request waits for a fresh IP before paid fallback
```

The compose service runs with `FIREWALL=on`: the container only talks through the VPN tunnel — no accidental leaks. The default config lists both official clients (`cli`/`desktop`); per-IP version/project/session diversity comes from the identity vault.

### How it works

- **Free model requests** go through the gluetun tunnel (HTTP proxy `:8888` / SOCKS5 `:1080`); **paid requests** always go direct, never through the VPN
- Free quota exhausted (HTTP 429) or `quota_per_ip` reached → the proxy switches to a new NordVPN server and records a per-(model, IP) cooldown, so the retry lands on a fresh address
- Each successful rotation advances the official client face (UA v2 + Bun TLS) while the per-IP vault issues a fresh `project`/`ses_`/`msg_` set
- VPN down → free requests fail over to a direct connection (fail-open, by design) until the tunnel recovers
- gluetun image updates are checked periodically and applied automatically during the configured idle window

### Station quality & fleet operations (2026-10-26)

- **Quality selector** (`stations.quality.enabled`): each eligible station gets
  a measured score (latency, success rate, quota headroom, server load,
  stability). A light active probe (static GET via SOCKS5, no inference/quota)
  feeds cold-start scores; the pool's legacy round-robin remains the rollback.
- **Health guard**: secondary-path OpenVPN resets on a WireGuard station no
  longer flap the status; a flap hysteresis and a bounded churn window are
  configurable under `stations.health`.
- **Fleet API**: `GET /api/vpn/fleet`, `POST /api/vpn/fleet/{connect,disconnect}`,
  `POST /api/vpn/station/{sid}/{start,stop,restart,connect,disconnect}`,
  `POST /api/vpn/jobs/fleet/{connect,disconnect,heal}` (202 + job id).
- **Per-station action buttons** are exposed in the VPN tab.
- **Secret migration**: `scripts/migrate_control_key.py` moves
  `control_api_key` from `config.yaml` to `.env`; restart the proxy after it.
- **Persistence**: quota counters are flushed every 30 s and on shutdown, not
  only on rotations.
- Detailed audit/roadmap: `docs/AUDIT_STATIONS_IP_2026-10-26.md`,
  `docs/ROADMAP_STATIONS_IP_2026-10-26.md`, `docs/SUIVI_ROADMAP_STATIONS_2026-10-26.md`.

## Web Dashboard

The dashboard is a Svelte 5 + Vite single-page app (source in `dashboard-ui/`, built to `static/dist/`) with 7 tabs, real-time updates via SSE (Server-Sent Events), per-tab lazy loading, dark/light themes, and English/French translations.

**Frontend development**

```bash
cd dashboard-ui
npm install
npm run dev      # hot reload on http://localhost:5173/static/dist/ (API proxied to :4000)
npm run build    # production bundle → static/dist/
npm run check    # svelte-check (types + a11y), must be clean
python scripts/precompress.py   # gzip/brotli sidecars served by the proxy
```

### Token Stats
- Overview counters: Input, Output, Cache, Total, Success, Failed, Avg Duration
- Charts: Token Distribution (donut), Token % by Model, Requests % by Model
- Detailed breakdown table by model

### Logs (Request History)
- Full request logs: time, original/model, tokens, thinking, effort, duration, status
- Filter: Today, 7 Days, 30 Days, Custom date range
- Error details: HTTP status codes with explanations (520, 524, etc.) on hover
- Pagination
- Delete history (all or before date)

### Quotas
- OpenCode Go usage bars: 5-hour rolling, weekly, monthly
- Live countdown timers for reset time
- Color-coded progress bars (green < 60%, orange 60–85%, red > 85%)
- Auto-refreshes every 5 minutes + instant update on change

### Free Models — auto-discovery

The proxy auto-discovers every `*-free` model announced on [Zen docs](https://opencode.ai/docs/fr/zen/) without any manual `config.yaml` edit.

- **Live at startup**: `GET https://opencode.ai/zen/v1/models` + `GET https://opencode.ai/zen/go/v1/models` (union, dedup), filtered by cascade `pricing {input:0,output:0}` → `is_free`/`free`/`capabilities.free` → suffix `-free`. Fallback filet: scrape docs HTML `(?i)<td>*-free</td>` only if the API returns nothing.
- **Instantly routable**: each new free id is added to `MODELS` (`{endpoint, protocol}`) — `muse-*`/`spark-*` → `https://opencode.ai/zen/v1/responses`, the rest → `free_base` — and, when a paid model has a `paid → paid-free` homonym (e.g. `mimo-v2.5 → mimo-v2.5-free`), a `free_model_map` entry is created automatically.
- **Pool fallback** (no blind remap): payants without homonym stay unmapped; they fall back to `FREE_MODEL_POOL = sorted(FREE_MODELS)` on `429`/`max_free_attempts` (round-robin, family-preferential). `custom_routes` are never mutated.
- **Durable + audit**: add-only merge, delta-check (`if new_set == FREE_MODELS: no-op` — no `mtime` bump), atomic `tmp+fsync+os.replace` under `_reload_lock`; removed upstream ids are never deleted, just logged (`[free-discovery] upstream removed … — keeping local`) and exposed in `GET /api/free-models → {removed}`.
- **Lifecycle**: fire-and-forget at boot (no boot delay if Zen is down) + background loop `GET /models` every `free_discovery.interval` s (default 3600 s, `±10 %` jitter; after 3 consecutive failures `min(7200, interval×2)` backoff). On-demand `POST /api/free-discovery/refresh` (singleflight lock, guarded by `control_api_key` when `control_enabled`, `429` if >1/min).
- **Observability**: `GET /api/free-models → {detected, mapped, pool, removed, last_refresh, next_refresh, source, consecutive_failures}` + dashboard badge + event `free_models_updated`. **5 free models covered with no manual step**: `mimo-v2.5-free`, `nemotron-3-ultra-free`, `nemotron-3.5-lightning-free`, `muse-spark-1.2-contributor-free`, `muse-spark-1.3-contributor-free` — `mimo-v2.5-free` is the live `free_model_map` target for the `minimax-*`/`qwen*` payants (strict rotation); the others are discovered and instantly usable (e.g. `model: mimo-v2.5-free`).

### Free vs paid — one canonical classification

Every model now carries a server-computed tier, so a client never has to guess
from the `-free` suffix:

```bash
curl -s 'http://localhost:4000/v1/models?tier=free' | jq '.counts, .data[].id'
curl -s 'http://localhost:4000/v1/models?tier=paid&free_first=true' | jq '.data[].id'
```

- `tier` = `free` | `paid` | `unknown` — billing classification. `free` requires an explicit signal: a `-free` suffix, a verified free id (`free_verified`, e.g. `big-pickle`), or membership in the verified `FREE_MODELS` registry. **Being listed on the anonymous free leg is not proof**: the live catalogue now sends new free-leg ids to the automatic free-only probe first. Only `alive`/`limited` (and `not_probeable` for an id already free by suffix/mapping) are promoted; `gpt-5.5`, `claude-opus-5-5`, … stay `paid` with `tier_source=unproven`.
- **Auto-probe des nouveaux ids** : le relevé vivant ne bloque jamais ; il planifie `_free_probe_new_candidates` en arrière-plan. La sonde utilise la jambe free + `Bearer public` (aucun repli payant), mémorise les refus et persiste les promotions dans [`config/free_models_probed.json`](config/free_models_probed.json). Un modèle free sans suffixe est donc testé et promu automatiquement dès sa détection. Un lot dont **tous** les ids répondent `429` est traité comme ambigu (quota global/transitoire), pas comme une preuve de gratuité : verdict `unknown` et re-test après `free_discovery.probe.transient_429_retry_s` (300 s par défaut), sauf si un modèle free de contrôle répond 2xx (le `429` est alors ciblé et reste `limited`).
- `tier_source` = `route` | `suffix` | `discovery` | `mapped` | `endpoint` | `unproven` | `catalog` | `unknown` — why the tier was chosen. `route` is resolved first: a `ROUTES`/custom alias is classified as its real target; `route_target` names that target. If the target is free, the alias stays **paid** with `free_target` (it is a free substitute, exactly like an active mapping), e.g. `glm-5.1 → glm-5.3-flash-free` is `paid` + `free_target`.
- `route_leg` = `free` | `paid` | `internal` | `unknown` — the leg actually used by the proxy, independent from billing.
- `free_first` = the proxy will try a free substitute first. A paid model with an active `free_model_map` entry stays `tier=paid`, `free_first=true`.
- `free_target` / `free_mapping` / `free_mapping_active` expose the substitution. Under the default `homonym` policy, `mimo-v2.5`'s mapping is **ignored**; `family` activates it. `glm-5.1`'s old mapping is also ignored, but its route alias supplies a free target.
- `route_mismatch` / `catalog_mismatch` are warnings, not filters: the first means billing and real route disagree; the second means the live catalogue and the route disagree (informational). `unproven` candidates do not raise `route_mismatch`.
- `proxy.probe` expose le dernier verdict free-leg connu (`alive`, `limited`, `not_free`, `dead`, `unknown`, `not_probeable`) et son statut HTTP, quand la sonde automatique a testé l’id ou sa cible.

`GET /api/config` exposes the same fields per model plus `model_counts`;
`GET /api/free-models` adds `counts` (`detected`, `listed`, `candidates`,
`routed`, `served`, `unproven`, `cooldown`, …) and a `status` per free model.
“Detected” is the verified registry, “listed” is the strict free list, and
“routed” does not mean “proven healthy”. On the local instance the live
catalogue has 56 free-leg ids, the probe rejects the 32 paid Go ids, and the
strict free list is 10.

Per-call headers (best-effort, set at response start):

| Header | Meaning |
|---|---|
| `X-OpenCode-Proxy-Tier` | actual tier of the leg that served the response |
| `X-OpenCode-Proxy-Requested-Tier` | tier of the model the client asked for |
| `X-OpenCode-Proxy-Model` | actual upstream model id |
| `X-OpenCode-Proxy-Requested-Model` | requested model id |
| `X-OpenCode-Proxy-Fallback` | `paid_to_free` or `free_to_paid` when a substitution happened |

A fallback that happens mid-stream cannot rewrite headers already sent; use
`GET /v1/models` for the authoritative per-model classification.

Config keys (all optional, defaults shown):

```yaml
free_discovery:
  enabled: true
  interval: 3600
  auto_persist: true
  default_target: mimo-v2.5-free
upstream:
  free_models_url: ''   # override GET /models URL; empty = auto (free_base + openai_base)
```

### Live model catalog (availability + effort)

`live_models` keeps the client-facing catalog aligned with what the two
opencode legs actually serve, without editing `config.yaml`:

- **Every `live_models.interval` s** (default 300), the proxy fetches both
  `GET /models` endpoints. New ids are added to `MODELS`; ids that disappeared
  or are `deprecated` on their routed leg are removed. A model that is
  `deprecated` on one leg but active on the other is migrated, not dropped.
  A partial fetch (one leg down) never deletes anything.
- **Effort capabilities are derived from `reasoning_options`** in
  `models.opencode.ai/api.json` (refreshed every `catalog_interval` s, default
  1800). `GET /v1/models` exposes the Anthropic Models API `capabilities`
  object: `effort.max.supported` is `true` only when the model really lists
  `max`; a model capped at `high` reports `max: {supported: false}`. The same
  catalog data drives request clamping (`config/effort_caps.py` and
  `config/reasoning_options.py`), so the announced and enforced levels match.
- **Clients that used a removed model** receive an explicit `410` when they
  retry it, and a `X-OpenCode-Model-Deprecated` / `X-OpenCode-Notice` header
  on their next API request. Notices are stored in memory for 7 days.
- **Dashboard pop-up**: the `models_updated` SSE event (and the `revision`
  returned by `GET /api/live-models`) triggers a toast when models are added,
  removed or deprecated — including changes that happened while the dashboard
  was closed.

```yaml
live_models:
  enabled: true
  interval: 300          # /models polling
  catalog_interval: 1800 # models.opencode.ai/api.json polling
  remove_deprecated: true
  notify_clients: true
  ignore_ids: [test]
```

`POST /api/live-models/refresh` forces an immediate full refresh (dashboard
token protected like the other mutation endpoints).

### Configuration
- **Proxy Status**: Running/stopped indicator, start/stop buttons, click-to-copy localhost and LAN addresses
- **Model Mapping**: Map opus/sonnet/haiku to any available backend model, with pass-through toggle
- **Custom Mapping**: Add keyword-based custom routes (e.g., "nimo" → model)
- **Available Models**: Auto-discovered from upstream at startup, with capability badges (Chat, Vision, Tools, Code) and per-model request limits (5h/weekly/monthly)
- **Server Settings**: API port, HTTP proxy, API key, Go workspace ID, Go auth cookie
- **Instant apply**: All changes take effect immediately — no restart required (hot-restart for port changes)

### Common Features
- Dark/Light theme (persisted in localStorage)
- Real-time updates via SSE (falls back to polling)
- Time filter shared across Stats and Logs tabs

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/messages` | Proxy Anthropic format → backend |
| POST | `/anthropic/v1/messages` | Proxy Anthropic format |
| POST | `/v1/messages/count_tokens` | Estimate token count |
| POST | `/v1/chat/completions` | Proxy OpenAI chat format → backend |
| POST | `/v1/responses` | Proxy OpenAI Responses format → backend |
| POST | `/v1/systemone` | Passthrough TypeSafe SystemOne (Jev) — `{model, state, questions}` → `zen/v1/systemone`. `jev-1.13` = clé Zen du pool, `jev-1.13-free` = anonyme gratuit. PAS un endpoint de chat (pas de `messages`, pas de tools) |
| GET | `/health` | Health check |
| GET | `/v1/models` | List available models |
| GET | `/api/stats` | Token stats (supports `from_date`, `to_date`) |
| GET | `/api/stats/timeseries` | Token stats time series |
| GET | `/api/history` | Request history (pagination, date filter) |
| GET | `/api/history/filters` | History filter values |
| GET | `/api/requests/{req_id}` | Single request detail |
| DELETE | `/api/history` | Delete history (`before` or `all=true`) |
| GET | `/api/logs` | Terminal logs |
| GET | `/api/events` | SSE event stream (real-time updates) |
| GET | `/api/quotas` | OpenCode Go quota usage |
| GET | `/api/free-models` | Free-model discovery snapshot `{detected,mapped,pool,removed,last_refresh,next_refresh,source,consecutive_failures}` |
| POST | `/api/free-discovery/refresh` | Force a discovery refresh (singleflight, rate-limited) |
| GET | `/api/live-models` | Live catalog snapshot `{revision,available,deprecated,added,removed,effort,events,notices}` |
| POST | `/api/live-models/refresh` | Force a full live-catalog refresh (singleflight) |
| GET | `/api/free-model-usage` | Free model request counters |
| GET | `/api/config` | Full proxy configuration |
| POST | `/api/config` | Update configuration at runtime |
| GET | `/api/config/custom-routes` | List custom routes |
| POST | `/api/config/custom-routes` | Save custom routes |
| GET | `/api/config/api-keys` | List API keys |
| POST | `/api/config/api-keys` | Save API keys |
| GET | `/api/config/tool-capabilities` | Tool capabilities config |
| POST | `/api/config/tool-capabilities` | Update tool capabilities |
| GET | `/api/config/web-search` | Web search config |
| POST | `/api/config/web-search` | Update web search config |
| GET | `/api/proxy/status` | Proxy running status |
| POST | `/api/proxy/start` | Start the proxy |
| POST | `/api/proxy/stop` | Stop the proxy |
| POST | `/api/proxy/restart` | Restart the proxy |
| GET | `/api/tools` | Tool usage aggregated from request history |
| GET | `/api/debug` | Debug mode status |
| POST | `/api/debug` | Toggle debug mode |
| GET | `/api/debug/logs` | Download debug logs |
| DELETE | `/api/debug/logs` | Clear debug logs |
| GET | `/api/vpn-status` | VPN status (IP, server, identity profile, circuit breaker) |
| GET | `/api/vpn-config` | VPN & identity rotation configuration |
| POST | `/api/vpn-config` | Update VPN config / credentials (hot-reload + persist) |
| POST | `/api/vpn/toggle` | Enable/disable the VPN |
| POST | `/api/vpn/connect` | Connect the tunnel |
| POST | `/api/vpn/disconnect` | Disconnect the tunnel |
| POST | `/api/vpn/health-check` | Run a tunnel health check |
| POST | `/api/vpn/next` | Rotate to the next server (advances identity) |
| POST | `/api/vpn/update` | Check/apply a gluetun image update |
| GET | `/api/vpn/credentials` | Credentials status |
| POST | `/api/vpn/credentials` | Save credentials to `credentials.env` |
| POST | `/api/vpn/save-state` | Persist VPN state to disk |
| GET | `/api/vpn/export` | Export VPN state (backup) |
| POST | `/api/vpn/import` | Import VPN state (restore) |

When `DASHBOARD_TOKEN` is set, sensitive endpoints (`/api/config*`, `/api/vpn-config`, `/api/vpn/credentials`, `/api/vpn/import`, `/api/vpn/export`, `/api/requests/{req_id}`) require the `X-Dashboard-Token` header.

## Security

- **Bind address**: default `OPENCODE_HOST=0.0.0.0` exposes dashboard to LAN. Use `127.0.0.1` for local-only or set `DASHBOARD_TOKEN` (header `X-Dashboard-Token`) for any `0.0.0.0` deployment — the proxy warns at startup if `0.0.0.0` is open without a token.
- **Secrets**: `api_keys.json`, `.env`, `credentials.env` are gitignored and never returned raw (API returns `*_masked` only; `dashboard/api.py:912`, `1093`). Rotate `OPENCODE_GO_AUTH_COOKIE` regularly — it is a session token.
- **`.ovpn` files**: tracked `vpn/configs/*.ovpn` contain no inline credentials (`auth-user-pass` absent) — verified at audit.

## Keyboard shortcuts (Terminal)

- `j`/`↓`: Scroll down
- `k`/`↑`: Scroll up
- `g`: Go to top
- `G`: Go to bottom
- `Ctrl+C`: Exit

## Project Structure

```
opencode.py              # Main FastAPI server + ServerManager (hot-restart)
vpn_manager.py           # VPN lifecycle: connect/rotate/status, identity rotation
free_ip_pool.py          # Free-model quota tracking + IP pool state
docker_events.py         # docker events watcher → watchdog wake + SSE vpn_event
station_supervisor.py    # Per-station supervisor registry (N stations)
latency_rotation.py      # EWMA/p95 latency engine → soft/hard rotations
ip_latency.py            # Per-IP latency probes shared across stations
free_discovery.py        # Upstream free-model discovery + auto-persist
shared_rotation.py       # Shared rotation state (anti-réutilisation d'IP)
shared_state.py          # Registres globaux (managers, pool, watcher)
traffic_capture.py       # Capture Wireshark-like (lazy, on-demand)
trust.py                 # Client auth + dashboard trust middlewares
config/
  __init__.py            # Package exports
  settings.py            # Configuration, .env management, routes, geo
  live_models.py         # Live catalog: additions, retirements, effort caps
dashboard/
  __init__.py            # Package exports
  api.py                 # Dashboard API endpoints
  display.py             # Rich terminal display
  events.py              # SSE event manager (real-time push)
  quota.py               # OpenCode Go quota fetcher + model discovery
gui/
  __init__.py            # GUI package
  icon.py                # System tray icon
  tray.py                # System tray menu
  window.py              # WebView window
  _webview_main.py       # WebView entry point
dashboard-ui/            # Dashboard UI — Svelte 5 + Vite (source)
  src/pages/             # Stats, Logs, Quotas, FreeModels, Config, Traffic, Debug
  src/lib/               # api, i18n, thème, router, temps réel, composants
  package.json           # svelte, vite, uplot, svelte-check
static/
  dist/                  # Build servi par le proxy (index.html + assets/ précompressés)
scripts/                 # Outils : bench_perf, bench, migration DB, precompress, etc.
tests/                   # Suite pytest (~740 tests)
custom_routes.json       # Custom route mappings (persistent)
docker-compose.yml       # Proxy + gluetun VPN tunnel stack
credentials.env          # NordVPN credentials (gluetun env_file, gitignored)
requirements.txt         # Python dependencies
.env.example             # Template environment configuration
.env                     # Environment configuration (gitignored)
```

### Dashboard trust

Le dashboard est servi sur le port principal du proxy. Par défaut (`dashboard_trust.mode: lan`),
les réseaux loopback + RFC1918 sont considérés de confiance sans token ; les mutations exigent le
même-host (anti-CSRF) + rate-limit (`client_auth.mode`). Un token peut être requis via
`DASHBOARD_TOKEN` / `DASHBOARD_REQUIRE_TOKEN=true`. Pour une exposition au-delà du LAN, passer
`dashboard_trust.mode` en mode strict et exiger le token.

## License

[MIT](LICENSE)
