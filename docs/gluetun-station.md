# Station gluetun — référence opérationnelle

> [plan v10 §10.1 Lot 3] Sources : wiki officiel
> [control-server](https://github.com/qdm12/gluetun-wiki/blob/main/setup/advanced/control-server.md) ·
> [healthcheck FAQ](https://github.com/qdm12/gluetun-wiki/blob/main/faq/healthcheck.md) ·
> source `qdm12/gluetun` internal/server/middlewares/auth/settings.go (schéma Role vérifié).
> Complété de l'incident ADR-005.

## Stacks par station

| Stack | Stations | Identifiants | Force | Faiblesse |
|---|---|---|---|---|
| `wireguard` | st1, st3, st4… | clé privée `vpn_configs/wireguard.env` (`WIREGUARD_PRIVATE_KEY`) | rapide, silencieux | handshake SANS erreur en cas d'échec : logs muets = suspect n°1 |
| `openvpn` | st2 (historique) | `OPENVPN_USER/PASSWORD` de `credentials.env` | logs explicites (AUTH_FAILED visible, hostnames nommés) | plus lent ; auth rejetée visiblement |

- Stack par station : `.env` clés `VPN_TYPE_STATION{n}` — écrites par `_apply_stack`
  sous lock (§14.1.9), **source de vérité du boot reconcile** (§14.1.1).
- Flip WG↔OV possible à chaud (`_apply_stack`, `--force-recreate`) ;
  canary `vpn-wg-test` (--profile wg-test) valide la clé NordLynx isolément.

## Rotation pays

1. Pin via control server : `PUT /v1/vpn/settings` (server_country) = vraie
   reconnexion sans recréation.
2. Curseur partagé `shared_rotation` : index effectif =
   `(cursor + country_offset + country_offset_stride × (station-1)) % N`
   → les stations ne tirent jamais le même pays.
3. Hostname serveur rejetant l'auth → blacklist TTL 24h + fast-pin skip.
4. Garde-fous : anti-flapping 6 rotations/h/station, `global_degraded`
   (≥50% stations tournées <10 min → pause 300 s), `rotation_paused`.

## Control server — authentification ⚠️ (incident ADR-005)

Auth **obligatoire depuis gluetun v3.39.1**. Deux mécanismes :

### A. Variable d'environnement (notre choix)

```env
HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE={"name": "normal", "auth": "apikey", "apikey": "<clé>"}
```

- Champs EXACTS (DisallowUnknownFields) : `name`, `auth` ∈ `none|apikey|basic`,
  `apikey`, `username`, `password`. ⚠️ `routes` est IGNORÉ en JSON (`json:"-"`) :
  le rôle par défaut hérite automatiquement de toutes les routes non couvertes.
- Le client envoie la clé dans `X-API-Key: <clé>`.
- ⚠️ Rôle absent ou `{}` → **401 sur TOUTES les routes y compris le healthcheck
  interne** → boucle unhealthy/churn (ADR-005). Symptôme signature dans les
  logs conteneur : `401 GET /v1/vpn/status wrote 13B` en rafale.

### B. Fichier (alternative)

```toml
[[roles]]
name = "normal"
auth = "apikey"
apikey = "..."
```
monté sur `HTTP_CONTROL_SERVER_AUTH_CONFIG_FILEPATH=/gluetun/auth/config.toml`.

## Healthcheck — comment ça marche (et ne pas paniquer pour rien)

Trois vérifications internes ; échec → gluetun redémarre SON VPN tout seul
(c'est de l'auto-cicatrisation normale, PAS un bug) :

| Check | Fréquence | Test |
|---|---|---|
| Startup | à chaque connexion, TCP+TLS 6 s | dial health target |
| Small periodic | chaque minute | ICMP ping OU requête DNS UDP |
| Full periodic | toutes les 5 min | TCP+TLS, 2 essais |

⚠️ **Ne pas confondre cause et conséquence** (FAQ officielle) :
`operation not permitted`, `i/o timeout`, `server misbehaving`,
`context canceled` sont des **conséquences** du tunnel non fonctionnel —
chercher la cause AMONT dans cet ordre :
1. identifiants/clé valides ? (expirent, notamment WireGuard)
2. IP serveur périmée ? → updater la liste de serveurs
3. serveur planté ? → changer les filtres (pays)
4. tag d'image ? essayer un tag précédent
5. firewall hôte bloque le sortant ?
6. connexion Internet hôte ?

## DNS

- L'image récente active par défaut son resolver avec upstreams **DoT**
  (DNS over TLS) — flanche par intermittence à travers certains tunnels WG.
- Notre config fiable : **plain UDP 53 vers 1.1.1.1 dans le tunnel** :
  ```yaml
  DNS_UPSTREAM_PLAIN_ADDRESSES: 1.1.1.1:53   # format ip:port OBLIGATOIRE
  ```
  (l'ancienne var `DNS_ADDRESS: ip` fonctionne encore, warning legacy ;
  `DNS_OVER_TLS` n'existe plus dans cette image — ignoré silencieusement.)
- Symptôme DoT cassé : `[socks5] handling connect request: dial tcp: lookup
  <host> on 127.0.0.1:53: server misbehaving`.

## MTU

- PMTUD peut échouer systématiquement selon le réseau (ICMP+TCP bloqués) →
  gluetun retombe à 1320, parfois insuffisant : les gros paquets TLS des
  probes black-holent alors que les petits HTTP passent.
- Knob officiel FAQ : `WIREGUARD_MTU=1280` et `OPENVPN_MSSFIX=1280`.
- Vérifier l'effectif : `docker exec <ctn> cat /sys/class/net/tun0/mtu`.

## Liste de serveurs NordVPN

- `UPDATER_PERIOD=480h` (20 jours) — une liste périmée = taux de serveurs
  morts élevé (symptôme : handshakes silencieux multi-pays).
- Update manuelle immédiate : `PUT /v1/updater/status {"status":"running"}`
  via le control server d'une station saine.
