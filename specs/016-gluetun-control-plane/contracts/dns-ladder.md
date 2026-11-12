# Contrat — DNS ladder redondant

**Feature** : `016-gluetun-control-plane`
**Composant** : `internal/dns/ladder` + intégration `internal/dns`
**Version** : `1.0.0`

---

## 1. Objectif

Garantir la résolution DNS lorsque qu'un type de transport (DoT, DoH, plain), un
fournisseur ou une adresse tombe, sans redémarrer le tunnel, sans fuite hors
tunnel, et avec un état observable. Le ladder est **fail-closed** : si tous les
étages sont down, `SERVFAIL`, jamais les résolveurs de l'hôte.

## 2. Configuration

| Variable | Défaut | Description |
|---|---|---|
| `DNS_LADDER` | `dot,plain` (flotte) / `dot` (upstream) | Étages ordonnés, séparés par virgule. |
| `DNS_TIER_PROVIDERS_DOT` | `cloudflare,quad9` | Fournisseurs DoT de l'étage. |
| `DNS_TIER_PROVIDERS_DOH` | `cloudflare,google` | Fournisseurs DoH. |
| `DNS_TIER_ADDRESSES_PLAIN` | `1.1.1.1:53,9.9.9.9:53` | Adresses plain. |
| `DNS_TIER_TIMEOUT_MS` | `800` | Timeout par tentative d'étage. |
| `DNS_TIER_ATTEMPTS` | `2` | Tentatives par étage avant fallback. |
| `DNS_HEALTH_FAILURES` | `3` | Échecs consécutifs → `down`. |
| `DNS_HEALTH_SUCCESSES` | `2` | Succès consécutifs → `up`. |
| `DNS_HEALTH_COOLDOWN_MS` | `30000` | Cooldown initial d'un étage down. |
| `DNS_HEALTH_COOLDOWN_MAX_MS` | `300000` | Plafond du backoff. |
| `DNS_PROBE_INTERVAL_S` | `30` | Sonde active par étage down/standby. |
| `DNS_PROBE_DOMAIN` | `example.com` | Domaine neutre de sonde. |
| `DNS_FAIL_CLOSED` | `true` | Interdit les resolvers de l'hôte. |
| `DNS_CACHE_ENABLED` | valeur upstream | Cache conservé au niveau listener. |

Contraintes :

- `DNS_LADDER` doit contenir au moins deux étages actifs si
  `DNS_FAIL_CLOSED=true` et `DNS_REDUNDANCY_REQUIRED=true` (défaut). Sinon la
  validation de configuration échoue au démarrage avec un message explicite.
- Un étage `plain` sans adresse est ignoré avec un warning, pas une erreur
  fatale.
- `dot` et `doh` partagent la même liste de fournisseurs si la variable
  spécifique est absente.
- DoH n'est **pas** prioritaire par défaut (mesure : bloqué sur le réseau de
  l'opérateur).

## 3. Modèle

```go
type TierType string // "dot" | "doh" | "plain"

type Tier struct {
    Type      TierType
    Endpoints []*Endpoint
    State     TierState // up|degraded|down|standby
    Timeout   time.Duration
    Attempts  int
    Cooldown  time.Duration
    Health    HealthCounters
}

type Endpoint struct {
    Address   string // ip:port ou hostname:port
    Provider  string
    State     EndpointState
    LatencyEWMA time.Duration
    Successes, Failures uint64
    CooldownUntil time.Time
    LastError string
}

type Ladder struct {
    Tiers      []*Tier // ordre de préférence
    ActiveTier *Tier
    FailClosed bool
}
```

## 4. Algorithme par requête

```text
resolve(q):
  for tier in ladder.tiers where tier.state != down:
      if tier in cooldown: continue
      for attempt in 1..tier.attempts:
          resp, err := query(tier, q, timeout=tier.timeout)
          if err == timeout or err == servfail or err == refused:
              recordFailure(tier, err); continue
          if err == truncated:
              resp, err = queryTCP(tier, q)  # même étage, avant fallback
              if err != nil: recordFailure(tier, err); continue
          if err != nil:
              recordFailure(tier, err); continue
          recordSuccess(tier)
          if resp.rcode == NXDOMAIN:
              return resp            # réponse valide, pas de fallback
          return resp
      # étage épuisé
      markDegraded(tier)
  if failClosed:
      emit("dns.ladder_exhausted")
      return SERVFAIL
  return error                       # jamais atteint si failClosed=true
```

Règles précises :

1. **Fallback uniquement** sur `timeout`, `SERVFAIL`, `REFUSED`, erreur réseau,
   TLS/handshake, ou réponse malformée.
2. **`NXDOMAIN`, `NOERROR` (même vide), `NOTIMP`** sont des réponses valides :
   retournées telles quelles, sans fallback.
3. **TC (truncated)** : retry TCP sur le **même étage** avant tout fallback.
4. **Plusieurs fournisseurs par étage** : si un endpoint échoue, essayer le
   suivant du même étage avant de passer à l'étage inférieur.
5. **Sélection d'endpoint** : préférer le dernier endpoint sain (sticky) pour
   maximiser le cache ; sinon round-robin pondéré par EWMA.
