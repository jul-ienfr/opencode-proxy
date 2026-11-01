# Plan d’architecture — Registre d’IP NordVPN et allocateur de stations

Statut : proposition à valider  
Portée : jambe free, sélection des IP, binding serveur, allocation aux stations  
Objectif : fiabilité, qualité, performance, maintenabilité, modularité

---

## 1. Objectif et invariants

Le système doit :

1. **Inventorier toutes les IP/serveurs NordVPN disponibles**, sauf lorsqu’une
   géolocalisation est obligatoire.
2. **Tester, présélectionner et classer** les IP de la meilleure à la moins bonne.
3. **Ne tester que les stations qui en ont besoin** : si toutes les stations sont
   connectées et fonctionnelles, aucun test n’est lancé.
4. Si une station a besoin d’une IP, **consommer le classement** et tester les
   candidates une par une jusqu’à en trouver une qui fonctionne.
5. **Enregistrer chaque IP testée** avec sa date, son statut, son `Retry-After`
   et sa date de reset.
6. **Exclure** les IP dont le quota n’est pas bon à la date courante (reset pas
   encore atteint, cooldown d’erreur, IP déjà assignée).
7. **Conserver une bonne IP dans sa station** et l’utiliser jusqu’à la prochaine
   rotation, au prochain cycle, ou jusqu’à échec réel.

### Invariants non négociables

- Une IP (egress observé) n’est assignée qu’à **une seule station** à la fois.
- Aucune IP `429` n’est retestée avant son `reset_at`.
- Aucune IP `error` n’est retestée avant la fin de son cooldown.
- Un `200` fige la station sur l’IP jusqu’à libération explicite.
- Aucun test n’est lancé si toutes les stations sont saines.
- Toute décision est persistée avant l’action (claim/lease) et après le résultat.
- Le système est **crash-safe** : un redémarrage ne laisse ni lease orphelin ni
  double assignation.

---

## 2. Contraintes et découvertes

### 2.1 NordVPN ne choisit pas une IP, il choisit un serveur

L’API NordVPN expose ~7 232 serveurs (`hostname`, `station`, pays, charge).
L’IP publique observée peut être une IP NAT différente du `station` :

```text
serveur 87.120.102.67:51820  →  egress 87.120.102.76
```

Conséquence : le registre doit stocker **le serveur** (pour y revenir) et
**l’egress observé** (clé du quota).

### 2.2 Le quota est par IP, pas par compte

Le `429` contient un `Retry-After` qui varie par IP (00:01 → 00:53 UTC observés).
`reset_at = observation + Retry-After` est la seule source de vérité.

### 2.3 Le verrou technique : binder un serveur précis

Aujourd’hui le manager pin un **pays** ; gluetun choisit un serveur au hasard.
C’est ce qui a fait perdre `.57` et `.13`. Pour que le classement survive, il
faut pouvoir dire à une station : « reconnecte-toi à **ce hostname précis** ».

Options à valider par spike :

1. `SERVER_HOSTNAMES` par station dans Compose + recréation du conteneur.
2. `PUT /v1/vpn/settings` avec `hostnames` + `countries` (si gluetun l’accepte
   en WireGuard).
3. Config WireGuard custom par serveur (`VPN_SERVICE_PROVIDER=custom`).

Le binder est un **port** avec plusieurs adaptateurs ; le reste du système ne
dépend pas de la méthode.

---

## 3. Architecture cible

