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
| `OPENCODE_PORT` | API port | `4000` |
| `OPENCODE_WEB_PORT` | Web UI port | `8082` |

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

Server starts:
- **API**: http://localhost:4000
- **Web Dashboard**: http://localhost:8082

## VPN & IP Rotation (Free Model Quota)

The proxy can rotate IP addresses via OpenVPN to multiply free model quotas.

### Setup

1. Install OpenVPN (included in Docker image, or `apt install openvpn` on Linux)
2. Download NordVPN `.ovpn` configs from https://nordvpn.com/servers/tools/
3. Configure in dashboard (VPN & IP tab) or in `config.yaml`:

```yaml
ip_rotation:
  enabled: true
  openvpn:
    servers:
      - name: NordVPN-FR
        config: /path/to/fr.nordvpn.com.udp.ovpn
    auth_file: /path/to/credentials.txt
    protocol: udp
  quota_per_ip: 300
```

4. Save NordVPN credentials in the dashboard (VPN & IP → Credentials)

### How it works

- Free model requests go through OpenVPN tunnel (different IP = different quota)
- Paid model requests go through direct IP (no VPN)
- When quota is exhausted (~300 requests), proxy auto-switches to next VPN server
- TLS fingerprint imitates Chrome via `curl_cffi` (anti-detection)

### GUI Mode

GUI is the default (system tray + dashboard window). For terminal mode:

```bash
python opencode.py --no-gui
```

`--gui` is still accepted for backward compatibility.

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
- **Server Settings**: API port, Web UI port, HTTP proxy, API key, Go workspace ID, Go auth cookie
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
| GET | `/api/history` | Request history (supports pagination, date filter) |
| DELETE | `/api/history` | Delete history (`before` or `all=true`) |
| GET | `/api/logs` | Terminal logs |
| GET | `/api/config` | Full proxy configuration |
| POST | `/api/config` | Update configuration at runtime |
| GET | `/api/config/custom-routes` | List custom routes |
| POST | `/api/config/custom-routes` | Save custom routes |
| GET | `/api/proxy/status` | Proxy running status |
| POST | `/api/proxy/start` | Start the proxy |
| POST | `/api/proxy/stop` | Stop the proxy |
| GET | `/api/events` | SSE event stream (real-time updates) |
| GET | `/api/quotas` | OpenCode Go quota usage |

## Keyboard shortcuts (Terminal)

- `j`/`↓`: Scroll down
- `k`/`↑`: Scroll up
- `g`: Go to top
- `G`: Go to bottom
- `Ctrl+C`: Exit

## Project Structure

```
opencode.py              # Main FastAPI server + ServerManager (hot-restart)
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
requirements.txt         # Python dependencies
.env.example             # Template environment configuration
.env                     # Environment configuration (gitignored)
```

## License

[MIT](LICENSE)
