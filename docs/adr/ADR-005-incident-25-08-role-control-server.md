# ADR-005 — Incident 25/08 : rôle du control server perdu, churn healthcheck, DNS/MTU

**Date** : 2026-08-24/25 (nuit) · **Statut** : résolu, prévention en place · **Sévérité** : P0 production

## Symptômes observés

1. Flotte 0/4 `connected` pendant des heures ; stations oscillant connecté ↔ error à chaque tick.
2. Docker : conteneurs `unhealthy` / boucle `Restarting (N)` ; « churn: 22 restarts in the last 10m ».
3. Logs gluetun : `401 GET /v1/vpn/status` **en boucle — y compris depuis le healthcheck interne de glueton lui-même**.
4. Proxies SOCKS5 hôtes muets ou intermittents ; probes du proxy en erreur (« conteneur actif mais tunnel sans réponse »).
5. GUI dashboard : tout rouge.

## Chaîne causale (post-mortem)

```
make_credentials_env.py réécrivait credentials.env EN ENTIER (2 lignes)
  → perte de HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE (et un temps, rôle = {})
    → gluetun v3.39.1+ : auth OBLIGATOIRE, rôle vide = zéro permission
      → TOUTES les routes control server en 401, y compris /v1/vpn/status
        → le healthcheck INTERNE de gluetun est rejeté comme les autres
          → docker unhealthy + boucle restart interne infinie
            → tunnels cyclés sans fin -> probes proxy -> tout en erreur
```

Deux facteurs contributifs ont prolongé/amplifié l'incident :
- **DoT par défaut de l'image** flanchait à travers certains tunnels WG
  (`lookup ... on 127.0.0.1:53: i/o timeout`) → résolutions intermittentes.
- **PMTUD échouait systématiquement** sur ce réseau (`PMTUD failed with both
  ICMP and TCP`, MTU retombé à 1320) → les gros paquets TLS des probes
  black-holaient même sur un serveur sain.

## Erreurs commises pendant le diagnostic (et leçons)

| Erreur | Ce que ça a coûté | Bonne pratique (source officielle) |
|---|---|---|
| Rôle deviné `{"routes":["ALL"]}` | Boot panic exit 2 (`missing code for authentication method`, settings.go:178) — crash-loop st3 | Ne JAMAIS deviner un schéma : lire la source (`qdm12/gluetun` internal/server/middlewares/auth/settings.go) et le wiki ([control-server](https://github.com/qdm12/gluetun-wiki/blob/main/setup/advanced/control-server.md)) |
| `DNS_UPSTREAM_PLAIN_ADDRESSES=1.1.1.1` (IP nue) | Crash-loop st3 : `not an ip:port` — la variable moderne exige `ip:port` | Format `ip:port` explicite ; l'ancienne `DNS_ADDRESS=ip` marche encore (warning legacy) |
| Confondre conséquences et causes | Temps perdu sur les probes/MTU avant de voir les 401 | FAQ healthcheck ⚠️ : « operation not permitted », « i/o timeout », « server misbehaving » sont des **conséquences** du VPN non fonctionnel — chercher la cause AMONT |
| Supposer les identifiants fausses puis bridage NordVPN | Fausse piste (« attendre demain ») | Tester chaque hypothèse séparément : canary `vpn-wg-test` (clé WG seule), egress interne par conteneur, validation creds directe API |

## Configuration correcte (validée wiki + source + production)

### Auth du control server (credentials.env)

```env
VPN_CONTROL_API_KEY=<clé>
HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE={"name": "normal", "auth": "apikey", "apikey": "<clé>"}
```

- Champs exacts du schéma (`DisallowUnknownFields` actif) : `name`, `auth`
  (`none|apikey|basic`), `apikey`, `username`, `password`. **`routes` est
  ignoré en JSON** (`json:"-"`) : le rôle par défaut couvre automatiquement
  toutes les routes non couvertes par le fichier de config.
- Alternative fichier : `HTTP_CONTROL_SERVER_AUTH_CONFIG_FILEPATH=/gluetun/auth/config.toml`
  ```toml
  [[roles]]
  name = "normal"
  auth = "apikey"
  apikey = "..."
  ```

### DNS (compose x-gluetun-multi-env)

```yaml
DNS_UPSTREAM_PLAIN_ADDRESSES: 1.1.1.1:53   # format ip:port OBLIGATOIRE
```
(DNS_OVER_TLS n'existe plus dans cette image — silencieusement ignoré.)

### MTU (compose x-gluetun-multi-env + blocs littéraux st1/st2)

```yaml
WIREGUARD_MTU: '1280'
OPENVPN_MSSFIX: '1280'
```
Knob officiel FAQ healthcheck quand PMTUD échoue et que TLS black-hole.

## Préventions mises en place

1. `make_credentials_env.py` : **upsert** au lieu d'overwrite — ne peut plus
   effacer VPN_CONTROL_API_KEY ni le rôle.
2. `tests/test_docs_drift.py` : sections protégées de config.yaml surveillées
   (toute disparition = gate rouge).
3. Toggle `rotation_paused` + escape hatch `supervisor.enabled` : interrupteurs
   d'urgence documentés (§14.0.3, runbook-docker.md).
4. Canary `vpn-wg-test` (--profile wg-test) : valide la clé NordLynx isolément,
   sans toucher aux stations réelles.
5. Chaos tests (`tests/test_chaos_lifecycle.py`) : primitives lifecycle
   idempotentes prouvées.

## Vérification finale

Après correction : 4/4 conteneurs `healthy`, flotte **4/4 connected**, uptime
5-6 h sans aucun restart, quotas free qui remontent normalement.
