# Plan d'auto-réparation et de disponibilité — 2026-11-10

**Objet** : expliquer pourquoi quatre stations restent `daily-bucket-exhausted`
alors que les tunnels sont sains, pourquoi le système ne se corrige pas seul,
ce qu'il en est d'OpenVPN et des identifiants, puis définir le plan pour que
la flotte redevienne automatiquement 6/6 utilisable.

---

## 1. Constat mesuré

### 1.1 Flotte

| Station | Tunnel | IP | `model_usable` | Raison | Quota | Échecs ledger |
|---|---|---|---|---|---|---|
| 1 | connecté | 186.247.50.20 | non | `daily-bucket-exhausted` | 8/200 | 118 |
| 2 | connecté | 130.195.218.110 | non | `daily-bucket-exhausted` | 0/200 | 117 |
| 3 | connecté | 87.120.102.39 | non | `daily-bucket-exhausted` | 6/200 | 117 |
| 4 | connecté | 186.247.65.10 | non | `daily-bucket-exhausted` | 41/200 | 117 |
| 5 | connecté | 84.247.40.26 | **oui** | — | 175/200 au pic | 0 |
| 6 | connecté | 87.120.102.87 | **oui** | — | — | 0 |

Les six tunnels sont up. Le problème n'est donc pas « OpenVPN ne monte plus » :
il est **quota + sélection + absence de rotateur actif**.

### 1.2 Le ledger est formel

Les buckets des IP 1–4 ont `successes=0`, `last_status=429`,
`last_error=FreeUsageLimitError`, `reset_at` dans ~19 min. Ce sont de vraies
réponses 429 du fournisseur, pas une estimation locale. Les IP 5/6 ont au
contraire 196 et 59 succès, sans échec.

### 1.3 La charge est anormale

- 196 requêtes finales en 30 min, dont 87 sur la station 5 et 44 sur la 6.
- `inflight` observé jusqu'à **160** sur une seule station.
- `model_slot_wait_s: 0.25` et `max_inflight_per_model_ip: 8` : au-delà de
  0,25 s d'attente, le pool **fail-open** et laisse passer la requête sans
  slot. Le plafond de concurrence est donc contourné.
- Les logs montrent des centaines de `model slot timeout ... fail-open`.
- Les buckets 1–4 accumulent ~117 échecs chacun alors que le trafic final est
  concentré sur 5/6 : une partie des tentatives libres frappe encore des IP
  dont le ledger dit déjà « épuisé ».

### 1.4 La boucle de réparation est ouverte mais personne ne la ferme

Configuration actuelle :

```yaml
ip_rotation:
  repair_owner: supervisor
  station_repair:
    enabled: false
ip_registry:
  allocator:
    mode: observe
```

Le pool fait exactement ce qu'on lui demande : il **délègue** chaque rotation
au superviseur (`rotation-delegated`). Le superviseur, lui, est en `observe` :
il journalise `observe: station N would repair (...)` et `broken=[1..6]`, mais
ne lance aucune chaîne. `station_repair.enabled=false` désactive le réparateur
local. **Il n'y a donc aucun propriétaire actif de la rotation quota.**

Le watchdog ne comble pas ce trou : depuis la correction `QUOTA`, une station
seulement épuisée passe `DRAINING` et **n'ouvre pas d'incident** — c'est correct
pour ne pas crier « panne », mais cela signifie aussi qu'aucun job de réparation
n'est créé par le watchdog. La rotation doit venir de l'allocateur, pas du
watchdog.

### 1.5 OpenVPN et identifiants

Les preuves ne montrent **pas** un problème d'identifiants :

- logs Gluetun : `VERIFY OK`, certificat NordVPN valide, TLS 1.3 ;
- aucun `AUTH_FAILED` récent sur les six conteneurs ;
- `OPENVPN_USER`, `OPENVPN_PASSWORD`, `NORDVPN_TOKEN` sont présents ;
- les stations 1–4 sont connectées et ont déjà servi des 200 ;
- les échecs des stations 5/6 avant réparation étaient des `Connection reset`
  sur des hostnames morts (bf1, al41, etc.), pas des refus d'authentification.

