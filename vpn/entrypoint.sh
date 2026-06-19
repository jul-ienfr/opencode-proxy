#!/bin/bash
# Docker VPN entrypoint — launch OpenVPN + tinyproxy
# Expects: /vpn/configs/<server>.ovpn and /vpn/credentials.txt

set -e

CONFIG="${VPN_CONFIG:-/vpn/configs/de1223.ovpn}"
CREDS="${VPN_CREDS:-/vpn/credentials.txt}"
PROXY_PORT="${VPN_PROXY_PORT:-8888}"

echo "=== Starting VPN in Docker ==="
echo "Config: $CONFIG"
echo "Creds: $CREDS"

# Kill any existing processes
killall openvpn 2>/dev/null || true
killall tinyproxy 2>/dev/null || true
sleep 1

# Start OpenVPN in background
openvpn \
    --config "$CONFIG" \
    --auth-user-pass "$CREDS" \
    --auth-nocache \
    --daemon \
    --log /tmp/openvpn.log

# Wait for tun interface
echo "Waiting for tun interface..."
for i in $(seq 1 30); do
    if ip link show tun0 >/dev/null 2>&1; then
        echo "tun0 is up"
        break
    fi
    sleep 1
done

if ! ip link show tun0 >/dev/null 2>&1; then
    echo "ERROR: tun0 not found after 30s"
    cat /tmp/openvpn.log 2>/dev/null | tail -10
    exit 1
fi

# Check VPN IP
VPN_IP=$(curl -s --max-time 10 https://api.ipify.org 2>/dev/null || echo "unknown")
echo "VPN IP: $VPN_IP"

# Configure tinyproxy
sed -i "s/^Port .*/Port $PROXY_PORT/" /etc/tinyproxy/tinyproxy.conf
sed -i "s/^Allow .*/Allow 127.0.0.1/" /etc/tinyproxy/tinyproxy.conf

# Start tinyproxy
echo "Starting tinyproxy on port $PROXY_PORT..."
tinyproxy

echo "=== VPN ready ==="
echo "HTTP proxy: http://127.0.0.1:$PROXY_PORT"
echo "VPN IP: $VPN_IP"

# Keep running and monitor
while true; do
    sleep 30
    if ! pgrep openvpn >/dev/null 2>&1; then
        echo "OpenVPN died, restarting..."
        openvpn --config "$CONFIG" --auth-user-pass "$CREDS" --auth-nocache --daemon --log /tmp/openvpn.log
        sleep 5
    fi
done
