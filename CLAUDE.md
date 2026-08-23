# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Application

```bash
# Install dependencies
pip install -r requirements.txt

# Run the proxy server (GUI by default: system tray + dashboard window)
# GUI deps: pip install pystray Pillow pywebview  (falls back to terminal mode if missing)
python opencode.py

# Force terminal mode (headless — used by systemd/Docker)
python opencode.py --no-gui
```

Server starts on:
- **API + Web Dashboard** (same app): http://localhost:4000

## Configuration

Configuration is managed through `.env` file (create from `.env.example`):

```bash
cp .env.example .env
```

Key environment variables:
- `OPENCODE_PROXY` - Proxy server URL
- `OPENCODE_API_KEY` - API key for OpenCode service
- `OPUS_MAP_MODEL` - Model for opus route (default: `kimi-k2.6`)
- `SONNET_MAP_MODEL` - Model for sonnet route (default: `glm-5.1`)
- `HAIKU_MAP_MODEL` - Model for haiku route (default: `minimax-m2.5`)

Optional server overrides:
- `OPENCODE_HOST` - Bind address (default: `0.0.0.0`)
- `OPENCODE_PORT` - API + dashboard port (default: `4000`)

## Deployment (Ubuntu Server)

### Option 1: systemd service

```bash
# Create user
sudo useradd -r -s /bin/false opencode

# Clone and setup
sudo mkdir -p /opt/opencode-proxy
sudo cp -r . /opt/opencode-proxy/
cd /opt/opencode-proxy
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env
# Edit .env with your config

# Install service
sudo cp opencode.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable opencode
sudo systemctl start opencode

# Check status
sudo systemctl status opencode
```

### Option 2: Docker

```bash
# Build
docker build -t opencode-proxy .

# Run (mount .env for config persistence)
docker run -d \
  --name opencode-proxy \
  -p 4000:4000 \
  -v $(pwd)/.env:/app/.env \
  -v opencode-logs:/app/logs \
  opencode-proxy
```

## Architecture

### Entry Point
- `opencode.py` - Main FastAPI server with request routing and token tracking

### Configuration Module (`config/`)
- `__init__.py` - Exports: `API_BASE_OPENAI`, `API_BASE_ANTHROPIC`, `HOST`, `PORT`, `WEB_PORT`, `MODELS`, `ROUTES`, `get_model_config`, `API_KEY`, `PROXY`
- `settings.py` - Loads environment variables from `.env`, defines:
  - `PROXY` and `API_KEY` (secrets)
  - `MODELS` (model endpoints and protocols)
  - `ROUTES` (model name mappings, built from env vars)
  - `get_model_config()` - Returns merged config for a model

### Dashboard Module (`dashboard/`)
- `__init__.py` - Exports: `register_dashboard`, `log`, `RichLogHandler`, `build_display`, `start_input_thread`
- `api.py` - Dashboard API endpoints: stats, logs, history, static file serving
- `display.py` - Rich terminal display: token usage table, log panel, keyboard input (j/k/g/G/arrows)

### Request Flow
1. Request comes to `/v1/messages` or `/anthropic/v1/messages`
2. `_route_for()` maps model name to route config using `ROUTES`
3. `get_model_config()` gets endpoint and protocol for the model
4. Request is forwarded to the appropriate endpoint (OpenAI or Anthropic protocol)
5. Response is converted between Anthropic and OpenAI formats
6. `/v1/messages/count_tokens` estimates token count without forwarding

### Token Tracking
- Non-streaming: Uses actual `usage` from API response
- Streaming: Estimates input tokens, reads actual usage from SSE events when available
- Stored in SQLite database (`logs/requests.db`)
- Terminal display via Rich (`dashboard/display.py`)
- Web dashboard via API endpoints (`dashboard/api.py`)

### Web Dashboard
- Static files in `static/` directory
- Token usage stats and request history via API endpoints

## Remote Server Maintenance (192.168.31.101)

Le serveur distant est une Ubuntu 24.04 qui héberge d'autres workloads (P-core, etc.).

### Problème connu : VS Code Remote-SSH

La connexion VS Code Remote-SSH peut échouer avec l'erreur `AsyncPipeFailed(NotFound)` quand :
- Le disque racine est > 90% (actuellement ~78% après nettoyage)
- Les jobs P-core saturent le CPU (load > 150)
- Le swap est saturé

### Nettoyage rapide

Sur la machine distante :
```bash
fix-vscode              # Tue les processus + nettoie l'état VS Code
~/scripts/clean-vscode-server.sh --rotate  # Rotation des logs seulement
~/scripts/clean-vscode-server.sh --check   # Vérification de l'état
```

Un cron de rotation des logs tourne chaque dimanche à 3h00.

### Swap

18.1 Go de swap total :
- `/swap.img` : 6.1 Go (fichier original)
- `/mnt/storage500/swap.img` : 12 Go (swap secondaire, ajouté le 29/05/2026)

### P-core : Contrôle des jobs

Les jobs P-core sont gérés par :
- `pcore-scheduler.service` (daemon CronManager)
- 17 timers systemd user

Modifications effectuées le 29/05/2026 :
- `prediction-core-live-observer.timer` : passage de 5 min → 15 min
- `pcore-calibration.timer` : décalé de 03:00 → 04:00 (conflit avec backup)

### Taille de la partition

P-core a été déplacé vers `/mnt/storage500/P-core/` avec un symlink :
```
/home/jul/P-core -> /mnt/storage500/P-core/
