#!/usr/bin/env bash
# [plan 18/08 §2a] Fetch the NordVPN NordLynx (WireGuard) private key.
#
# gluetun with VPN_TYPE=wireguard does NOT auto-register a key — it
# requires WIREGUARD_PRIVATE_KEY pre-obtained from NordVPN. The service
# credentials endpoint returns the SAME key for every call of one
# account (NordGen issue #26: one access token, one private key per
# account, 10 devices max) — so ONE file is shared by both stations.
#
#   NORDVPN_ACCESS_TOKEN=<64-char token> ./scripts/fetch_nordlynx_key.sh
#   ./scripts/fetch_nordlynx_key.sh <64-char token>     # arg form
#
# Token source: dashboard my.nordaccount.com or `nordvpn token`.
# Exchange (verified against live API 18/08 — auth is basic with the
# literal username "token", NOT the token as username):
#   curl -s -u "token:<TOKEN>" "https://api.nordvpn.com/v1/users/services/credentials"
#   → {"nordlynx_private_key": "<base64>"}
#
# Writes vpn_configs/wireguard.env (gitignored, chmod 600, same regime
# as credentials.env). Refuses to overwrite without --force. The key is
# a secret (it grants a VPN connection) — never put it in .env,
# config.yaml or any committed file.
set -euo pipefail

cd "$(dirname "$0")/.."                       # repo root
OUT="vpn_configs/wireguard.env"

FORCE=0
TOKEN=""
for a in "$@"; do
    if [[ "$a" == "--force" ]]; then FORCE=1; else TOKEN="$a"; fi
done
TOKEN="${NORDVPN_ACCESS_TOKEN:-$TOKEN}"
if [[ -z "$TOKEN" ]]; then
    echo "usage: NORDVPN_ACCESS_TOKEN=<token> $0 [--force]   (or token as \$1)" >&2
    echo "token = 64-hex NordVPN access token (dashboard my.nordaccount.com)" >&2
    exit 1
fi
if [[ ! "$TOKEN" =~ ^[A-Za-z0-9]{64}$ ]]; then
    echo "error: token must be exactly 64 alphanumeric chars (got ${#TOKEN})" >&2
    exit 1
fi

if [[ -f "$OUT" && "$FORCE" != 1 ]]; then
    echo "error: $OUT already exists — rerun with --force to overwrite" >&2
    exit 1
fi

echo "exchanging access token for NordLynx key…" >&2
RESP="$(curl -sS -u "token:$TOKEN" "https://api.nordvpn.com/v1/users/services/credentials")"
KEY="$(printf '%s' "$RESP" | sed -n 's/.*"nordlynx_private_key"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
if [[ -z "$KEY" ]]; then
    echo "error: no nordlynx_private_key in response: $RESP" >&2
    exit 1
fi

mkdir -p vpn_configs
umask 177                                   # rw------- before the file exists
printf 'WIREGUARD_PRIVATE_KEY=%s\n' "$KEY" > "$OUT"
chmod 600 "$OUT"
echo "ok: $OUT written (chmod 600, shared by both stations)" >&2
