# Implementation Plan: Plan de contrôle Gluetun — sélection, rotation, DNS redondant et vérification d'egress

**Branch**: `016-gluetun-control-plane` | **Date**: 2026-11-11 | **Spec**: [spec.md](./spec.md)

**Input**: `specs/016-gluetun-control-plane/spec.md`, [audit.md](./audit.md),
[research.md](./research.md), fork `jul-ienfr/gluetun` (identique à
`passteque/gluetun:master` au 2026-11-11).

## Summary

Le fork est aujourd'hui un **agent de tunnel** sans plan de contrôle : sélection
statique par variables d'environnement, rotation seulement comme effet de bord
d'un redémarrage interne, `PUT /v1/vpn/settings` en stop/start brutal sans
préflight ni rollback, DNS mono-étage, et consensus d'IP publique qui porte sur
la géolocalisation, pas sur la valeur de l'IP.

Le plan ajoute cinq primitives composables, sans changer la frontière de
propriété :

1. **SwitchJob** — transaction validation → préflight → snapshot → application →
   attente du tunnel → preuve d'egress → commit/rollback, journal persistant,
   idempotence, un seul job actif.
2. **Rotation** — exécution d'une liste ordonnée fournie par l'orchestrateur (ou
   résolue depuis une politique), exclusions, budget, `exhausted` structuré,
   arrêt à la première IP prouvée.
3. **DNS ladder** — étages ordonnés `dot,doh,plain` (défaut flotte `dot,plain`),
   plusieurs fournisseurs par étage, santé active/passive, bascule par requête,
   `fail-closed`.
4. **Egress verification** — ≥ 3 sources indépendantes, consensus k-of-n sur la
   valeur d'IP, exclusion de l'IP d'hôte, canary HTTP 200 avec contenu, cache TTL
   et single-flight.
5. **Transport ladder** — ordre `wireguard,openvpn-udp,openvpn-tcp`, un seul
   tunnel, breaker/backoff, retour au premier après un cycle, sans recréation de
   conteneur et sans changer de serveur/IP.

L'exposition se fait par `/v1/control/*`, `/v1/vpn/{switch,rotate,jobs,transport,…}`,
`/v1/egress/*`, `/v1/dns/*`, `/v1/events*`, avec scopes, état durable et SSE.
L'orchestrateur Python migre par **strangler** (legacy → shadow → active).
`registry.db` reste l'autorité des IP, leases, quotas et du score composite
(constitution III et IX). Six stations, free uniquement, aucune attente.

## Technical Context

**Language/Version**: Go 1.26 (fork) ; Python 3.12 (orchestrateur).

**Primary Dependencies**: `github.com/qdm12/dns/v2` (`pkg/server`, `pkg/provider`,
dialers DoT/DoH/plain), `qdm12/gosettings`, `qdm12/gotree`,
`prometheus/client_golang`, stdlib `net/http`, `net/netip`, `log/slog` ; côté
projet `vpn/manager.py`, `free/ip_registry/*`, `free/station_health.py`,
`free/rotation.py`, `free/egress_*`, `dashboard/routers/*`, `sqlite3`.

**Storage**:
- Fork : `CONTROL_STATE_DIR` (défaut `/gluetun/control`) avec `state.json`
  (état désiré + dernier commit, `tmp` + `fsync` + `rename`, SHA-256, 0600),
  `jobs.jsonl`, `events.jsonl` (append-only bornés, rotation/rétention),
  `idempotency.json` (24 h). Les secrets ne sont pas persistés en clair : le
  state ne contient que les settings non secrets et des références
  d'environnement ; une modification de secret via l'API exige
  `CONTROL_SECRET_KEY` (AES-256-GCM), sinon `409
  secret_persistence_unavailable`.
- Projet : `registry.db` reste l'autorité ; les événements du fork sont ingérés
  dans `observations`/`incidents`, jamais lus comme seconde vérité.

