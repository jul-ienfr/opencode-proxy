#!/bin/bash
# Docker VPN entrypoint — NordVPN official client + tinyproxy
# Uses the NordVPN Linux CLI instead of raw OpenVPN

set -e

TOKEN="${NORDVPN_TOKEN:-}"
COUNTRY="${NORDVPN_COUNTRY:-}"
GROUP="${NORDVPN_GROUP:-}"
TECHNOLOGY="${NORDVPN_TECHNOLOGY:-NordLynx}"
PROXY_PORT="${VPN_PROXY_PORT:-8888}"
KILLSWITCH="${NORDVPN_KILLSWITCH:-off}"

echo "=== Starting NordVPN in Docker ==="
echo "Technology: $TECHNOLOGY"
echo "Country: ${COUNTRY:-auto}"
echo "Group: ${GROUP:-none}"

# Clean up stale state (prevent boot loops after crash)
rm -f /run/nordvpn/nordvpnd.sock /run/nordvpn/nordvpnd.pid 2>/dev/null || true

# Start NordVPN daemon
nordvpnd &
sleep 3

# Login with token
if [ -n "$TOKEN" ]; then
    nordvpn login --token "$TOKEN"
else
    echo "ERROR: NORDVPN_TOKEN not set"
    exit 1
fi

# Configure technology
nordvpn set technology "$TECHNOLOGY" 2>/dev/null || true

# Enable kill switch if requested
if [ "$KILLSWITCH" = "on" ]; then
    nordvpn set killswitch on 2>/dev/null || true
fi

# Connect to VPN
echo "Connecting to NordVPN..."
if [ -n "$COUNTRY" ]; then
    nordvpn connect "$COUNTRY"
elif [ -n "$GROUP" ]; then
    nordvpn connect --group "$GROUP"
else
    nordvpn connect
fi

# Wait for connection
echo "Waiting for VPN connection..."
for i in $(seq 1 30); do
    if nordvpn status | grep -q "Connected"; then
        echo "NordVPN connected"
        break
    fi
    sleep 2
done

# Get VPN IP
VPN_IP=$(curl -s --max-time 10 https://api.ipify.org 2>/dev/null || echo "unknown")
echo "VPN IP: $VPN_IP"

# Start tinyproxy
tinyproxy
echo "HTTP proxy: http://127.0.0.1:$PROXY_PORT"

echo "=== NordVPN Docker ready ==="

# Monitor and auto-reconnect
while true; do
    sleep 30
    if ! nordvpn status | grep -q "Connected"; then
        echo "NordVPN disconnected, reconnecting..."
        if [ -n "$COUNTRY" ]; then
            nordvpn connect "$COUNTRY" || true
        elif [ -n "$GROUP" ]; then
            nordvpn connect --group "$GROUP" || true
        else
            nordvpn connect || true
        fi
        sleep 5
    fi
done
