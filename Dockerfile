FROM python:3.11-slim

WORKDIR /app

# System deps: ca-certificates + OpenVPN for IP rotation
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    openvpn \
    && rm -rf /var/lib/apt/lists/*

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

EXPOSE 4000 8082

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:4000/health')" || exit 1

CMD ["python", "opencode.py"]