**Testing**: `go test ./...`, `go vet`, `golangci-lint`, `httptest` (contrat),
domaine pur (consensus, sélecteur, machine à états, ladder de transports),
intégration avec faux DNS et échos HTTP locaux, chaos (kill tunnel, blocage
TCP/853 et UDP/TCP 1194-1198, 429, state corrompu) ; côté projet `pytest`,
adaptateur avec faux control server, parité shadow, chaos flotte 6 stations, soak
24 h.

**Target Platform**: conteneur Linux amd64/arm64 ; client Python Windows/Linux/
macOS (constitution XV), sans fenêtre (XVI).

**Project Type**: service Go existant + application Python existante ; modules
additifs et branchement progressif.

**Performance Goals**:
- `GET /v1/vpn/state` p95 < 100 ms ; `capabilities` < 20 ms.
- Bascule même transport p95 ≤ 8 s, p99 ≤ 20 s ; changement de transport p95
  ≤ 20 s, p99 ≤ 45 s.
- Ladder de transports : une tentative ≤ 30 s (WG/UDP) ou ≤ 45 s (TCP) ; un
  cycle complet ≤ 105 s ; retour au premier transport après backoff ≤ 300 s.
- Vérification d'egress p95 ≤ 3 s (cache hit < 10 ms) ; failover DNS p95 ≤ 2 s.
- SSE < 1 s ; rotation ≤ 3 candidats et ≤ 30 s p95.

**Constraints**:
- Exactement 6 stations (`station_count=6`, `target_slots=6`) ; aucune session ou
  station supplémentaire.
- Free uniquement ; aucune référence payante, second compte ou attente.
- Quota par IP ; les sondes n'appellent jamais l'API modèle ; canary neutre ≤ 1
  requête par vérification.
- Le fork ne possède ni quota, ni claim, ni score : il exécute et prouve.
- Une seule vérité par cycle de vie : tunnel/settings/DNS/egress au fork ;
  IP/lease/quota/SLO à l'orchestrateur.
- Pas de double tunnel dans le même conteneur (D2) ; le make-before-break
  inter-stations reste la feature 015.
- Le ladder de transports ne change que le transport : jamais le serveur, l'IP
  ou le hostname ; un seul tunnel actif à la fois.
- Modules Go < 1 500 lignes, domaine pur, pas de cycle d'import.
- Rétro-compatibilité des routes et variables ; nouveautés additives.
- Jamais d'état global « plus d'IP » : `exhausted` local et structuré.

**Scale/Scope**: 6 stations, milliers de serveurs NordVPN, 3 transports, ladder
2–3 étages, 3–6 sources d'egress, 30 jours d'historique borné, ≤ 100 jobs/jour/
station, 24 h d'idempotence.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principe | Vérification | Statut |
|---|---|---|
| I. Disponibilité mesurée | Le fork expose observations et événements horodatés ; `client_usable` reste calculé par 015 depuis `registry.db`. | PASS |
| II. Trois ressources | Tunnel/station au fork ; session et IP restent des objets de l'orchestrateur ; aucun cooldown d'IP dans le fork. | PASS |
| III. Une source de vérité | `state.json` ne décrit que le tunnel ; `registry.db` reste l'autorité ; les événements sont des faits ingérés. | PASS |
| IV. Reset fournisseur | Le fork ne sonde pas le quota modèle et n'utilise pas `reset_at`. | PASS |
| V. IP unique par station | Le fork filtre localement (`exclude_ips`, `require_new_ip`) mais le claim exclusif reste dans `registry.db`. | PASS |
| VI. Indépendance | Un job par instance, aucun verrou inter-stations, aucun état mutable partagé. | PASS |
| VII. Aucune preuve, aucun succès | Commit sur consensus k-of-n + canary 200 avec contenu uniquement. | PASS |
| VIII. Un seul réparateur | L'orchestrateur décide ; le fork ne s'auto-rotationne pas sans ordre. | PASS |
| IX. Score ordonne, portes dures | Le fork ne re-score jamais ; l'ordre reçu est exécuté tel quel. | PASS |
| X. Dégradation explicite, free | `exhausted`, `failed_degraded`, `rolled_back` explicites ; aucun état fabriqué, aucun payant. | PASS |
| XI. Fallbacks multiples | DNS multi-étages, egress multi-sources, rotation multi-candidats, rollback, legacy. | PASS |
| XII. Option GUI = effet réel | Chaque variable `CONTROL_*`/`DNS_*` a un consommateur, un test et une entrée `capabilities`. | PASS |
| XIII. Données traçables | State versionné + checksum, jobs/events append-only, rétention, provenance. | PASS |
| XIV. Modularité, strangler | Packages `internal/control/*`, `internal/egress/*`, `internal/dns/ladder` ; < 1 500 lignes ; legacy conservé puis décommissionné. | PASS |
| XV. Multiplateforme | Fork Linux par nature ; client Python cross-platform sans chemin OS en dur. | PASS |
| XVI. Silencieux | Client HTTP pur, aucun sous-processus, aucune fenêtre. | PASS |
| XVII. Gates qualité | `go test`, `go vet`, `golangci-lint`, contrats, `pytest`, chaos, soak, non-régression 015. | PASS |