Conclusion : **les identifiants OpenVPN sont valides**. Le problème est la
sélection de serveurs dont l'egress est mort ou dont l'IP est épuisée. Un
`AUTH_FAILED` réel serait visible dans les logs et toucherait toutes les
nouvelles sessions OpenVPN ; ce n'est pas le cas.

---

## 2. Pourquoi ça ne se corrige pas automatiquement

### 2.1 Un seul propriétaire, mais inactif

La constitution (principe VIII) impose **un seul réparateur**. Le pool ne
recrée jamais une station quand `repair_owner=supervisor` ; il publie une
demande dans `repair_requests`. Le superviseur est le seul à pouvoir la
consommer. En `observe`, il ne la consomme pas. Résultat : les demandes
`rotation-delegated` s'empilent et expirent sans effet.

### 2.2 Le quota n'est pas une panne, mais il exige une rotation

Le watchdog a maintenant raison de ne pas ouvrir d'incident pour un simple
quota. Mais le superviseur doit traiter `DRAINING` / `daily-bucket-exhausted`
comme **« IP à remplacer »**, pas comme « attendre le reset ». La politique
actuelle attend passivement le reset (~20 min) pendant que le trafic restant
sature les deux seules IP utilisables.

### 2.3 Le plafond de concurrence est fail-open

`acquire_model_slot` retourne `None` après 0,25 s ; les appelants continuent.
Sous rafale, 160 flux partent sur 2 IP : les 429 arrivent plus vite que la
rotation, et chaque 429 ajoute un échec au ledger. C'est un cercle vicieux :
**plus le système est chargé, moins il peut se réparer**.

### 2.4 La sélection ne barre pas le ledger à chaque tentative

Le ledger bloque la sélection finale, mais une partie des tentatives libres
(fallback, streams, reprises) ne repasse pas la garde
`_ledger_usable(model, station)` au moment du dispatch. Les 117 échecs par IP
le prouvent : des requêtes atteignent encore des IP déjà marquées épuisées.

### 2.5 Les pins/hostnames morts sont réessayés

Les hostnames connus-200 ne sont pas filtrés par transport avant notre
correction récente, et un hostname peut être re-lié à la même IP épuisée. Les
tentatives OpenVPN sur bf1/al41/uk3071 etc. consomment un bind et un AUTH pour
un `connection reset` ou un `no-egress`.

---

## 3. Plan cible

### P0 — Arrêter la consommation anormale (immédiat)

1. **Admission control fail-closed**
   - `model_slot_wait_s` : 0,25 s → **10–15 s**.
   - `max_inflight_per_model_ip` : 8 → **4**.
   - Au timeout : répondre **429/503 avec `Retry-After`** au client, jamais
     laisser passer sans slot.
   - Ajouter un plafond global de requêtes free simultanées.
2. **Réparer les fuites de slots et de `stream_tasks`**
   - garantir `release_model_slot` et `unregister_stream` sur tous les chemins
     (annulation client, erreur de POST, flux abandonné) ;
   - test de flux annulé/abandonné ;
   - exposer `inflight`, `slot_timeouts`, `stream_tasks` dans `/api/pool-status`.
3. **Garde ledger au dispatch**
   - Avant chaque tentative free, appeler `_ledger_usable(model, station)` ;
   - si faux : ne pas envoyer, passer à la station suivante ;
   - un 429 ne doit jamais être réessayé sur la même IP avant `reset_at`.
4. **Activer le propriétaire**
   - `allocator.mode: enforce` ;
   - `max_parallel_repairs: 2` (puis 3 après validation) ;
   - `rebind_limit_per_min: 8`, `rotation_stagger: 10`,
     `station_min_rotation_interval_s: 600`.
   - Le superviseur doit traiter `DRAINING`/`daily-bucket-exhausted` comme
     `broken` et lancer une chaîne, sans ouvrir d'incident « panne ».

