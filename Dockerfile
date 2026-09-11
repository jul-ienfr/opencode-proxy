FROM python:3.11-slim

WORKDIR /app

# System deps: ca-certificates + OpenVPN for IP rotation
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    openvpn \
    && rm -rf /var/lib/apt/lists/*

# docker CLI + compose v2 plugin ([13]): vpn_manager.py drives gluetun via
# `docker compose up/pull/inspect/restart`, talking to the HOST daemon over
# /var/run/docker.sock (mounted in docker-compose.yml). Debian bookworm has
# no docker-cli / docker-compose-plugin packages, so install the pinned
# static binaries. docker-compose.yml itself is NOT baked in — the compose
# deployment mounts the host project dir (see VPN_DOCKER_COMPOSE_FILE).
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-27.5.1.tgz -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && install -m 0755 /tmp/docker/docker /usr/local/bin/docker \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64 \
         -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose \
    && rm -rf /tmp/docker /tmp/docker.tgz \
    && docker --version && docker compose version

# Install Python dependencies from requirements.txt (respects version pins)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Non-root user for security
RUN groupadd -r opencode && useradd -r -g opencode -d /app -s /sbin/nologin opencode \
    && mkdir -p /app/logs /app/vpn_configs && chown -R opencode:opencode /app

# Explicit COPY only — never `COPY . .` (would bake config.yaml / vpn_configs secrets into the image)
# [plan v10 §14.0.5] Liste alignée sur les imports réels au boot :
#   opencode.py → traffic_capture, protocol_mapping, trust
#   opencode.py lifespan → shared_rotation, scripts.make_credentials_env
#   [P6 fix] imports tardifs/lifespan NON copiés avant → ModuleNotFoundError
#   possible au boot conteneurisé : docker_events, station_supervisor,
#   latency_rotation, ip_latency, free_discovery.
# [Phase 9 plan boot — corrigé 10/09] Les packages issus des refontes
# (phases 1-9) n'étaient PAS copiés : `core`, `app`, `upstream`, `server`,
# `observability`, `ops`, `protocol`, `streaming`, `vpn`, `free`,
# `dashboard.routes`. Le smoke-test d'import en fin de build échouait donc
# systématiquement (ModuleNotFoundError: No module named 'core' — vérifié en
# rejouant les COPY dans un dossier isolé). On copie désormais les packages
# en entier, en gardant l'exclusion stricte des secrets.
COPY --chown=opencode:opencode requirements.txt ./
COPY --chown=opencode:opencode opencode.py trust.py vpn_manager.py free_ip_pool.py shared_state.py shared_rotation.py traffic_capture.py protocol_mapping.py docker_events.py station_supervisor.py latency_rotation.py ip_latency.py free_discovery.py ./
COPY --chown=opencode:opencode app/ ./app/
COPY --chown=opencode:opencode config/ ./config/
COPY --chown=opencode:opencode core/ ./core/
COPY --chown=opencode:opencode dashboard/ ./dashboard/
COPY --chown=opencode:opencode free/ ./free/
COPY --chown=opencode:opencode gui/ ./gui/
COPY --chown=opencode:opencode observability/ ./observability/
COPY --chown=opencode:opencode ops/ ./ops/
COPY --chown=opencode:opencode protocol/ ./protocol/
COPY --chown=opencode:opencode server/ ./server/
COPY --chown=opencode:opencode static/ ./static/
COPY --chown=opencode:opencode streaming/ ./streaming/
COPY --chown=opencode:opencode upstream/ ./upstream/
COPY --chown=opencode:opencode vpn/ ./vpn/
COPY --chown=opencode:opencode scripts/make_credentials_env.py ./scripts/make_credentials_env.py
COPY --chown=opencode:opencode scripts/precompress.py ./scripts/precompress.py
# [Phase 5 plan boot] Assets pré-compressés AU BUILD (jamais au runtime) : le
# démarrage ne compresse plus 250 Ko de JS en zlib et le service sert du
# brotli (−82 % mesuré sur app.js). Non fatal : sans brotli, gzip seul ; sans
# aucun pré-compressé, dashboard/routes/static.py recompresse à la volée.
RUN python scripts/precompress.py -q || echo "precompress skipped (fallback runtime)"

# [P6] smoke-test d'import au build : un module manquant casse le build ici
# (et non au boot conteneur, en prod).
RUN python -c "import opencode" || (echo "SMOKE TEST FAILED: import opencode" && exit 1)
# [Phase 3b-1] Vérifie que httpx (donc rich/click) N'EST PAS chargé au boot :
# c'est le contrat de perf du boot. Régression = build rouge.
RUN python -c "import sys, opencode; assert 'httpx' not in sys.modules, 'httpx charge au boot (Phase 3b-1 regressee)'; print('boot import: httpx deferred OK')"

USER opencode

EXPOSE 4000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:4000/health')" || exit 1

CMD ["python", "opencode.py", "--no-gui"]