Aucune violation ; deux choix structurants sont justifiés dans « Complexity
Tracking ».

## Architecture

### Frontière de propriété

```text
┌──────────────────────────── orchestrateur Python (autorité) ───────────────────────────┐
│ registry.db : IP, leases, quotas, claims, score composite, SLO, incidents               │
│ rotation.py / warm_pool / superviseur : décide QUEL candidat, QUAND, pour QUELLE station│
│ feature 015 : standby inter-stations, make-before-break, verdict client_usable          │
└───────────────────────────────────────┬────────────────────────────────────────────────┘
                                        │ HTTP /v1/control, /v1/vpn/{switch,rotate},
                                        │ /v1/egress, /v1/dns, /v1/events (SSE)
┌───────────────────────────────────────▼────────────────────────────────────────────────┐
│ fork Gluetun (agent par station)                                                        │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐  ┌─────────────────────────────┐  │
│  │ SwitchJob   │  │ Rotation     │  │ DNS ladder   │  │ Egress verification         │  │
│  │ state mach. │  │ executor     │  │ dot/doh/plain│  │ sources + consensus + canary│  │
│  └──────┬──────┘  └──────┬───────┘  └──────┬───────┘  └──────────────┬──────────────┘  │
│         │                │                 │                          │                 │
│  ┌──────▼────────────────▼─────────────────▼──────────────────────────▼──────────────┐  │
│  │ Settings snapshot/apply · VPN loop · firewall · proxies · publicip (existant)     │  │
│  │ Transport ladder WG→UDP→TCP · breaker · watchdog même-serveur                     │  │
│  └───────────────────────────────────────────────────────────────────────────────────┘  │
│  state.json · jobs.jsonl · events.jsonl · idempotency.json (atomique, borné)            │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

**Règle d'or** : le fork ne choisit pas un « meilleur » candidat ; il exécute un
ordre et prouve un fait. Toute logique de score, quota ou claim dans le fork est
un bug de frontière.

### Modules Go ajoutés

```text
internal/
├── control/
│   ├── api/        # /v1/control, /v1/vpn/{switch,rotate,jobs,rollback},
│   │               # /v1/servers, /v1/egress, /v1/dns, /v1/events
│   ├── auth/       # scopes reader/controller/admin
│   ├── jobs/       # machine à états, phases, idempotence, rotation
│   ├── state/      # DesiredState, snapshot, journal atomique, reprise
│   ├── events/     # bus, SSE hub, seq monotone, store borné
│   └── catalog/    # facettes, validation de cible, stats runtime
├── egress/
│   ├── source/     # HTTP echo, DNS whoami, publicip interne
│   ├── consensus/  # k-of-n, dissidents, confiance, exclusions
│   └── canary/     # 200 + contenu, hash, timeout
├── dns/
│   └── ladder/     # tiers, health, per-request fallback, fail-closed
└── vpn/
    └── transport/  # ladder WG→UDP→TCP, breaker, backoff, un seul tunnel
