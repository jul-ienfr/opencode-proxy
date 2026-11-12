# Contrat — Ladder de transports

**Feature** : `016-gluetun-control-plane`
**Composant** : `internal/vpn/transport` + intégration `internal/vpn`
**Version** : `1.0.0`

---

## 1. Objectif

Choisir automatiquement le premier transport qui fournit un tunnel **prouvé**
parmi `wireguard`, `openvpn-udp` et `openvpn-tcp`, passer au suivant en cas
d'échec, puis revenir au premier après un cycle complet et un backoff, **sans
redémarrer le conteneur**, **sans second tunnel** et **sans changer de
serveur/IP/hostname**.

Le ladder est local à la station : il ne reçoit pas de liste de serveurs, ne
re-score rien, ne consulte ni quota ni claim.

## 2. Configuration

| Variable | Défaut | Description |
|---|---|---|
| `TRANSPORT_LADDER` | `wireguard,openvpn-udp,openvpn-tcp` | Ordre des transports. |
| `TRANSPORT_ATTEMPT_TIMEOUT_S` | `30` (WG/UDP), `45` (TCP) | Budget par tentative. |
| `TRANSPORT_MAX_ATTEMPTS` | `2` | Tentatives par transport dans un cycle. |
| `TRANSPORT_BREAKER_THRESHOLD` | `3` | Échecs consécutifs avant `open`. |
| `TRANSPORT_BREAKER_COOLDOWN_S` | `120` | Cooldown initial d'un breaker `open`. |
| `TRANSPORT_BREAKER_COOLDOWN_MAX_S` | `900` | Plafond du backoff de breaker. |
| `TRANSPORT_BACKOFF_BASE_S` | `30` | Backoff après un cycle épuisé. |
| `TRANSPORT_BACKOFF_MAX_S` | `300` | Plafond du backoff de cycle. |
| `TRANSPORT_MAX_CYCLES` | `3` | Cycles complets avant `tunnel.repair_needed`. |
| `TRANSPORT_JITTER` | `0.2` | Jitter appliqué aux backoffs (fraction). |
| `TRANSPORT_LOCAL_RECONNECT_MAX` | `3` | Reconnexions même-serveur avant escalade. |
| `VPN_TYPE` | vide | `openvpn`/`wireguard` explicite ⇒ ladder désactivé. |

Contraintes :

- `TRANSPORT_LADDER` doit contenir au moins un transport connu ; une valeur
  inconnue est une erreur de configuration au démarrage.
- Un doublon dans le ladder est ignoré avec un warning.
- `VPN_TYPE` explicite prime toujours : comportement upstream préservé.
- Les variables `OPENVPN_*` et `WIREGUARD_*` existantes restent valides et sont
  utilisées par le transport correspondant.

## 3. Modèle

```go
type Transport string // "wireguard" | "openvpn-udp" | "openvpn-tcp"

type Policy struct {
    Ladder            []Transport
    AttemptTimeout    map[Transport]time.Duration
    MaxAttempts       int
    BreakerThreshold  int
    BreakerCooldown   time.Duration
    BreakerCooldownMax time.Duration
    BackoffBase       time.Duration
    BackoffMax        time.Duration
    MaxCycles         int
    Jitter            float64
}

type Attempt struct {
    ID       string
    Transport Transport
    Server   string
    Started  time.Time
    Ended    time.Time
    Result   AttemptResult // success|failed|skipped
    Reason   Reason
    Latency  time.Duration
    NextRetry time.Time
}

type State struct {
    Active    *Transport
    Phase     string // idle|preflight|applying|awaiting_tunnel|verifying|committed|cooldown
    Cycle     int
    Breakers  map[Transport]*Breaker
    Attempts  []Attempt // borné
    NextAt    *time.Time
}
```

Raisons typées :

```text
auth                 credentials refusés ; ne pas boucler sur ce transport
udp_blocked          pas de réponse UDP / handshake muet
tcp_refused          connexion TCP refusée ou filtrée
tls                  échec TLS/handshake OpenVPN
handshake_timeout    tunnel non établi dans le budget
no_egress            tunnel up mais preuve d'egress absente
interface_down       interface absente ou down après apply
config_invalid       settings du transport invalides
```

## 4. Algorithme

```text
run(server, policy):
  cycle = 0
  for cycle < policy.max_cycles:
      for transport in policy.ladder:
          if breaker[transport].open and now < retry_at: continue
          if breaker[transport].open and now >= retry_at: breaker = half_open
          if not preflight(transport, server):
              breaker.failure(transport, preflight.reason); continue

          teardown_current()               # aucun autre tunnel
          apply(transport, same server/ip/hostname)
          ok = await_tunnel(transport, deadline=policy.timeout[transport])
          if ok:
              ok = verify_egress()         # consensus + canary (Phase 1)
          if ok:
              commit(); breaker.success(transport); return success
          teardown_current()
          breaker.failure(transport, reason)
          emit("transport.failed", {transport, reason, latency_ms})
      cycle++
      emit("transport.cycle_exhausted", {cycle, ladder, reasons})
      sleep(backoff(cycle) with jitter)
  emit("tunnel.repair_needed")
  return exhausted
```

Règles :

1. **Un seul tunnel** : `teardown_current()` est appelé avant chaque `apply` et
   après chaque échec ; un verrou de boucle empêche deux exécutions.
2. **Ordre** : le ladder est suivi tel quel ; aucun réordonnancement dynamique.
3. **Retour au premier** : après un cycle complet, backoff exponentiel avec
   jitter, puis reprise à `policy.ladder[0]`.