```text
┌──────────────────────────────────────────────────────────────────┐
│                        Control Plane                             │
│  Dashboard / API  ────────────────►  Allocator Service           │
│                                      │  needs / claims / leases  │
└──────────────────────────────────────┼───────────────────────────┘
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        │                              ▼                              │
        │  ┌────────────────┐   ┌──────────────┐   ┌───────────────┐  │
        │  │  ServerCatalog │   │ IpRegistry   │   │  IpRanker     │  │
        │  │  (NordVPN API) │   │ (SQLite)     │   │ score/elig.   │  │
        │  └────────────────┘   └──────────────┘   └───────────────┘  │
        │                              │                              │
        │                     ┌────────▼────────┐                     │
        │                     │  StationBinder  │                     │
        │                     │  (hostname/WG)  │                     │
        │                     └────────┬────────┘                     │
        │                              │                              │
        │                     ┌────────▼────────┐                     │
        │                     │  ProbeClient    │                     │
        │                     │  (official TLS) │                     │
        │                     └────────┬────────┘                     │
        │                              │                              │
        │                     ┌────────▼────────┐                     │
        │                     │  StationHealth  │                     │
        │                     │  (pool/watchdog)│                     │
        │                     └─────────────────┘                     │
        └──────────────────────────────────────────────────────────────┘
```

### Modules

| Module | Rôle |
|---|---|
| `ServerCatalog` | Import/refresh des serveurs NordVPN, pays, charge, technologies |
| `IpRegistry` | Persistance des observations, états, scores, assignations |
| `IpRanker` | Calcul du score et de l’éligibilité |
| `IpAllocator` | Détecte les besoins, claim, test, assigne/libère |
| `StationBinder` | Reconnecte une station à un hostname précis |
| `ProbeClient` | Teste le corps officiel free, mesure statut/latence |
| `StationHealth` | Source de vérité sur l’état des stations |
| `Scheduler` | Événements, réconciliation, leases expirés |
| `GeoPolicy` | Filtre les pays autorisés si géo obligatoire |
| `Api/Dashboard` | Lecture, actions manuelles, observabilité |

---

## 4. Modèle de données

### 4.1 SQLite (`logs/ip_registry.db`)

SQLite WAL, un seul writer applicatif, transactions courtes.

#### Table `servers` (catalogue)

```sql
CREATE TABLE servers (
  hostname        TEXT PRIMARY KEY,
  server_ip       TEXT,
  country         TEXT NOT NULL,
  city            TEXT,
  load            REAL,
  technologies    TEXT,             -- JSON: ["wireguard","openvpn","socks"]
  first_seen_at   REAL NOT NULL,
  last_seen_at    REAL NOT NULL,
  active          INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_servers_country ON servers(country, active);
```

#### Table `observations` (historique des tests)

```sql
CREATE TABLE observations (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  hostname        TEXT NOT NULL REFERENCES servers(hostname),
  egress_ip       TEXT,
  station         INTEGER,
  tested_at       REAL NOT NULL,
  status          INTEGER NOT NULL,       -- 200, 429, 0 (erreur)
  retry_after_s   REAL NOT NULL DEFAULT 0,
  reset_at        REAL,
  latency_ms      REAL,
  error           TEXT,
  attempt_id      TEXT NOT NULL
);
CREATE INDEX idx_obs_hostname_time ON observations(hostname, tested_at DESC);
CREATE INDEX idx_obs_egress_time ON observations(egress_ip, tested_at DESC);
```

#### Table `ip_state` (vue matérialisée pour la sélection)

```sql
CREATE TABLE ip_state (
  egress_ip       TEXT PRIMARY KEY,
  hostname        TEXT NOT NULL,
  country         TEXT NOT NULL,
  state           TEXT NOT NULL,           -- fresh|candidate|claimed|assigned|exhausted|error|retired
  score           REAL NOT NULL DEFAULT 0,
  last_status     INTEGER,
  last_tested_at  REAL,
  last_ok_at      REAL,
  reset_at        REAL,
  error_until     REAL,
  latency_ms      REAL,
  tests           INTEGER NOT NULL DEFAULT 0,
  successes       INTEGER NOT NULL DEFAULT 0,
  failures        INTEGER NOT NULL DEFAULT 0,
  assigned_station INTEGER,
  assigned_at     REAL,
  lease_id        TEXT,
  lease_expires_at REAL,
  updated_at      REAL NOT NULL
);
CREATE INDEX idx_state_eligible ON ip_state(state, reset_at, error_until, score DESC);
CREATE INDEX idx_state_country ON ip_state(country, state, score DESC);
CREATE INDEX idx_state_station ON ip_state(assigned_station);
```

