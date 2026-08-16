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
COPY --chown=opencode:opencode requirements.txt ./
COPY --chown=opencode:opencode opencode.py vpn_manager.py free_ip_pool.py shared_state.py ./
COPY --chown=opencode:opencode config/ ./config/
COPY --chown=opencode:opencode dashboard/ ./dashboard/
COPY --chown=opencode:opencode static/ ./static/

USER opencode

EXPOSE 4000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:4000/health')" || exit 1

CMD ["python", "opencode.py", "--no-gui"]
