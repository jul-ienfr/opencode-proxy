#!/bin/bash
# WSL2 VPN start — launch OpenVPN + tinyproxy
# Called by vpn_manager.py: wsl -- bash vpn/wsl_start.sh <config> <creds>

set -e

CONFIG="${1:?Usage: wsl_start.sh <config.ovpn> <credentials.txt>}"
CREDS="${2:?Usage: wsl_start.sh <config.ovpn> <credentials.txt>}"
PROXY_PORT="${3:-8888}"

# Convert Windows paths to WSL paths
CONFIG_WSL=$(wslpath -u "$CONFIG" 2>/dev/null || echo "$CONFIG")
CREDS_WSL=$(wslpath -u "$CREDS" 2>/dev/null || echo "$CREDS")

echo "=== Starting VPN in WSL2 ==="
echo "Config: $CONFIG_WSL"
echo "Creds: $CREDS_WSL"

# Kill any existing OpenVPN/tinyproxy
sudo killall openvpn 2>/dev/null || true
sudo killall tinyproxy 2>/dev/null || true
sleep 1

# Start OpenVPN in background
sudo openvpn \
    --config "$CONFIG_WSL" \
    --auth-user-pass "$CREDS_WSL" \
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

# Start tinyproxy
echo "Starting tinyproxy on port $PROXY_PORT..."
sudo sed -i "s/^Port .*/Port $PROXY_PORT/" /etc/tinyproxy/tinyproxy.conf 2>/dev/null || true
sudo tinyproxy

echo "=== VPN ready ==="
echo "SOCKS proxy: http://127.0.0.1:$PROXY_PORT"
echo "VPN IP: $VPN_IP"

# Keep running
while true; do
    sleep 60
    # Check if OpenVPN is still running
    if ! pgrep openvpn >/dev/null 2>&1; then
        echo "OpenVPN died, restarting..."
        sudo openvpn --config "$CONFIG_WSL" --auth-user-pass "$CREDS_WSL" --auth-nocache --daemon --log /tmp/openvpn.log
        sleep 5
    fi
done