### P1 — Rotation quota automatique

1. Déclencheur double :
   - **429 réel** → rotation immédiate de l'IP ;
   - **headroom < 30 %** (seuil proactif) quand une autre station a du quota.
2. Ne jamais attendre le reset si une IP fraîche est disponible.
3. Si aucune IP fraîche : `DRAINING` explicite, pas de 6/6 simulé.
4. Protéger la dernière station utilisable (pas de rotation qui met la flotte
   à 0/6) ; mais une station épuisée doit être remplacée dès qu'une autre
   peut servir.
5. Sélection fresh-first :
   - ledger `reset_at` futur → exclue ;
   - IP déjà vue sur une autre station → exclue ;
   - hostname dont la dernière IP est épuisée → exclue (fait pour la file
     qualification) ;
   - preuves 200 filtrées par transport (fait : `registry_hosts(transport)`).
6. Standby/seconde session pour cutover sans coupure (T076), une fois le
   reste stable.

### P2 — Fiabiliser OpenVPN et les credentials

1. **Canary credentials** au boot et toutes les 6 h :
   - jeton NordVPN ;
   - un bind OpenVPN de test sur un hostname connu ;
   - un `AUTH_FAILED` réel doit alerter, pas être confondu avec un
     `connection reset`.
2. Sélection par transport :
   - un hostname dont les 200 sont uniquement WireGuard ne doit pas être
     prioritaire pour une réparation TCP ;
   - un hostname qui reset TCP 2 fois de suite est mis en quarantaine TCP.
3. Nettoyer les pins morts : après 2 `connection reset`/`no-egress`, retirer
   l'override Compose et laisser Gluetun choisir un serveur frais.
4. Vérifier la limite de sessions NordVPN : six stations simultanées sont
   autorisées, mais un `AUTH_FAILED` global doit être diagnostiqué comme
   limite de compte, pas comme panne de transport.

### P3 — Observabilité et acceptance

Tableau de bord par station :

- `quota_state` (`active`, `draining`, `exhausted`), `headroom`, `reset_at` ;
- `rotation_due`, `rotation_owner`, `last_rotation_at`, `last_rotation_ms` ;
- `inflight`, `slot_timeouts`, `stream_tasks`, `ledger_failures` ;
- `openvpn_auth_ok`, `last_tcp_reset_age_s`.

Critères d'acceptation :

- 6/6 `client_usable` ≥ 99 % sur 24 h quand au moins une IP 200 existe ;
- rotation quota p95 < 60 s, max < 5 min ;
- `inflight` ≤ plafond configuré, 0 fuite de slot ;
- 0 `AUTH_FAILED` non expliqué ;
- canary `logs/canary-014.json` à 6/6 ;
- aucun incident pour un simple quota (DRAINING, pas panne).

---

## 4. Séquence de déploiement

1. **P0.1/P0.2** : admission control + fuites de slots, tests de charge.
2. **P0.3** : garde ledger au dispatch, test « 0 tentative sur IP épuisée ».
3. **P0.4** : `allocator.mode=enforce`, `max_parallel_repairs=1`, canary sur
   **une** station ; vérifier rotation < 60 s et 0 incident.
4. Étendre à 2 stations, puis aux six.
5. **P1** : rotation proactive au seuil, standby, canary 6/6.
6. **P2** : canary credentials et quarantaine TCP.
7. **P3** : SLO 24 h et signature de release.

## 5. Rollback

- `allocator.mode: observe` (le superviseur redevient observateur) ;
- `station_repair.enabled: false` (déjà le cas) ;
- redémarrer le superviseur ;
- les IP/ledger ne sont pas modifiés par le rollback.

## 6. Conclusion

