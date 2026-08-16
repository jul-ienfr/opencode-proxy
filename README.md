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

The proxy multiplies free model quotas by rotating the egress IP through a [gluetun](https://github.com/qdm12/gluetun-wiki) container (declared in `docker-compose.yml`, managed automatically by the proxy). Each successful IP rotation also advances the **client identity** — a `curl_cffi` impersonation profile (TLS fingerprint, HTTP/2 parameters, `sec-ch-*` headers) + User-Agent + optional extra headers — so opencode.ai cannot correlate sessions across IPs. No paid-account artifact (API key, client UA, cookies) ever reaches the free endpoint.

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
  identity_rotation: true        # rotate client fingerprint with each IP change
  identity_profiles:             # curl_cffi impersonation targets (desktop browsers)
    - impersonate: chrome131
      user_agent: null           # null = curl_cffi bundle provides the matching UA
      extra_headers: {}
  update_enabled: true           # auto-apply gluetun image updates
  update_check_interval: 21600   # check every 6 h
  update_apply_window: 03:00-05:00
  update_apply_idle_minutes: 15  # apply only after this much idle time
```

The compose service runs with `FIREWALL=on`: the container only talks through the VPN tunnel — no accidental leaks. The default config uses a single `chrome131` profile, so behavior is unchanged until you add more profiles.

### How it works

- **Free model requests** go through the gluetun tunnel (HTTP proxy `:8888` / SOCKS5 `:1080`); **paid requests** always go direct, never through the VPN
- Free quota exhausted (HTTP 429) or `quota_per_ip` reached → the proxy switches to a new NordVPN server and records a per-(model, IP) cooldown, so the retry lands on a fresh address
- Each successful rotation advances the identity profile: a different TLS fingerprint + User-Agent on the new IP
- VPN down → free requests fail over to a direct connection (fail-open, by design) until the tunnel recovers
- gluetun image updates are checked periodically and applied automatically during the configured idle window

## Web Dashboard

The dashboard features 4 tabs with real-time updates via SSE (Server-Sent Events).

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
config/
  __init__.py            # Package exports
  settings.py            # Configuration, .env management, routes
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
static/
  index.html             # Dashboard UI (4 tabs)
  styles.css             # Theming (dark/light)
  app.js                 # Frontend logic
custom_routes.json       # Custom route mappings (persistent)
docker-compose.yml       # Proxy + gluetun VPN tunnel stack
credentials.env          # NordVPN credentials (gluetun env_file, gitignored)
requirements.txt         # Python dependencies
.env.example             # Template environment configuration
.env                     # Environment configuration (gitignored)
```

## License

[MIT](LICENSE)