```

Les packages existants (`internal/vpn`, `internal/dns`, `internal/server`,
`internal/publicip`, `internal/provider`) sont étendus, pas réécrits. Les routes
sont montées dans `internal/server/handler.go` derrière
`CONTROL_PLANE_ENABLED` (défaut `on` dans l'image du fork).

### Flux d'une bascule

```text
POST /v1/vpn/switch
  ├─ scope controller (403 si reader)
  ├─ idempotence → job existant
  ├─ un seul job actif (409 + active_job_id)
  ├─ validating : cible vs catalogue, credentials, transport
  ├─ preflight : résolution, appartenance IP, TCP/UDP joignable — aucune coupure si échec
  ├─ snapshot : settings complets, serveur effectif, IP vérifiée, DNS state
  ├─ drain optionnel borné
  ├─ applying : SetSettings avec contexte annulable
  ├─ awaiting_tunnel : Running + handshake + interface up
  ├─ verifying_egress : consensus + canary (fresh)
  ├─ gates : require_new_ip / require_new_subnet / expected_egress_ip / canary
  ├─ committed : dernier commit persisté + egress.verified
  └─ échec : rollback snapshot → re-vérifie l'ancienne IP → rolled_back
             ou failed_degraded si l'ancien serveur est mort (pas de boucle)
```

### Flux rotation

```text
POST /v1/vpn/rotate
  ├─ candidats explicites[] (ordonnés, non re-scorés) OU politique → catalogue
  ├─ exclusions (hostnames, IPs, sous-réseaux), cooldown, budget
  ├─ pour chaque candidat : SwitchJob interne (un seul actif)
  │    ├─ succès + gates → committed, stop
  │    └─ échec → raison typée, candidat suivant, cooldown
  └─ budget épuisé → exhausted {attempts[], reasons[]} ; station conservée
```

### Flux du ladder de transports

```text
boucle locale (par station, sans orchestrateur)
  ├─ policy = TRANSPORT_LADDER (défaut wireguard,openvpn-udp,openvpn-tcp)
  ├─ pour transport in policy :
  │    ├─ breaker[transport] == open ? skip (sauf half_open dû)
  │    ├─ preflight(transport, serveur courant) : résolution, port, credentials
  │    ├─ teardown_current()        # aucun second tunnel
  │    ├─ apply(transport, même serveur/hostname/IP)
  │    ├─ awaiting_tunnel : interface + handshake + statut
  │    ├─ verifying_egress : consensus + canary
  │    └─ succès → commit, breaker.success ; échec → breaker.failure(raison), suivant
  ├─ cycle épuisé → transport.cycle_exhausted + backoff(jitter)
  └─ reprise à policy[0]            # retour au premier transport
