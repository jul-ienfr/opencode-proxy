# Quickstart — plan de contrôle Gluetun 016

**Feature** : `016-gluetun-control-plane`
**Public** : opérateur de la flotte (6 stations), développeur du fork.
**Prérequis** : Docker, Go 1.26, Python 3.12, accès au fork
`jul-ienfr/gluetun`.

---

## 1. Construire et tester le fork

```bash
git clone https://github.com/jul-ienfr/gluetun.git
cd gluetun
git checkout master

go build ./...
go test ./internal/egress/... ./internal/dns/ladder/... ./internal/control/...
golangci-lint run ./...
```

Build de l'image multi-arch :

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t ghcr.io/jul-ienfr/gluetun:016-dev \
  --push .
```

Épingler par digest dans `docker-compose.yml` :

```yaml
services:
  vpn-gluetun-6:
    image: ghcr.io/jul-ienfr/gluetun@sha256:<digest>
    environment:
      CONTROL_PLANE_ENABLED: "on"
      CONTROL_STATE_DIR: /gluetun/control
      CONTROL_STATE_MODE: state
      CONTROL_EGRESS_QUORUM: "2"
      CONTROL_EGRESS_MIN_SOURCES: "3"
      DNS_LADDER: "dot,plain"
      DNS_FAIL_CLOSED: "true"
      TRANSPORT_LADDER: "wireguard,openvpn-udp,openvpn-tcp"
      TRANSPORT_ATTEMPT_TIMEOUT_S: "30"
      TRANSPORT_MAX_ATTEMPTS: "2"
```

## 2. Vérifier le démarrage

```bash
docker compose up -d vpn-gluetun-6
docker compose logs -f vpn-gluetun-6
```

Attendu :

```text
control plane enabled (version=1.0.0, state_mode=state)
state recovered (job=none, server=nl-…)
dns ladder ready (tiers=[dot,plain], fail_closed=true)
egress host baseline captured (ips=[…])
transport ladder ready (ladder=[wireguard,openvpn-udp,openvpn-tcp])
```

Contrôles :

```bash
curl -s http://127.0.0.1:8006/v1/control/capabilities | jq .
curl -s http://127.0.0.1:8006/v1/vpn/state | jq .
curl -s http://127.0.0.1:8006/v1/egress | jq .
curl -s http://127.0.0.1:8006/v1/dns/state | jq .
curl -s http://127.0.0.1:8006/v1/vpn/transport | jq .
```

Remplacez `8006` par le port de contrôle de la station (réseau Docker interne).

## 3. Faire une bascule

```bash
KEY="$GLUETUN_CONTROL_API_KEY"

# 1. planifier sans couper
curl -s -X POST http://127.0.0.1:8006/v1/vpn/switch \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: switch-s6-001" \
  -d '{
    "target": {"hostname": "nl-1234.nordvpn.com"},
    "constraints": {"require_new_ip": true, "require_verified": true},
    "options": {"dry_run": true}
  }' | jq .

# 2. exécuter et attendre
curl -s -X POST http://127.0.0.1:8006/v1/vpn/switch \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: switch-s6-002" \
  -d '{
    "target": {"hostname": "nl-1234.nordvpn.com"},
    "constraints": {"require_new_ip": true, "require_verified": true},
    "options": {"wait": true, "timeout_s": 45,
                "verify": {"quorum": 2, "min_sources": 3, "canary": true}}
  }' | jq '{id,state,error,verification}'
```

Suivre le job :

```bash
curl -s http://127.0.0.1:8006/v1/vpn/jobs/<job_id> \
  -H "Authorization: Bearer $KEY" | jq .
```

## 4. Rotation

```bash
curl -s -X POST http://127.0.0.1:8006/v1/vpn/rotate \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: rotate-s6-001" \
  -d '{
    "candidates": [
      {"hostname": "nl-1234.nordvpn.com"},
      {"hostname": "nl-5678.nordvpn.com"},
      {"hostname": "nl-9012.nordvpn.com"}
    ],
    "constraints": {
      "require_new_ip": true,
      "require_new_subnet": false,
      "exclude_subnets": ["198.51.100.0/24"],
      "max_candidates": 10,
      "max_attempts": 6
    },
    "options": {"wait": true, "timeout_s": 120}
  }' | jq '{state,attempts,station_preserved}'
```

- `committed` : première IP prouvée, arrêt.
- `exhausted` : budget épuisé, `attempts[]` contient la raison de chaque
  candidat ; la station reste sur son serveur courant.

## 5. DNS

```bash
# état
curl -s http://127.0.0.1:8006/v1/dns/state -H "Authorization: Bearer $KEY" | jq .

# sonde active
curl -s -X POST http://127.0.0.1:8006/v1/dns/probe \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"tier":"dot","domain":"example.com","timeout_s":3}' | jq .
```

Test de bascule (sur une station de test) :

```bash
# bloquer TCP/853 dans le conteneur de test
docker exec vpn-gluetun-test iptables -I OUTPUT -p tcp --dport 853 -j DROP
# résoudre deux fois
docker exec vpn-gluetun-test nslookup example.com 127.0.0.1
# attendu : 1re requête lente (fallback plain), suivante rapide
docker exec vpn-gluetun-test iptables -D OUTPUT -p tcp --dport 853 -j DROP
```

Vérifier `fail_closed` en bloquant tous les étages : la réponse doit être
`SERVFAIL`, jamais une requête vers le DNS de l'hôte.

## 6. Vérification d'egress

```bash
curl -s "http://127.0.0.1:8006/v1/egress/verify?fresh=true&quorum=2&min_sources=3" \
  -H "Authorization: Bearer $KEY" | jq .