Le système n'a pas un problème d'identifiants OpenVPN. Il a un problème de
**boucle de contrôle** : un propriétaire de réparation inactif, un plafond de
concurrence contourné, et des tentatives qui ne respectent pas toujours le
ledger au dispatch. Les corrections P0 doivent précéder l'activation
`enforce`, sinon la rotation sera noyée par la rafale de requêtes.

---

## 7. P0 appliqué — 2026-11-10 21:00Z

### Admission fail-closed

- `free/pool/__init__.py` : `ModelSlotTimeout` ; `acquire_model_slot` lève au
  timeout au lieu de rendre `None` (`model_slot_fail_closed: true` par défaut).
- `model_slot_wait_s` : **12 s** ; `max_inflight_per_model_ip` : **4** ;
  `model_slot_retry_after_s` : **2 s**.
- Les quatre call-sites (hedge, stream, séquentiel, SystemOne) convertissent le
  refus en **503 + Retry-After** (non-stream) ou refus SSE immédiat, sans
  rotation ni jambe payante.
- `/api/free-health.slot_wait` expose `limit`, `timeouts`, `rejections`.

### Fuites de slots

- `_open_free_stream` ferme désormais le **wrapper** (`_CurlCffiStreamResponse`)
  et non la réponse brute : le jeton (modèle, IP) est rendu même si le
  consommateur n'appelle pas `aclose()`.
- `_acquire_free_model_slot` ne transforme plus `ModelSlotTimeout` en `None`.

### Garde ledger au dispatch

- `pick_candidates` et `_best_station*` reçoivent le modèle (filtre ledger)
  dans les chemins chat, hedge et SystemOne.
- Avant chaque POST, `model_bucket_usable(model, station)` est revérifié ; une
  IP dont le bucket est épuisé est sautée, jamais re-strikée.
- `_legacy_candidate` filtre maintenant les hostnames dont `claims.egress_ip`
  est dans `forbidden_ips` : al73/al74/uk2433 ne sont plus re-piochés en boucle.
- La file de qualification legacy est filtrée sur `claims.egress_ip` **et**
  `last_egress_ip` : une ligne sans IP observée ne peut plus ramener une IP
  brûlée.
- Une demande de réparation dont l'IP ne correspond plus à l'IP live est
  supprimée (`_repair_request_is_stale`) : plus de re-rotation d'une station
  déjà réparée.
- Les refreshs utilisent une tranche de **25** candidats au lieu de 5, avec
  `refresh_cooldown_s: 120` (au lieu de 900) : la recherche d'IP fraîche ne
  reste plus bloquée 15 min après 5 échecs.

### Propriétaire actif

- `allocator.mode: enforce`, `max_parallel_repairs: 2`,
  `rebind_limit_per_min: 3`.
- Superviseur redémarré (PID 6832), proxy redémarré (PID 9556).
- `egress_catalog.qualification.daemon_enabled: true` avec
  `daemon_max_hosts: 1` : le sweep de qualification est borné et le tick
  superviseur reste < 1 s (plus de blocage 180 s). Le résumé de qualification
  garde donc des états de station frais pour le canary (6/6, SLO 100 %).
- **Bridge chaîne → qualification** : chaque tentative réelle de
  `fresh_chain` (WireGuard/OpenVPN UDP/TCP, 200/429/no-egress/bind_error) est
  enregistrée dans `qualification_attempts` et agrégée dans
  `transport_health`. Les preuves de transport ne dépendent plus du seul
  sweep shadow.
- **Garde de réserve** : `_repair_holds` empêche un refresh proactif de
  descendre la flotte sous 3 stations utilisables, et une réparation de
  protéger la dernière station debout. `max_parallel_repairs` est repassé à
  **2** : quatre refreshs simultanés avaient laissé la flotte à 0/6.
