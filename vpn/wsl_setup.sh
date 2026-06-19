#!/bin/bash
# WSL2 VPN setup — install OpenVPN + tinyproxy
# Run once: wsl -- bash vpn/wsl_setup.sh

set -e
echo "=== Installing OpenVPN + tinyproxy in WSL2 ==="

sudo apt-get update -qq
sudo apt-get install -y -qq openvpn tinyproxy curl

echo "=== Setup complete ==="
echo "OpenVPN: $(which openvpn)"
echo "Tinyproxy: $(which tinyproxy)"