#### Table `assignments` (journal)

```sql
CREATE TABLE assignments (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  egress_ip       TEXT NOT NULL,
  hostname        TEXT NOT NULL,
  station         INTEGER NOT NULL,
  assigned_at     REAL NOT NULL,
  released_at     REAL,
  reason          TEXT,                     -- failure|rotation|cycle|manual
  result          TEXT                      -- 200/429/error
);
CREATE INDEX idx_assign_station ON assignments(station, assigned_at DESC);
```

### 4.2 États d’une IP

```text
fresh ──► candidate ──► claimed ──► assigned
  │           │            │
  │           │            └──► released ──► candidate / exhausted / error
  │           └──► exhausted (429, reset_at futur)
  └──► error (cooldown)
```

Règles :

- `fresh` : jamais testée, score neutre.
- `candidate` : testée, éligible, non assignée.
- `claimed` : lease court pendant un test (TTL 5 min).
- `assigned` : conservée par une station.
- `exhausted` : `429`, exclue jusqu’à `reset_at`.
- `error` : tunnel/DNS, exclue jusqu’à `error_until`.
- `retired` : serveur disparu du catalogue.

---

## 5. Catalogue NordVPN

### 5.1 Import

- Source : `GET https://api.nordvpn.com/v1/servers?limit=0`.
- Filtre : `status=online`.
- Champs : `hostname`, `station`, pays, ville, `load`, technologies.
- Refresh : toutes les 6 h (config), ou manuel.
- Échec API : garder le dernier catalogue, logguer, ne pas bloquer les stations.

### 5.2 IP « disponibles »

Le catalogue contient des **serveurs**, pas des egress. Une IP n’est connue
qu’après un test. On distingue :

- `server_ip` : IP du endpoint ;
- `egress_ip` : IP publique observée après connexion.

La sélection se fait sur `egress_ip` ; le binding se fait sur `hostname`.

---

## 6. Score et éligibilité

### 6.1 Score (0–100)

Formule explicable et configurable :

```text
availability = (successes + 1) / (tests + 2)          # Laplace
latency_score = 1 - min(1, p50_latency_ms / budget_ms)
freshness = exp(-age_h / freshness_ttl_h)

score = 100 * (
    w_avail   * availability +
    w_latency * latency_score +
    w_fresh   * freshness
) - load_penalty
```

Valeurs par défaut :

- `w_avail = 0.55`
- `w_latency = 0.25`
- `w_fresh = 0.20`
- `budget_ms = 8 000`
- `freshness_ttl_h = 48`
- `load_penalty = min(10, load / 10)`

Cas spéciaux :

- `429` : score `0` tant que `now < reset_at` ; après reset, retour à un score
  neutre (`40`) jusqu’à retest.
- `error` : score `5` pendant le cooldown ; après, `30`.
- `200` : score calculé normalement, `last_ok_at` mis à jour.
- `fresh` : score `50` (neutre), sauf heuristique pays/charge.

### 6.2 Éligibilité

```text
eligible(ip, now, geo):
  state in ("fresh", "candidate")
  and (reset_at IS NULL OR now >= reset_at)
  and (error_until IS NULL OR now >= error_until)
  and (geo is NULL OR country IN geo.allowed)
  and (assigned_station IS NULL)
  and score >= min_score
```

Une IP `assigned` n’est jamais candidate : elle reste dans sa station.
Une IP `claimed` est exclue tant que le lease est valide.

### 6.3 Classement

`ORDER BY score DESC, latency_ms ASC, last_tested_at DESC`.
Le classement est calculé à la volée depuis `ip_state` (index couvrant).

---

## 7. Allocation et cycle de vie

### 7.1 Détection des besoins