```

Attendu :

```json
{
  "verified": true,
  "ip": "198.51.100.7",
  "quorum": 2,
  "source_count": 3,
  "confidence": "high",
  "dissenters": [],
  "canary": {"status": 200, "ok": true, "body_bytes": 128}
}
```

## 6bis. Ladder de transports

```bash
# état courant, breakers et tentatives
curl -s http://127.0.0.1:8006/v1/vpn/transport -H "Authorization: Bearer $KEY" | jq .

# forcer un transport pour une bascule de test
curl -s -X POST http://127.0.0.1:8006/v1/vpn/switch \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: switch-s6-tcp" \
  -d '{
    "target": {"hostname": "nl-1234.nordvpn.com"},
    "constraints": {"require_new_ip": false},
    "options": {"wait": true, "transport_policy": ["openvpn-tcp"]}
  }' | jq '{state,transport,attempts}'

# réarmer un breaker (admin)
curl -s -X POST http://127.0.0.1:8006/v1/vpn/transport/reset \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"transport": "wireguard"}' | jq .
```

Simuler un réseau qui bloque UDP (station de test uniquement) :

```bash
docker exec vpn-gluetun-test iptables -I OUTPUT -p udp --dport 1194 -j DROP
docker exec vpn-gluetun-test iptables -I OUTPUT -p udp --dport 51820 -j DROP
# attendu : wireguard/openvpn-udp en échec typé, openvpn-tcp committed
curl -s http://127.0.0.1:8006/v1/vpn/transport | jq '{active_transport,breaker,attempts}'
docker exec vpn-gluetun-test iptables -D OUTPUT -p udp --dport 1194 -j DROP
docker exec vpn-gluetun-test iptables -D OUTPUT -p udp --dport 51820 -j DROP
```

Après un cycle complet sans succès : événement `transport.cycle_exhausted`, puis
retour au premier transport (`wireguard`) après backoff. Aucun conteneur n'est
recréé.

## 7. Événements

```bash
# poll
curl -s "http://127.0.0.1:8006/v1/events?since=0&limit=50" \
  -H "Authorization: Bearer $KEY" | jq '.items[] | {seq,type,job_id}'

# SSE
curl -N "http://127.0.0.1:8006/v1/events/stream" \
  -H "Authorization: Bearer $KEY" \
  -H "Last-Event-ID: 0"
```

## 8. Intégration orchestrateur

```yaml
# config.yaml
control_plane:
  mode: shadow          # legacy | shadow | active
  base_url: "http://vpn-gluetun-{station}:8000"
  api_key_env: "GLUETUN_CONTROL_API_KEY"
  switch_timeout_s: 60
  rotate_timeout_s: 180
  events:
    ingest: true
    poll_interval_s: 5
```

```bash
python -m pytest tests/test_gluetun_control_v2.py \
                 tests/test_control_plane_shadow.py \
                 tests/test_control_plane_events.py -q
```

Passer une station en `active` :

```yaml
control_plane:
  mode: active
  stations: ["vpn-gluetun-6"]
```

## 9. Rollback

```bash
# côté fork : revenir au dernier commit
curl -s -X POST http://127.0.0.1:8006/v1/vpn/rollback \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"to":"last_committed","wait":true,"timeout_s":45}' | jq '{state,error}'

# côté projet : repasser en legacy
# config.yaml → control_plane.mode: legacy
# puis redémarrer le superviseur

# côté image : repin du digest précédent
docker compose up -d --no-deps vpn-gluetun-6
```

Si le state est suspect : `CONTROL_STATE_MODE=env` puis redémarrer ; les jobs en
cours seront marqués `interrupted`.

## 10. Dépannage

| Symptôme | Cause probable | Action |
|---|---|---|
| `409 active_job` | Un job tourne déjà | `GET /v1/vpn/jobs/{id}` ; attendre ou `cancel` (admin). |
| `403 forbidden_scope` | Clé `reader` sur une action | Utiliser une clé `controller`/`admin`. |
| `422 unknown_target` | Hostname absent du catalogue | `GET /v1/servers/facets` ; rafraîchir le catalogue. |
| `409 secret_persistence_unavailable` | Secret modifié sans clé de chiffrement | Définir `CONTROL_SECRET_KEY` ou ne pas persister le secret. |
| `verified=false` + `host_ip` | Tunnel down ou fuite | Vérifier le tunnel avant de conclure. |
| `dns.ladder_exhausted` | Tous les étages down | Vérifier TCP/853, DoH bloqué, connectivité ; ne pas revenir au DNS hôte. |
| `transport.cycle_exhausted` | Aucun transport n'a fourni de tunnel prouvé | Vérifier `GET /v1/vpn/transport`, les breakers et les raisons ; l'orchestrateur décide d'une rotation. |
| `tunnel.repair_needed` | Budget local épuisé (transport ou same-server) | Ne pas boucler ; escalade 015/orchestrateur, station conservée. |
| `failed_degraded` | Rollback impossible | Incident critique ; laisser 015 réparer, ne pas boucler. |
| `state.invalid` | Fichier corrompu | Fallback `env`, inspecter `state.json`, restauration backup. |

## 11. Critères de sortie

- `capabilities` répond, `state_mode=state`.
- `GET /v1/egress/verify` renvoie `verified=true` avec quorum ≥ 2.
- Une bascule dry-run ne coupe pas le tunnel.
- Une bascule réelle commit avec IP différente, et rollback fonctionne.
- DNS : blocage TCP/853 → bascule plain ≤ 2 s, tout down → `SERVFAIL`.
- Transports : UDP bloqué → `openvpn-tcp` committed en ≤ 2 tentatives ; cycle
  complet → retour `wireguard` après backoff ; un seul tunnel à tout instant.
- Le shadow Python ne diverge pas sur l'IP finale ni sur le transport retenu.