6. **Cooldown** : un endpoint en échec est exclu pendant un backoff exponentiel
   (base `DNS_HEALTH_COOLDOWN_MS`, plafond `DNS_HEALTH_COOLDOWN_MAX_MS`).
7. **Fail-closed** : aucune requête vers `/etc/resolv.conf`, `127.0.0.53`,
   `systemd-resolved`, ni les DNS de l'hôte/underlay.
8. **Pas de fuite** : les requêtes partent dans le namespace du tunnel ; si le
   tunnel est down, `SERVFAIL` plutôt qu'une requête hors tunnel.

## 5. Santé

### États d'étage

```text
standby ── première requête/probe OK ──► up
up ── DNS_HEALTH_FAILURES échecs ──────► degraded
degraded ── nouvel échec ──────────────► down (cooldown)
degraded ── succès ────────────────────► up
down ── DNS_HEALTH_SUCCESSES succès ───► up
down ── cooldown expiré ───────────────► half_open (probe)
half_open ── échec ────────────────────► down (cooldown ×2)
```

### Compteurs

- Succès/échec par **requête** et par **probe active**.
- `latency_ewma_ms` : EWMA α=0,3, mise à jour sur succès uniquement.
- `last_error` : code typé court (`timeout`, `servfail`, `refused`, `tls`,
  `truncated_retry_failed`, `malformed`), jamais de message contenant une
  donnée utilisateur.
- La sonde active utilise `DNS_PROBE_DOMAIN` et n'écrit pas dans le cache.

### Événements

```text
dns.tier_up        {tier, endpoint, latency_ms}
dns.tier_down      {tier, endpoint, failures, cooldown_ms}
dns.tier_recovered {tier, latency_ms}
dns.ladder_exhausted {ladder, last_errors}
```

## 6. Intégration avec l'existant

- Un seul listener DNS (port 53 interne) ; le ladder est le `Resolver` en amont.
- Cache, filtre de blocklist, `localdns` et `publicNamesAsLocal` restent
  **au-dessus** du ladder, une seule fois.
- Le healthcheck DNS de Gluetun interroge le listener local, pas chaque étage :
  il ne redémarre plus le tunnel quand un étage tombe, puisque le listener
  répond tant qu'un étage est up.
- `GET /v1/dns/status` (upstream) continue de fonctionner ; `GET /v1/dns/state`
  et `/v1/dns/tiers` exposent le détail.
- `PUT /v1/dns/status` et `PUT /v1/dns/settings` restent compatibles ; un
  changement de `DNS_LADDER` à chaud prend effet à la requête suivante, sans
  restart.

## 7. Endpoints

| Méthode | Route | Scope | Réponse |
|---|---|---|---|
| GET | `/v1/dns/state` | reader | résumé + `active_tier` + `fail_closed` |
| GET | `/v1/dns/tiers` | reader | `ResolverTier[]` complet |
| POST | `/v1/dns/probe` | controller | sonde active (`tier`, `domain`, `timeout_s`) |

`POST /v1/dns/probe` :

```json
{"tier": "dot", "domain": "example.com", "timeout_s": 3, "dry_run": false}
```

Réponse :

```json
{"tier":"dot","ok":true,"latency_ms":42,"rcode":"NOERROR","answers":1,"endpoint":"1.1.1.1:853"}
```

## 8. Métriques Prometheus

```text
gluetun_dns_tier_requests_total{tier,result}
gluetun_dns_tier_failures_total{tier,reason}
gluetun_dns_tier_latency_seconds{tier}
gluetun_dns_tier_state{tier}            # 0=down,1=degraded,2=up,3=standby
gluetun_dns_endpoint_requests_total{tier,provider,result}
gluetun_dns_ladder_fallbacks_total{from_tier,to_tier,reason}
gluetun_dns_fail_closed                       # 0/1
```

## 9. Critères d'acceptation

| ID | Scénario | Attendu |
|---|---|---|
| DNS-AT-01 | `DNS_LADDER=dot,plain`, TCP/853 bloqué | 1re requête DoT timeout, rejouée plain, réponse OK ; requête suivante direct plain. |
| DNS-AT-02 | Fournisseur DoT A renvoie SERVFAIL, B répond | Réponse B, pas de fallback d'étage. |
| DNS-AT-03 | Tous les étages down | `SERVFAIL`, `fail_closed=true`, événement, aucune requête hors tunnel. |
| DNS-AT-04 | `NXDOMAIN` du premier étage | Retourné tel quel, aucun fallback. |
| DNS-AT-05 | Réponse TC | Retry TCP même étage, puis succès. |
| DNS-AT-06 | Récupération DoT | 2 probes OK → `up`, événement `dns.tier_recovered`. |
| DNS-AT-07 | `DNS_LADDER=doh` seul | Démarre mais expose un warning `single_tier` ; pas de bascule possible. |
| DNS-AT-08 | DoH bloqué, ladder `dot,plain` | La station reste utilisable ; aucun restart de tunnel. |

## 10. Hors périmètre

- Modification des réponses DNS (fastest-IP, split-horizon).
- DoH/DoT vers des résolveurs choisis dynamiquement par score de latence
  Internet (seulement EWMA local par endpoint).
- Résolution des noms du control server lui-même (elle utilise le résolveur
  système avant le tunnel, comme upstream).