```text
need(station) =
    station absente
    ou tunnel non connecté
    ou IP absente
    ou dernier probe != 200
    ou 429 sur l’IP courante
    ou rotation demandée
    ou cycle de renouvellement
```

Si `needs` est vide → **aucun test**.

### 7.2 Boucle d’allocation

```text
pour chaque station dans needs:
    candidats = registry.eligible(geo=station.geo)
    pour ip dans rank(candidats):
        if not registry.claim(ip, station, ttl=300s):
            continue
        bind = binder.assign(station, ip.hostname)
        if not bind.ok:
            registry.release(ip, reason="bind_error")
            continue
        result = probe.test(station)
        registry.record(ip, result, tested_at=now)
        if result.status == 200:
            registry.assign(ip, station)
            station.freeze(ip)
            break
        registry.release(ip, reason=result.reason)
        # 429 -> exhausted(reset_at) ; erreur -> error(cooldown)
```

### 7.3 Règles de conservation

- Une IP `assigned` avec un `200` reste sur la station.
- Elle n’est libérée que si :
  - probe `429` / erreur répétée ;
  - rotation manuelle ou planifiée ;
  - cycle de renouvellement ;
  - station désactivée.
- À la libération :
  - `429` → `exhausted` + `reset_at` ;
  - erreur → `error` + cooldown ;
  - `200` → `candidate` avec score mis à jour.

### 7.4 Concurrence et leases

- `claim` = transaction SQLite `BEGIN IMMEDIATE` :
  - vérifie l’éligibilité ;
  - pose `lease_id`, `lease_expires_at`, `state=claimed`.
- Un seul writer ; les tests sont en dehors de la transaction.
- Lease expiré → `reconcile()` le libère au démarrage et périodiquement.
- `attempt_id` unique pour l’idempotence et les logs.

### 7.5 Backoff

- Échec de test : 3 tentatives par candidate maximum.
- Entre candidates : backoff `[1s, 2s, 5s, 10s]` + jitter.
- Erreur de binder : cooldown de la station, pas de l’IP.
- Limite globale : `max_test_concurrency = 2` (config).

---

## 8. Binding serveur (critique)

### 8.1 Port

```python
class StationBinder(Protocol):
    async def assign(self, station: int, hostname: str) -> BindResult: ...
    async def release(self, station: int) -> None: ...
    async def current(self, station: int) -> str | None: ...
```

### 8.2 Adaptateurs

1. **ComposeHostnameBinder**
   - `SERVER_HOSTNAMES_STATIONn=<hostname>` dans Compose.
   - recréation du conteneur de la station ;
   - attente `running` + vérification egress.
2. **ControlApiBinder**
   - `PUT /v1/vpn/settings` avec `countries` + `hostnames`.
   - à valider en WireGuard ; fallback sinon.
3. **CustomWireguardBinder**
   - config WG par serveur (endpoint, clés) ;
   - conteneur `VPN_SERVICE_PROVIDER=custom` ;
   - le plus déterministe, mais demande la génération des configs.

### 8.3 Vérification

Après `assign` :

- attendre le tunnel (`status=running`) ;
- mesurer l’egress réel ;
- si l’egress diffère de `ip.egress_ip`, mettre à jour l’observation ;
- si le hostname n’est pas joignable, marquer l’IP `error` et essayer la suivante.

### 8.4 Geo

- Si `geo` obligatoire : filtrer `country` avant le ranking.
- Le binder reçoit le hostname ; le pays est déjà cohérent.
- En cas de conflit geo/binder : refuser l’assignation (fail-closed).

---

## 9. Ordonnanceur

- **Événementiel** : health station, rotation, API manuelle.
- **Réconciliation** toutes les 60 s : leases expirés, stations orphelines,
  assigned IP absentes du catalogue.
- **Pas de scan continu** : le scan complet n’est lancé que si
  `allocator.preselect.enabled` et aucune station n’a besoin (option).
- **Refresh catalogue** toutes les 6 h.
- **Cycle** quotidien optionnel : libérer les IP assignées et re-scorer.

---