- **Garde AUTH** : `_auth_pause_remaining` lit
  `vpn.auth_global_pause_remaining_s` ; pendant la pause, le superviseur
  n'autorise **qu'une seule** chaîne (comme la probe unique du manager), au
  lieu d'attendre 5/15 min sans réparer. Quand `auth_fail_30min > 0`, les
  démarrages sont espacés de **60 s** (deux échecs dans la même fenêtre
  60 s déclenchent la pause globale). Un épisode 0/6 le 2026-11-10 22:44Z
  (binds simultanés → `AUTH_FAILED` en cascade) a été résorbé en 2 min par
  ce garde.
- **Limite de capacité mesurée** : les IP NordVPN gratuites rendent souvent
  `429` après **20–40 requêtes** (IP partagées), la limite de sessions
  NordVPN est basse, et un bind utile ne sort qu'après 1–2 min. Le 6/6
  **stable** exige le standby/second tunnel (T076) ou une source d'egress
  supplémentaire ; aucun réglage de boucle ne crée des IP fraîches.
- **État au 2026-11-11 00:45Z** : 6/6 connectées, **1/6 `model_usable`**
  (s4), superviseur `enforce`, rotation en cours. Le 6/6 a été atteint
  plusieurs fois dans la session, mais n'est pas stable tant que la réserve
  d'IP fraîches ne se reconstruit pas. Le contrôleur ne s'auto-détruit plus :
  il attend les pauses AUTH, espace les binds et protège la réserve.

### Preuve live

- `slot_wait`: `limit=4`, `timeouts=0`, `rejections=0`, p95 0 ms, max 16 ms.
- **6/6 `model_usable` simultanément** : s1 `84.247.40.31`, s2
  `186.247.44.119`, s3 `186.247.51.4`, s4 `87.120.102.94`, s5
  `87.120.102.12`, s6 `87.120.102.57` (six IP distinctes, toutes en
  WireGuard en parallèle).
- Superviseur : `mode=enforce`, `cycle: broken=[] running=[]` — la boucle de
  réparation est au repos tant que la flotte sert.
- Les IP fraîches peuvent être déjà épuisées côté amont : le ledger les marque
  et la rotation continue, sans re-tenter l'IP brûlée. La confirmation en deux
  probes (`fresh_chain`) évite de pinner une IP qui n'a qu'une requête de marge.
- Canary de qualification toujours rouge sur les preuves de cutover/transports
  (T148/T149, T030–T037) : la sonde live 6/6 est verte, le manifeste de release
  attend les artefacts de qualification, pas un mensonge.

## 8. Qualifieurs dédiés — 2026-11-11

### 8.1 Le goulot mesuré

Le daemon de qualification tournait en **shadow** et ses époques duraient
**7 ms** (`epoch-20261111-055456` : 1794376496.397 → .403) : il rendait le
verrou sans jamais ouvrir de tunnel. Avec `daemon_max_hosts: 1` et
`daemon_interval_s: 900`, le plafond était de **4 hosts/heure** même en live.
En pratique, seules les chaînes de réparation testaient des IP :
**284 tentatives/24 h**, p50 **68,4 s**, 26 hosts, **3 hosts utiles/h**.

Deux causes racines empêchaient la voie rapide :