```

Le ladder ne reçoit pas de liste de serveurs : il ne boucle que sur les
transports. Un `transport_policy` de job peut restreindre l'ordre ou forcer un
transport ; `VPN_TYPE` explicite désactive le ladder (compatibilité upstream).

### Décisions structurantes

| ID | Décision | Justification | Alternative rejetée |
|---|---|---|---|
| D1 | Le fork exécute une liste ordonnée et ne re-score jamais. | Constitution IX ; un seul classement dans `registry.db`. | Re-scorer en Go : double autorité, dérive. |
| D2 | Pas de second tunnel dans le conteneur ; bascule intra-station rapide et réversible ; make-before-break inter-stations via 015. | Un conteneur = un namespace réseau et un tunnel ; deux tunnels cassent firewall/proxies. | Double tunnel : refonte réseau majeure, hors périmètre. |
| D3 | Un seul job actif, machine à états persistée, `supersede` admin. | Évite courses et états inconnus. | Jobs concurrents : courses sur settings/interfaces. |
| D4 | State non secret + secrets par référence d'environnement ; AES-GCM si `CONTROL_SECRET_KEY`. | Pas de credentials en clair ; survit au restart sans fuite. | Persister tout le JSON : fuite disque. |
| D5 | DNS ladder par requête, un listener, cache/filter conservés au-dessus. | Bascule sans redémarrer le tunnel. | Un resolver par processus : healthcheck en boucle. |
| D6 | Consensus sur la valeur d'IP. | La géoloc ne prouve pas l'IP de sortie. | Consensus géographique upstream : faux positifs. |
| D7 | IP d'hôte capturée au boot via les interfaces. | Un écho avant tunnel peut renvoyer l'IP d'hôte. | Écho externe : dépendance et faux positif. |
| D8 | `exhausted` local, jamais d'état global. | La station reste réparable ; constitution X. | État global : gèle la flotte. |
| D9 | Idempotence 24 h, journal de jobs, SSE. | Les retries ne déclenchent pas deux bascules. | Sans idempotence : doubles bascules sur timeout. |
| D10 | Scopes `reader/controller/admin`, secrets masqués par défaut. | Réduire la surface ; `GET settings` expose déjà les credentials. | Rôles sans scope : lecture/action indistinguables. |
| D11 | `PUT /v1/vpn/settings` conservé ; `dry_run`/`wait`/`require_verified` routent vers SwitchJob. | Aucune régression ; migration progressive. | Remplacer la route : casse les clients. |
| D12 | Intégration Python par `control_plane.mode=legacy|shadow|active`, événements ingérés dans `registry.db`. | Strangler, retour arrière immédiat, mesure de parité. | Big-bang : risque sur 6 stations et le SLO. |
| D13 | Le ladder de transports (`wireguard,openvpn-udp,openvpn-tcp`) est une primitive locale du fork, avec breaker/backoff et retour au premier ; il ne change jamais de serveur/IP. | Bascule en quelques secondes sans recréation de conteneur ; autonomie locale (NFR-009) ; constitution IX respectée (aucun re-score). | Ladder Python/Docker : lenteur, recréations, deux autorités. |

### Phases d'implémentation

#### Phase 0 — Baseline, flags et harnais (3–4 j)

- Geler l'image au digest upstream ; ajouter `CONTROL_PLANE_ENABLED`,
  `CONTROL_STATE_DIR` sans changer le comportement par défaut.
- Harnais de contrat `/v1/vpn/status`, `/v1/vpn/settings`, `/v1/publicip/ip`,
  `/v1/dns/status` avant/après.
- Faux DNS DoT/plain, échos HTTP locaux, `vpn-wg-test`.
- CI Go Linux + image GHCR épinglée par digest.

**Sortie** : image publiée, zéro régression, flag inerte.

#### Phase 1 — Egress verification (P1, 4–5 j)

- Sources HTTP echo / DNS whoami / publicip interne ; timeouts, backoff, santé.
- Consensus k-of-n, normalisation, dissidents, confiance, rejets
  privé/loopback/link-local/CGNAT/multicast/host/forbidden.
- Canary 200 + corps non vide + hash optionnel.
- Endpoints `/v1/egress`, `/verify`, `/canary`, `/history` ; cache 10 s,
  single-flight, capture de l'IP d'hôte au boot.

**Sortie** : preuve avec quorum et canary ; chaos source menteuse/IP d'hôte/corps
vide.

#### Phase 2 — DNS ladder (P1, 5–6 j)

- `Tier`/`Ladder` : ordre, sélection par requête, retry TCP sur TC, fallback sur
  timeout/SERVFAIL/REFUSED uniquement, `NXDOMAIN` terminal, fail-closed.
- Intégration dans `internal/dns` : cache, filtre, DNS local conservés ; un seul
  listener ; métriques par étage/endpoint.
- `DNS_LADDER`, `DNS_TIER_PROVIDERS_*`, `DNS_HEALTH_*` ; défaut flotte
  `dot,plain` (DoH configurable, non prioritaire).
- Endpoints `/v1/dns/state`, `/v1/dns/tiers`, `POST /v1/dns/probe`.

**Sortie** : blocage TCP/853 → plain ≤ 2 s ; tout down → `SERVFAIL` +
`fail_closed`, zéro requête hors tunnel.

#### Phase 2b — Ladder de transports (P1, 5–6 j, parallèle à P2/P3)

- `internal/vpn/transport` : `Policy` (ordre, budgets par transport, seuils de
  breaker, backoff/jitter), `Attempt` (raison typée), `State`, `Breaker`.
- Boucle : preflight → teardown → apply même serveur → tunnel → egress →
  commit/échec → transport suivant → cycle épuisé → backoff → `policy[0]`.
- Un seul tunnel garanti : teardown explicite, verrou de boucle, firewall
  réappliqué et MTU/PMTUD recalculés avant `up`.
- Watchdog même-serveur (interface, handshake, statut, DNS local) avec
  `local_reconnect_max` puis `tunnel.repair_needed`.
- Endpoints `GET /v1/vpn/transport`, `POST /v1/vpn/transport/reset` (admin) ;
  `transport_policy` accepté par `switch`/`rotate` ; événements et métriques par
  transport.
- Compatibilité : `VPN_TYPE` explicite = un seul transport, ladder désactivé.
- Le ladder Python de 015 passe en shadow (observation seule, jamais un second
  tunnel) avant décommissionnement.

**Sortie** : UDP bloqué → TCP en ≤ 2 tentatives ; cycle complet → retour à
WireGuard après backoff ; zéro double tunnel ; zéro recréation Compose.

#### Phase 3 — SwitchJob, état, rollback (P1, 6–8 j)

- `DesiredState` versionné, snapshot, journal atomique, checksum, reprise,
  `CONTROL_STATE_MODE=state|env|merge`.
- Machine à états, phases, timings, tentatives, idempotence, cancel, supersede.
- `POST /v1/vpn/switch`, `/rollback`, `GET /v1/vpn/state`, `GET /v1/vpn/jobs[/{id}]`,
  `POST /v1/vpn/jobs/{id}/cancel`.
- Préflight catalogue/credentials/résolution/appartenance/TCP-UDP.
- Rollback snapshot + re-vérification ; `failed_degraded` si impossible.
- Compatibilité `PUT /v1/vpn/settings` avec `dry_run`/`wait`/`require_verified`.

**Sortie** : bascule déterministe, rollback prouvé, état restauré après restart.

#### Phase 4 — Catalogue et rotation (P1, 4–5 j)

- Facettes pays/régions/villes/ISP/catégories/tiers, pagination, détail + stats,
  validation d'IP d'endpoint.
- `POST /v1/vpn/rotate` : candidats ou politique, exclusions, `require_new_ip`,
  `require_new_subnet`, `expected_egress_ip`, budgets, timeouts.
- Cooldown par hostname, backoff, `exhausted` structuré.
- `GET /v1/servers`, `/v1/servers/{hostname}`, `/v1/servers/facets`,
  `POST /v1/servers/refresh` (admin, rate-limité).

**Sortie** : 2 échecs puis succès au 3e ; 3 échecs → `exhausted` sans
désactivation ; aucun re-classement.

#### Phase 5 — Événements, sécurité, observabilité (P2, 4–5 j)

- Bus + SSE `Last-Event-ID`, rétention, `GET /v1/events`, `/v1/events/stream`.
- Scopes par route, `reveal=true` admin, masquage, limites de corps, rate-limit
  des switches, logs sans credentials.
- Métriques `gluetun_control_job_*`, `gluetun_dns_tier_*`,
  `gluetun_egress_source_*`, `gluetun_control_jobs_total`.
- `GET /v1/control/capabilities` + documentation.

**Sortie** : état durable, événements consommables, 403 sur scope insuffisant,
métriques visibles.

#### Phase 6 — Intégration orchestrateur et déploiement (P1, 6–8 j)

- `free/ip_registry/adapters/gluetun_control_v2.py` : client typé, retries
  bornés, idempotence, JSON, pas de `docker exec`.
- `config.yaml` : `control_plane.mode`, `base_url`, `api_key`, timeouts.
- Mode `shadow` ≥ 24 h : observation, décision legacy, comparaison IP/job/gates.
- Mode `active` station par station (6 → 1), flag par station.
- Ingestion `egress.verified`, `job.*`, `dns.*` dans `registry.db`, sans double
  comptage.
- Décommissionnement du chemin chaud `docker exec`/parsing `running`, gardé en
  secours froid.

**Sortie** : ≥ 95 % des bascules par l'API ; six stations `client_usable` pendant
le rollout ; fallback legacy testé.

### Ordre et dépendances

```text
P0 ──► P1 ──► P3 ──► P4
        │      │
        └──► P2┘
               └──► P2b ──► P5 ──► P6