## 10. API et dashboard

### Endpoints

```text
GET  /api/free/ip-registry?state=&country=&limit=&offset=
GET  /api/free/ip-registry/{egress_ip}
POST /api/free/ip-registry/refresh
POST /api/free/ip-registry/{egress_ip}/test
GET  /api/free/allocator/status
POST /api/free/allocator/run
POST /api/free/allocator/release   { station }
GET  /api/free/allocator/assignments
```

Auth : `X-Dashboard-Token` si `DASHBOARD_TOKEN` défini (comme les endpoints
existants).

### Dashboard

- Table classée : score, IP, serveur, pays, état, dernier test, reset, station.
- Filtres : état, pays, score, assignée.
- Actions : tester, assigner, libérer, rafraîchir le catalogue.
- Badge « toutes les stations saines » / « N stations en attente ».
- Compte à rebours des resets.

---

## 11. Observabilité

### Métriques

```text
ip_registry_servers_total
ip_registry_ips_total{state}
ip_registry_eligible_total{country}
ip_registry_tests_total{result}
ip_registry_test_duration_ms
ip_registry_429_total
ip_registry_reset_soon_total
allocator_needs_total
allocator_claim_conflicts_total
allocator_assignments_total{result}
allocator_lease_expired_total
station_ip_age_seconds{station}
```

### Logs structurés

```json
{
  "event": "ip_test",
  "attempt_id": "...",
  "station": 6,
  "hostname": "al109.nordvpn.com",
  "egress_ip": "87.120.102.53",
  "status": 200,
  "latency_ms": 3200,
  "reset_at": null
}
```

### Alertes

- Aucune IP éligible pour une station depuis > 15 min.
- Taux de 429 > 80 % sur 30 min.
- Lease expiré > 3 en 10 min.
- Catalogue vide / API NordVPN en échec > 1 h.
- Toutes les stations saines mais tests en cours (anomalie).

---

## 12. Performance

- SQLite WAL + index ; requête d’éligibilité < 50 ms pour 10k lignes.
- Catalogue en mémoire (lecture seule), refresh en tâche de fond.
- Tests lazy : seuls les besoins déclenchent des probes.
- Concurrence : 1 test par station, 2–3 globaux.
- Cache des sessions curl_cffi officielles par IP (existant).
- Pas de body SSE complet : lire les headers puis fermer.
- Batch d’écritures : une transaction par résultat, pas par ligne de log.
- Budget de scan : `max_tests_per_hour` configurable.

---

## 13. Fiabilité et reprise

- **Lease** : TTL 5 min, renouvelé pendant le test ; expiré → libéré.
- **Idempotence** : `attempt_id` unique ; un retry ne crée pas de doublon.
- **Transactions** : `BEGIN IMMEDIATE` pour claim/assign/release.
- **Crash** : au démarrage, `reconcile()` libère les `claimed` expirés et
  vérifie que les `assigned` sont toujours sur la bonne station.
- **Circuit breaker** : API NordVPN, binder, probe.
- **Fail-open** de la lecture du registre : si la DB est indisponible, le pool
  retombe sur l’ancienne sélection (feature flag) pour ne pas couper le service.
- **Fail-closed geo** : jamais d’IP hors pays autorisé.
- **Sauvegarde** : copie quotidienne de la DB + rétention 7 jours.

---

## 14. Sécurité

- `NORDVPN_TOKEN` et credentials uniquement en env, jamais loggués.
- API dashboard protégée par token.
- Aucune IP client/paid dans la jambe free.
- Les probes utilisent le corps officiel et l’identité par IP existants.
- Limite de débit sur les endpoints manuels.
- Audit des assignations/libérations dans `assignments`.

---

## 15. Tests

### Unitaires

- Scoring : 200/429/error/fresh, bornes, poids, latence.
- Éligibilité : reset futur, cooldown, geo, assigned, claimed.
- State machine : transitions autorisées/interdites.
- Lease : expiration, concurrence, idempotence.