4. **Breaker** : `closed → open` après `threshold` échecs consécutifs ;
   `open → half_open` après cooldown ; `half_open → closed` sur succès,
   `half_open → open` sur échec (cooldown doublé, plafonné).
5. **Erreur `auth`** : le transport est marqué, les suivants sont tentés ; après
   `max_cycles`, `tunnel.repair_needed` ; jamais de boucle infinie.
6. **Aucune sélection de serveur** : le ladder ne lit pas le catalogue et ne
   modifie jamais `hostname`, `endpoint_ip` ou `expected_egress_ip`.
7. **Sécurité** : firewall/kill switch réappliqués et MTU/PMTUD recalculés avant
   de déclarer le transport `up`.
8. **Watchdog même-serveur** : interface absente, handshake stale, statut non
   `running`, DNS local injoignable ⇒ reconnexion même transport/serveur,
   `local_reconnect_max` fois, puis transport suivant.

## 5. Shadow et compatibilité

- `free/transport_ladder.py` (015) peut rester en observation : il lit
  `GET /v1/vpn/transport` et compare son verdict, mais ne pilote plus le tunnel.
- `VPN_TYPE` explicite désactive le ladder ; `PUT /v1/vpn/settings` legacy
  continue de fonctionner.
- Un `transport_policy` de job restreint l'ordre ou force un transport ; sans
  lui, `auto` utilise `TRANSPORT_LADDER`.

## 6. Endpoints

### `GET /v1/vpn/transport` — reader

```json
{
  "active_transport": "openvpn-tcp",
  "policy": {"ladder": ["wireguard", "openvpn-udp", "openvpn-tcp"]},
  "phase": "committed",
  "cycle": 1,
  "breaker": {
    "wireguard": {"state": "open", "failures": 3, "retry_at": "2026-11-11T18:12:31Z"},
    "openvpn-udp": {"state": "open", "failures": 3, "retry_at": "2026-11-11T18:12:31Z"},
    "openvpn-tcp": {"state": "closed", "failures": 0}
  },
  "attempts": [
    {"transport": "wireguard", "result": "failed", "reason": "handshake_timeout", "latency_ms": 30000},
    {"transport": "openvpn-udp", "result": "failed", "reason": "udp_blocked", "latency_ms": 30000},
    {"transport": "openvpn-tcp", "result": "success", "reason": "", "latency_ms": 8000}
  ],
  "next_attempt_at": null,
  "updated_at": "2026-11-11T18:10:40Z"
}
```

### `POST /v1/vpn/transport/reset` — admin

```json
{"transport": "wireguard"}
```

Réponse : `{"transport":"wireguard","breaker":"closed","reset":true}`.
`transport` absent ⇒ réarme tous les breakers (`all`). Une remise à zéro ne
coupe pas le tunnel actif et ne force pas de cycle.

## 7. Métriques Prometheus

```text
gluetun_transport_attempts_total{transport,result}
gluetun_transport_failures_total{transport,reason}
gluetun_transport_connect_seconds{transport}
gluetun_transport_active{transport}          # 0/1
gluetun_transport_breaker_state{transport}   # 0=closed,1=open,2=half_open
gluetun_transport_cycles_total{result}
gluetun_transport_cycle_exhausted_total
```

## 8. Événements

```text
transport.attempt          {transport, server, attempt, result, reason, latency_ms}
transport.connected        {transport, server, egress_ip, latency_ms}
transport.failed           {transport, server, reason, latency_ms}
transport.breaker_open     {transport, failures, retry_at}
transport.breaker_reset    {transport}
transport.cycle_exhausted  {cycle, ladder, reasons}
tunnel.repair_needed       {cycles, last_reasons}
```

## 9. Critères d'acceptation

| ID | Scénario | Attendu |
|---|---|---|
| TR-AT-01 | WireGuard OK | `wireguard` commit au premier essai ; aucun autre transport appliqué. |
| TR-AT-02 | WireGuard handshake timeout, UDP OK | `openvpn-udp` commit ; événement `transport.failed` puis `connected`. |
| TR-AT-03 | UDP bloqué (WG + OVPN UDP) | `openvpn-tcp` commit en ≤ 2 tentatives ; station utilisable. |
| TR-AT-04 | Tous les transports échouent | `transport.cycle_exhausted`, backoff, reprise à `wireguard` ; aucun nouveau serveur choisi. |
| TR-AT-05 | Breaker `open` | Transport ignoré jusqu'au cooldown, puis sonde `half_open`. |
| TR-AT-06 | Credentials invalides | Aucune boucle infinie sur le même transport ; `tunnel.repair_needed` après budget. |
| TR-AT-07 | Tunnel meurt après commit | Watchdog même-serveur, reconnexion même transport, puis ladder si échec. |
| TR-AT-08 | `VPN_TYPE=wireguard` | Ladder désactivé ; comportement upstream. |
| TR-AT-09 | Pendant toute la séquence | `ip link` ne montre jamais deux interfaces VPN `up`. |
| TR-AT-10 | `POST /v1/vpn/transport/reset` | Breaker réarmé, tunnel actif inchangé. |

## 10. Hors périmètre

- Sélection ou scoring de serveur.
- Modification du quota, des claims ou des leases.
- Deux tunnels simultanés ou make-before-break intra-conteneur (feature 015
  pour l'inter-stations).
- Transport payant ou compte supplémentaire.