```

P1 et P2 sont parallélisables. P2b dépend de P1 (preuve d'egress) et réutilise
la boucle VPN ; il peut démarrer dès P1. P3 dépend de P1+P2b. P4 dépend de P3.
P5 dès P3. P6 dépend de P3+P4+P5+P2b.

## Project Structure

### Documentation

```text
specs/016-gluetun-control-plane/
├── spec.md
├── plan.md
├── research.md
├── audit.md
├── data-model.md
├── quickstart.md
├── tasks.md
├── checklist.md
└── contracts/
    ├── control-api.md
    ├── dns-ladder.md
    ├── egress-verification.md
    ├── transport-ladder.md
    └── orchestrator-integration.md
```

### Code du fork (`jul-ienfr/gluetun`)

```text
cmd/gluetun/main.go                     # montage du plan de contrôle
internal/
├── control/{api,auth,jobs,state,events,catalog}/
├── egress/{source,consensus,canary}/
├── dns/ladder/
├── vpn/transport/                      # ladder WG→UDP→TCP, breaker, watchdog
├── server/                             # handlers existants étendus
└── vpn/                                # loop/state existants utilisés
tests/{contract,integration,unit}/
```

### Code du projet (Python)

```text
free/ip_registry/adapters/
├── gluetun_control_v2.py        # nouveau client HTTP
├── gluetun_control.py           # legacy conservé (chemin froid)
└── egress.py                    # couche /v1/egress/verify
free/control_plane/
├── client.py                    # wrapper typé + retries + idempotence
├── shadow.py                    # comparaison legacy/nouveau
└── events.py                    # ingestion registry.db
tests/
├── test_gluetun_control_v2.py
├── test_control_plane_shadow.py
└── test_control_plane_events.py
```

**Structure Decision** : deux dépôts, deux cycles de vie. La frontière est un
contrat HTTP versionné, pas un import de code. Les modules Go sont regroupés par
domaine pour rester < 1 500 lignes et éviter les cycles.

## Testing Strategy

### Unitaire (fork)

- Consensus : table de vérité k-of-n, dissidents, tie-break, IPv4/IPv6, rejets.
- Ladder : ordre, timeout, SERVFAIL, REFUSED, NXDOMAIN, TC, cooldown,
  récupération, fail-closed.
- Jobs : transitions, annulation, idempotence, crash/reprise, rollback,
  `failed_degraded`.
- Transports : ordre du ladder, budget par transport, breaker
  closed→open→half_open, backoff/jitter, retour à `policy[0]`, raisons typées
  (`auth`, `udp_blocked`, `tcp_refused`, `tls`, `handshake_timeout`,
  `no_egress`, `interface_down`), un seul tunnel, `VPN_TYPE` explicite.
- State : atomicité, checksum, corruption, modes `state|env|merge`.
- Catalogue : facettes, validation hostname/IP, pagination.

### Contrat

- Routes upstream inchangées : `/version`, `/v1/vpn/status`, `/v1/vpn/settings`
  (masqué), `/v1/dns/status`, `/v1/publicip/ip`.
- Nouvelles routes : schémas, 400/401/403/404/409/422/429/500,
  `Idempotency-Key`, `Last-Event-ID`.

### Intégration

- Conteneur de test + `vpn-wg-test` + faux DNS + échos locaux.
- Switch heureux, hostname inconnu, échec de preuve → rollback, rollback
  impossible → `failed_degraded`, rotation 3 candidats, DNS bloqué, source
  menteuse, IP d'hôte, state corrompu, restart pendant job.
- Ladder de transports : UDP bloqué → TCP, TCP refusé → retour WG après cycle,
  auth invalide → pas de boucle infinie, handshake qui meurt après commit →
  watchdog même-serveur, aucun double tunnel observé (`ip link`).

### Projet

- Parité shadow : 100 bascules legacy vs nouveau, même IP finale, mêmes gates.
- Non-régression `pytest`, `vpn/manager.py`, dashboard, Compose binder.
- Chaos flotte 1→6, aucune 7e session, cinq stations restent `client_usable`.
- Soak 24 h : 0 job bloqué, 0 fuite DNS, 0 secret loggé.

## Rollout et rollback

1. **Image** : build multi-arch, scan, GHCR, épinglage par digest station par
   station.
2. **Flags** : `CONTROL_PLANE_ENABLED=on`, `CONTROL_STATE_MODE=state`,
   `DNS_LADDER=dot,plain`, `CONTROL_EGRESS_QUORUM=2`,
   `TRANSPORT_LADDER=wireguard,openvpn-udp,openvpn-tcp`.
3. **Canary** : station 6 puis 5… une seule à la fois ; vérifier
   `client_usable`, IP et événements avant la suivante.
4. **Shadow Python** : `mode=shadow` ≥ 24 h ; zéro divergence sur l'IP finale et
   les gates.
5. **Active** : `mode=active` station par station ; legacy utilisé si l'API
   répond 5xx/timeout.
6. **Rollback** : repin du digest précédent, `CONTROL_STATE_MODE=env` si state
   douteux, `mode=legacy` côté Python ; jobs en cours marqués `interrupted`.

## Risks

| ID | Risque | Prob. | Impact | Mitigation |
|---|---|---|---|---|
| R-01 | Divergence avec l'upstream | Moyenne | Moyen | Modules isolés, patchs minimaux, contrats, rebase trimestriel. |
| R-02 | `qdm12/dns/v2` insuffisant pour le fallback par requête | Moyenne | Élevé | Ladder au-dessus de `server.Dialer` ; petit fork testé si nécessaire. |
| R-03 | Failover DNS > 2 s | Moyenne | Moyen | Timeout 800 ms/étage, hedged request bornée, cache négatif. |
| R-04 | Rollback vers serveur mort | Moyenne | Élevé | Préflight du snapshot ; sinon `failed_degraded`, pas de boucle. |
| R-05 | State corrompu / disque plein | Faible | Élevé | Écriture atomique, checksum, `state.invalid`, fallback env. |
| R-06 | Fuite de credentials | Faible | Critique | Masquage, `reveal` admin, tests de sérialisation, revue logs. |
| R-07 | Double propriété rotation | Moyenne | Élevé | Frontière D1/D12, shadow, décommissionnement explicite. |
| R-08 | Rate-limit provider catalogue | Faible | Moyen | Refresh admin rate-limité, cache. |
| R-09 | Tests réseau flaky | Moyenne | Faible | Faux DNS/échos locaux, marquage `integration`, timeouts. |
| R-10 | Régression 015 / 6 stations | Faible | Critique | Rollout 1→6, parité shadow, mesure avant/après, rollback digest. |
| R-11 | Ladder qui boucle ou crée deux tunnels | Faible | Critique | Verrou de boucle, teardown explicite, test `ip link`, breaker + backoff borné, `max_cycles` puis arrêt. |
| R-12 | Mauvais classement d'erreur (auth vs réseau) | Moyenne | Moyen | Raisons typées, tests par code, `tunnel.repair_needed` après budget ; jamais de boucle sur `auth`. |

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Journal de jobs + state file | Le state décrit l'état courant ; le journal permet reprise, audit et idempotence après crash. | Un seul state : impossible de rejouer un job interrompu. |
| Couche `dns/ladder` au-dessus du resolver | Fallback par requête et santé multi-étages absents de l'upstream. | Mono-étage : point de panne unique déjà observé (DoH bloqué). |
| Boucle `vpn/transport` avec breaker | Le ladder est local, borné et sans second tunnel ; il évite les recréations Compose et les deux autorités. | Ladder Python/Docker : lent, fragile, et ne garantit pas l'unicité du tunnel. |

Aucune autre violation ; modules < 1 500 lignes, domaine testable sans I/O.