### Propriétés (property-based)

- Jamais deux stations avec la même IP assignée.
- Jamais de test d’une IP avant `reset_at`.
- Un `200` ne peut pas être libéré sans raison.
- Le score reste dans [0, 100].

### Intégration

- Faux catalogue NordVPN (7k entrées).
- Faux gluetun/control API.
- Faux probe (200/429/error/timeout).
- SQLite temporaire, WAL, crash simulé.

### Contrats

- Compose : `SERVER_HOSTNAMES_STATIONn` bien injecté.
- API : schémas JSON, codes d’erreur, auth.
- Migration : ancien ledger → nouveau registre.

### Live (opt-in)

- Une station, un serveur, probe officiel, vérification egress.
- Test de binding : reconnecter au même hostname et vérifier l’egress.

### Chaos

- Crash pendant claim.
- DB verrouillée.
- Timeout NordVPN API.
- Tunnel qui tombe pendant un test.
- Deux allocateurs concurrents (doit être impossible via lease).

### Performance

- 10 000 lignes : éligibilité < 50 ms, allocation < 100 ms.
- 100 tests simulés : pas de fuite de lease.

---

## 16. Migration et feature flags

### Flags

```yaml
ip_registry:
  enabled: false          # off | observe | enforce
  shadow_compare: true
allocator:
  mode: off               # off | observe | enforce
binder:
  mode: compose_hostname  # control_api | compose_hostname | custom_wg
```

### Étapes

1. **Off** : aucun changement de comportement.
2. **Observe** : import catalogue + tests passifs, aucune assignation ;
   comparaison avec la sélection actuelle.
3. **Enforce canary** : une seule station (ex. 6) utilise l’allocateur.
4. **Enforce flotte** : toutes les stations.
5. **Suppression** de l’ancien scanner ad hoc.

### Double écriture

Pendant la migration, écrire à la fois dans `quota_ledger.json` et dans
`ip_registry.db`, puis vérifier la cohérence.

---

## 17. Phases et critères d’acceptation

### Phase 0 — ADR + schéma + flags

- ADR validé.
- Schéma SQLite créé, migration initiale.
- Flags en place, comportement inchangé.
- **Critère** : tests verts, aucune régression.

### Phase 1 — Catalogue + registre + score

- Import 7 232 serveurs.
- `ip_state` alimenté en observe.
- Éligibilité et score testés.
- **Critère** : 0 IP éligible avec reset futur ; 10k lignes < 50 ms.

### Phase 2 — Binder serveur (spike critique)

- Prouver qu’une station peut se reconnecter à un hostname précis.
- Vérifier l’egress reproductible.
- Fallback documenté.
- **Critère** : 3 reconnexions sur le même hostname → même egress.

### Phase 3 — Allocateur (canary)

- Needs → claim → bind → probe → assign.
- Aucun test si toutes les stations saines.
- Lease/crash recovery.
- **Critère** : station canary 200 via allocateur, 0 double assignation.

### Phase 4 — Flotte + dashboard

- Toutes les stations via allocateur.
- API/dashboard complets.
- Métriques et alertes.
- **Critère** : 6 stations, IP distinctes, stabilité 24 h.

### Phase 5 — Preselect + optimisation

- Job de présélection optionnel.
- Cycle de renouvellement.
- Nettoyage/retention.
- **Critère** : budget de tests respecté, pas de régression.

---

## 18. Risques et mitigations

| Risque | Impact | Mitigation |
|---|---|---|
| Gluetun ignore `hostnames` en WG | Binding impossible | Spike phase 2 ; fallback custom WG |
| Egress NAT ≠ server IP | Registre faux | Toujours stocker l’egress observé ; revérifier |
| Reset par IP mal modélisé | Retests inutiles | `reset_at = observation + Retry-After` |
| Scan complet trop long | Blocage | Lazy + classement heuristique |
| SQLite concurrent | Corruption | WAL, un writer, transactions courtes |
| API NordVPN indisponible | Catalogue vide | Cache dernier bon + alerte |
| Fuite de credentials | Sécurité | Env, jamais logs, audit |
| Station 6 interrompue | Perte du service | Canary, shadow, rollback flag |