- `free/qualification/station_adapter.py` ne construisait `HybridBinder` que si
  `GLUETUN_CONTROL_SERVER_API_KEY` était défini ; la clé réellement présente
  est `VPN_CONTROL_API_KEY` (présente dans `.env` et dans l'env des conteneurs) ;
- `GluetunControlBinder` visait `vpn-gluetun-{N}` (nom du service Compose) au
  lieu de `opencode-vpn-{N}` (nom du conteneur) : `docker exec` répondait
  `No such container`, donc chaque bascule retombait sur un recreate Compose
  à ~68 s.

Le control API Gluetun était déjà actif (`HTTP_CONTROL_SERVER=on`, port 8000
interne non publié, accès via `docker exec`) mais n'était jamais utilisé pour
la qualification.

### 8.2 Slots dédiés 7–10

`free/qualification/qualifiers.py` exploite les services **déjà déclarés**
dans `docker-compose.yml` (`vpn-gluetun-7` … `vpn-gluetun-10`, profil
`multi-station`) :

- un slot = un conteneur `opencode-vpn-{7..10}` en **WireGuard**, démarré via
  `docker compose --profile multi-station -f docker-compose.yml -f logs/compose-qualifier-N.yml up -d --no-deps` ;
- une boucle `QualificationSweep` **live** par slot, époque nommée
  `qualifier-{N}-{ts}`, `transport_order: [wireguard]`, une seule tentative par
  host et par époque — UDP/TCP restent réservés aux réparations `fresh_chain` ;
- sonde limitée à **un seul modèle** (`probe_models=(first,)`, timeout 8 s)
  pour éviter 11 probes × 60 s par host ;
- arrêt des conteneurs 7–10 au shutdown du proxy (libère les sessions NordVPN).

Les six stations de production ne sont jamais touchées : le manager ne
connaît que `station_base=7`, et le tick du superviseur est forcé en shadow
dès qu'un qualifieur est actif (`daemon_shadow: true` explicite).

### 8.3 Pilotage GUI

Carte **« Qualifieurs dédiés »** dans `QualificationStatus.svelte` :

- interrupteur `activer` + champ `nombre` (1 à `max_count`) ;
- bouton **Appliquer** → `POST /api/egress/qualification/qualifiers`
  `{"enabled": true, "count": 2}` ;
- bouton **Tout arrêter** → `count=0`, tâches annulées, conteneurs stoppés ;
- tableau par slot : état (`running`/`starting`/`error`), dernière époque,
  hosts testés/promus, durée, fin, erreur Docker.

`GET /api/egress/qualification/qualifiers` renvoie l'instantané du manager.
Si le manager n'est pas attaché (proxy démarré avant la fonctionnalité), le
POST persiste la demande dans `config.yaml` et répond
`restart_required: true`.

Clés `config.yaml` :

```yaml
egress_catalog:
  qualification:
    shadow_mode: false      # autorise les époques live manuelles/GUI
    daemon_shadow: true     # le tick automatique reste shadow
    qualifiers:
      enabled: false
      count: 0
      max_count: 4
      recommended_max: 2
      station_base: 7
      transport: wireguard
      max_hosts_per_epoch: 40
      interval_s: 30
      max_attempts_per_host: 1
      transport_rounds: 1
```

### 8.4 Plafonds

| Dispositif | Débit mesuré/estimé |
|---|---|
| live 1 station Compose (~68 s/bind) | 25–40 hosts/h |
| live 6 stations Compose | 250–350 hosts/h |
| 1 qualifieur control API | 220–280 hosts/h |
| 2 qualifieurs | 440–560 hosts/h |
| plafond pratique (sessions NordVPN) | ~500 hosts/h |

Le GUI autorise `max_count: 4`, mais **2 est le maximum recommandé** : au-delà,
les binds simultanés déclenchent une cascade `AUTH_FAILED` sur les stations de
production (incident 0/6 du 2026-11-11 22:44Z, résolu par le garde AUTH en
~2 min). Le manager affiche un avertissement explicite au-delà de
`recommended_max`.

### 8.5 Validation live

1. GUI : activer, `nombre = 1`, **Appliquer**.
2. `docker ps --filter name=opencode-vpn-7` → `Up`, `VPN_TYPE=wireguard`.
3. `qualification_epochs` : époques `qualifier-7-*`, `status=complete|partial`.
4. Débit : hosts testés / durée ; viser 200–280/h.
5. Passer à `nombre = 2` seulement si les six stations restent `model_usable`
   et si aucun `AUTH_FAILED` n'apparaît dans `vpn.auth_global_pause_remaining_s`.
6. **Tout arrêter** en fin de test : les conteneurs 7–10 doivent disparaître de
   `docker ps`.