---

## 19. Configuration cible

```yaml
ip_registry:
  enabled: true
  db_path: logs/ip_registry.db
  catalog_refresh_s: 21600
  max_test_concurrency: 2
  test_timeout_s: 60
  lease_ttl_s: 300
  retention_days: 7
  score:
    availability_weight: 0.55
    latency_weight: 0.25
    freshness_weight: 0.20
    latency_budget_ms: 8000
    freshness_ttl_h: 48
    min_score: 20
  eligibility:
    error_cooldown_s: 1800
  allocator:
    mode: enforce
    max_attempts_per_need: 20
    backoff_s: [1, 2, 5, 10]
  binder:
    mode: compose_hostname
    recreate_timeout_s: 180
  preselect:
    enabled: false
    max_tests_per_hour: 60
```

---

## 20. Arborescence cible

```text
free/ip_registry/
  __init__.py
  domain.py          # entités, value objects, state machine
  models.py          # dataclasses / TypedDict
  ports.py           # Protocols (Catalog, Registry, Binder, Probe)
  scoring.py         # formule de score
  eligibility.py     # règles d’éligibilité
  allocator.py       # needs → claim → test → assign
  registry.py        # façade registre
  catalog.py         # refresh NordVPN
  binder.py          # façade binder
  probe.py           # probe officiel
  adapters/
    sqlite_registry.py
    nordvpn_catalog.py
    compose_binder.py
    control_api_binder.py
    custom_wg_binder.py
    curl_probe.py
  api.py             # routes FastAPI
tests/ip_registry/
  test_scoring.py
  test_eligibility.py
  test_state_machine.py
  test_allocator.py
  test_lease.py
  test_sqlite.py
  test_binder_contract.py
  test_api.py
docs/adr/ADR-00x-ip-registry.md
```

---

## 21. Runbook opérationnel

### Voir l’état

```bash
sqlite3 logs/ip_registry.db \
  "SELECT state, COUNT(*) FROM ip_state GROUP BY state;"
```

### IP d’une station

```bash
sqlite3 logs/ip_registry.db \
  "SELECT * FROM ip_state WHERE assigned_station=6;"
```

### Libérer une station

```bash
curl -X POST http://127.0.0.1:4000/api/free/allocator/release \
  -H "Content-Type: application/json" -d '{"station":6}'
```

### Relancer l’allocateur

```bash
curl -X POST http://127.0.0.1:4000/api/free/allocator/run
```

### Urgence : revenir à l’ancienne sélection

```yaml
ip_registry.enabled: false
allocator.mode: off
```

Redémarrer le proxy.

---

## 22. Décisions ouvertes

1. **Binder** : Compose hostname vs control API vs custom WG — décision après
   spike phase 2.
2. **Stockage** : SQLite (recommandé) vs JSON — SQLite pour les requêtes et la
   concurrence.
3. **Présélection** : lazy uniquement ou job complet en fond — commencer lazy,
   activer le job si nécessaire.
4. **Cycle** : quotidien à 00:00 UTC ou après reset individuel — suivre
   `reset_at` par IP.
5. **Partage d’IP** : interdit par défaut ; option `allow_shared_ips` pour les
   cas de secours.
6. **Géo** : un pool par pays/région ou filtre à la volée — commencer par filtre.

---

## 23. Résumé exécutif

Le système cible est un **registre d’IP classées** alimenté par un catalogue
NordVPN, avec un **allocateur à la demande** qui ne teste que les stations en
besoin, claim une IP à la fois, la teste, l’assigne et la fige jusqu’à échec
ou rotation.

Le verrou est le **binding serveur** : sans pouvoir reconnecter une station à
un hostname précis, le classement des IP est perdu à chaque reconnexion.
C’est la phase 2, à valider en premier.
