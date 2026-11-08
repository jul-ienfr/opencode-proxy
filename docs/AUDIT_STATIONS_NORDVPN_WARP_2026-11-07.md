# Audit exhaustif — cycle de vie des stations NordVPN et WARP

**Date** : 2026-11-07 14:10 (Europe/Paris)  
**Périmètre** : `opencode-proxy-one`, jambe free, stations NordVPN (Gluetun) et endpoints Cloudflare WARP (`caomingjun/warp`).  
**Objectif opérateur** : **toutes les stations et toutes les IP doivent être utilisables**, en free, avec bascule automatique sur une IP qui répond 200.  
**Méthode** : lecture du code et de la configuration, inspection Docker live, sondes d'egress, base `registry.db`, journaux, specs 001–010. Les faits live sont datés ; les faits de code sont cités `fichier:ligne`.  
**Statut** : audit terminé, aucun changement runtime appliqué lors de la rédaction. Une première itération d'implémentation a ensuite été livrée : voir `docs/evidence/011-implementation-2026-11-07.md`.  
**Plan Spec Kit associé** : `specs/011-station-fleet-usability/`.

---

## 1. Résumé exécutif

### 1.1 Verdict live

| Flotte | Conteneurs | Santé conteneur | Egress réel | Client-utilisable | Verdict |
|---|---:|---|---:|---:|---|
| NordVPN | **5 / 6** (station 2 absente) | **0 / 5** healthy | **0 / 5** | **0 / 6** | **P0 — flotte morte** |
| WARP | **6 / 6** | **6 / 6** healthy | **6 / 6** répondent | **5 buckets uniques / 6** (client non prouvé) | **P1 — 1 station en double quota** |

**Constat central** : le proxy tourne (`pythonw opencode.py`, PID 14704) mais **n'écoute pas sur `:4000`**. Les six stations NordVPN sont annoncées `station_count: 6` ; une seule n'a jamais été recréée après une purge, les cinq autres bouclent entre `openvpn` et `wireguard`. WARP répond techniquement, mais deux endpoints partagent le même `/64` et la configuration persistée ne correspond plus aux IP live.

### 1.2 Chaîne causale principale (NordVPN)

```text
egress_providers = [warp]                    (config.yaml:140-142)
repair_owner = supervisor, station_repair.enabled = false
        │
        ├─► le manager ne se connecte plus au boot
        │     (start() saute _startup_connect si repair_owner=supervisor,
        │      vpn/manager.py:2128-2140)
        │
        ├─► le superviseur s'arrête            (fresh_chain_supervisor.py:1236-1241)
        │
        ├─► personne ne démarre/répare les stations NordVPN
        │
        ├─► les pins morts ne sont jamais remplacés
        │     us10506 / us10019 / us10508 / us10018 ne résolvent plus
        │
        ├─► tunnels sans tun0, SOCKS timeouts, healthcheck 15 s en échec
        │
        ├─► watchdog : 31 restarts en 10 min     (logs/debug.log 13:01:39Z)
        │
        ├─► boot reconcile : stack conteneur (openvpn) != .env (wireguard)
        │     → `docker rm -f` de 5 stations toutes les ~10 min
        │
        └─► recréation avec un stack mémoire divergent → boucle
```

Cette boucle viole trois principes de la constitution : **VIII** (un seul réparateur, autonomie), **III** (une seule source de vérité) et **I** (la disponibilité se mesure, elle ne se déclare pas).

### 1.3 Ce qui fonctionne déjà

- Les six conteneurs WARP sont sains et leur egress IPv6 répond (`api6.ipify.org`) ; le `WarpRotator` a un vrai mécanisme restart → recreate → probe → persist, avec refus des blocs dupliqués (`free/warp_rotation.py:179-259`).
- L'isolation réseau NordVPN est correcte : ports publiés sur `127.0.0.1` uniquement, image Gluetun épinglée par digest (`docker-compose.yml:185-283`).
- Le conseiller de capacité 010, le ledger de quota, le registre SQLite et les tests existent ; le problème n'est pas l'absence d'outils, c'est leur **branchement** et la **cohérence de leurs décisions**.
- Le catalogue contient 9 788 hosts et 1 175 serveurs ; il y a de la matière pour trouver des IP fraîches.

### 1.4 Verdict par domaine

| Domaine | NordVPN | WARP | Verdict |
|---|---|---|---|
| Création / provisioning | Code présent, mais aucun propriétaire de démarrage quand `repair_owner=supervisor` ; station 2 absente | Aucun provisioner ; conteneurs créés à la main ; recréation par `docker run` | **Non conforme** |
| Gestion / config | Trois writers `.env`/mémoire/compose ; dérive stack/proto/host | Config `egress_ip`/`block` périmée pour 5/6 ; reconstruction écrase l'egress appris | **Non conforme** |
| Maintenance | Watchdog 31 restarts/10 min ; repair désactivé ; superviseur idle | Conteneur mort jamais recréé ; pas de probe périodique ; logs perdus | **Non conforme** |
| Rotation | Bloquée par les pins morts ; budgets jamais le facteur limitant | 4 recréations/tentative, pas de plafond horaire, tempête observée | **Non conforme** |
| Quotas | Ledger existe, mais IP fraîches déjà 429 | Ledger indexé sur blocs périmés ; 5 buckets pour 6 | **Partiel** |
| Observabilité | Statut riche mais JSON périmé ; `current_server` trompeur ; **faux 6/6 live** (F-32) | Champs egress/bloc absents ; zéros forcés ; logs de rotation perdus | **Non conforme** |
| Sécurité | Loopback et digest corrects ; secret dupliqué dans `.env` ; diagnostic sans token | SOCKS5 sur `0.0.0.0` sans auth ; image `latest` ; pas de limites | **Non conforme** |
| Tests | 329 fichiers, 2 774 tests ; bons filets, mais G1–G25 non couverts | 49 tests rotation ; provisioning/rollback/bind non couverts | **Partiel** |
| Documentation | Très riche mais stale sur les stacks/DNS/MTU/capacité | Contrats 005 non implémentés ; doc IPv6 obsolète | **Partiel** |

---

## 2. État live daté

### 2.1 Conteneurs

Relevé `docker ps` du 2026-11-07 vers 14:05 :

```text
opencode-vpn    Up 8 min (unhealthy)   127.0.0.1:1080->1080, 127.0.0.1:8888->8888
opencode-vpn-3  Up 9 min (unhealthy)   127.0.0.1:1082->1082, 127.0.0.1:8890->8890
opencode-vpn-4  Up 8 min (unhealthy)   127.0.0.1:1083->1083, 127.0.0.1:8891->8891
opencode-vpn-5  Up 8 min (unhealthy)   127.0.0.1:1084->1084, 127.0.0.1:8892->8892
opencode-vpn-6  Up 9 min (unhealthy)   127.0.0.1:1085->1085, 127.0.0.1:8893->8893
warp-test       Up 16 h (healthy)      0.0.0.0:11080->1080
warp-test2      Up 9 h  (healthy)      0.0.0.0:11085->1080
warp-test3      Up 9 h  (healthy)      0.0.0.0:11086->1080
warp-test4      Up 9 h  (healthy)      0.0.0.0:11087->1080
warp-restore1   Up 9 h  (healthy)      0.0.0.0:11103->1080
warp-restore3   Up 15 h (healthy)      0.0.0.0:11105->1080
```

`opencode-vpn-2` est **absente**. Les cinq conteneurs NordVPN présents ont tous `FailingStreak >= 9` et un healthcheck en timeout.

### 2.2 Divergence `.env` ↔ conteneur

| Station | `.env` (intention) | Conteneur (réel) | Écart |
|---|---|---|---|
| 1 | `VPN_TYPE=openvpn`, `OPENVPN_PROTOCOL=tcp`, `SERVER_HOSTNAMES=al41.nordvpn.com`, pays Albanie | `openvpn`, `udp`, `us10506.nordvpn.com`, États-Unis | proto, host, pays |
| 2 | `wireguard`, `udp`, pas de host | **conteneur absent** | station manquante |
| 3 | `wireguard`, `tcp`, pas de host | `openvpn`, `tcp`, `us10019.nordvpn.com` | stack, host |
| 4 | `wireguard`, `tcp` | `openvpn`, `udp`, `us10508.nordvpn.com` | stack, proto, host |
| 5 | `openvpn`, `udp` | `wireguard`, `udp`, `us10507.nordvpn.com` | stack |
| 6 | `wireguard`, `tcp` | `openvpn`, `udp`, `us10018.nordvpn.com` | stack, proto, host |

Cause code : `_compose_env()` écrase `VPN_TYPE_STATION{n}` avec le stack mémoire (`vpn/manager.py:5441-5444`), tandis que le reconcile de boot lit le `.env` comme autorité (`vpn/manager.py:11284-11288`). Les deux sources divergent, et le reconcile supprime les conteneurs qu'il juge « mauvais stack » (`vpn/manager.py:11313-11321`).

### 2.3 Boucle de reconcile

Extraits de `logs/debug.log` :

```text
11:48:05Z boot reconcile: opencode-vpn-4 stack=openvpn != attendu=wireguard (source .env) -> rm
11:48:07Z boot reconcile removed 5 container(s): opencode-vpn-3, opencode-vpn-5, opencode-vpn-2, opencode-vpn-4, opencode-vpn-6
11:54:48Z boot reconcile: opencode-vpn-6 stack=openvpn != attendu=wireguard (source .env) -> rm
...
12:30:26Z boot reconcile removed 5 container(s): ...
12:45:39Z boot reconcile removed 1 container(s): opencode-vpn-5
```

Le cycle se répète environ toutes les 6 à 12 minutes. `fresh_chain_supervisor.log` confirme que les six stations sont vues `broken` à chaque cycle, puis que le superviseur s'arrête :

```text
12:52:21Z cycle: broken=[1, 2, 3, 4, 5, 6] running=[]
12:53:21Z cycle: broken=[1, 2, 3, 4, 5, 6] running=[1, 2, 3, 4, 5, 6]
12:55:07Z NordVPN non sélectionné — supervisor idle
```

### 2.4 Pins DNS morts

Les hostnames épinglés dans les conteneurs ne résolvent plus :

```text
nslookup us10506.nordvpn.com  -> nom seul, aucune adresse A
nslookup us10019.nordvpn.com  -> nom seul, aucune adresse A
nslookup us10508.nordvpn.com  -> nom seul, aucune adresse A
nslookup us10018.nordvpn.com  -> nom seul, aucune adresse A
al41.nordvpn.com              -> 87.120.102.83 (vivant)
```

Les tunnels se connectent à des IP `155.133.5.x` / `185.215.181.x` qui refusent ou ne répondent pas. Logs Gluetun :

```text
[warn] restarting VPN because it failed to pass the healthcheck: startup check: ... lookup cloudflare.com: i/o timeout
[openvpn] UDPv4 link remote: [AF_INET]155.133.5.2:1194
[socks5] dial tcp 34.160.111.145:443: connect: connection timed out
```

Aucun conteneur n'a de route `tun0` ; seul `eth0` est présent. Le healthcheck Docker teste `wget http://api.ipify.org` **à travers le tunnel** (`docker-compose.yml:278-283`) : quand le tunnel est mort, il échoue et déclenche un restart, mais un restart ne peut pas réparer un hostname mort.

### 2.5 WARP : six conteneurs, cinq buckets

Sondes directes `socks5h://127.0.0.1:<port>` vers `https://api6.ipify.org` :

| Port | Label | Egress live | Bloc /64 | Bucket distinct |
|---:|---|---|---|---|
| 11080 | warp-ipv6 | `2a09:bac5:32d2:be::13:271` | `2a09:bac5:32d2:be::/64` | oui |
| 11085 | warp-ipv6-2 | `2a09:bac1:27a0:2060::3db:41` | `2a09:bac1:27a0:2060::/64` | **non — doublon** |
| 11086 | warp-ipv6-3 | `2a09:bac5:32d5:14f0::216:cd` | `2a09:bac5:32d5:14f0::/64` | oui |
| 11087 | warp-ipv6-4 | `2a09:bac1:27a0:2060::219:9e` | `2a09:bac1:27a0:2060::/64` | **non — doublon** |
| 11103 | warp-restore1 | `2a09:bac1:27c0:2060::13:225` | `2a09:bac1:27c0:2060::/64` | oui |
| 11105 | warp-restore3 | `2a09:bac5:32d7:be::13:2b8` | `2a09:bac5:32d7:be::/64` | oui |

**6 endpoints, 5 `/64` uniques** : la sixième station ne peut pas ajouter de quota. Deux endpoints ont été recréés dans le même bloc Cloudflare sans que le système le détecte.

### 2.6 WARP : configuration persistée périmée

`config.yaml` (lignes 290-381) enregistre des `egress_ip`/`block` qui ne correspondent plus au live pour **5 endpoints sur 6** :

| Label | Config `egress_ip` | Live | Écart |
|---|---|---|---|
| warp-ipv6 | `2a09:bac5:32d0:14dc::214:83` | `2a09:bac5:32d2:be::13:271` | oui |
| warp-ipv6-2 | `2a09:bac5:32d6:26b4::3db:f` | `2a09:bac1:27a0:2060::3db:41` | oui |
| warp-ipv6-3 | `2a09:bac5:32d4:14dc::214:bf` | `2a09:bac5:32d5:14f0::216:cd` | oui |
| warp-ipv6-4 | `2a09:bac1:27a0:2060::219:9e` | idem | non |
| warp-restore1 | `2a09:bac5:32d3:28a0::40c:3a` | `2a09:bac1:27c0:2060::13:225` | oui |
| warp-restore3 | `2a09:bac5:32d6:be::13:283` | `2a09:bac5:32d7:be::13:2b8` | oui |

La persistance n'a lieu que lors d'une rotation gérée (`free/pool.py:2938-2952`). Un redémarrage externe du conteneur change l'IP sans passer par le rotator : la config, le ledger et la détection de doublons restent sur l'ancienne valeur.

### 2.7 Capacité : 6 annoncés, 5 réels

`GET /api/egress/capacity` ne peut pas être interrogé (proxy non démarré), mais le calcul est lisible :

- `_egress_capacity_counts()` compte chaque endpoint activé (`dashboard/api.py:1599-1609`) ;
- `FreeIPPool.egress_endpoint_counts()` compte chaque endpoint `_socks5_usable` sans dédupliquer les blocs (`free/pool.py:4198-4220`) ;
- le conseiller 010 raisonne en « endpoints », pas en « buckets uniques » (`free/capacity.py:10-11`, `155-200`).

Conséquence : avec 6 endpoints WARP dont deux partagent un `/64`, le rapport peut afficher `warp.usable = 6` et `verdict = ok`, alors que la capacité de quota réelle est **5**.

### 2.8 Registre et source de vérité

`logs/registry.db` contient :

```text
catalog          9788
servers          1175
egress_ips       3205
quota_buckets    2772
leases             92
observations     1626
incidents         157
endpoints           0
station_state       0
server_transports   0
repair_jobs         0
```

La constitution III dit que `registry.db` est l'autorité pour les endpoints, l'état des stations, les transports, les leases et les réparations. Dans les faits, **les tables qui porteraient le cycle de vie des stations sont vides** ; la décision runtime vient de `config.yaml`, `.env`, des JSON `logs/*` et de la mémoire du processus. C'est une violation structurelle, pas un détail.

### 2.9 Proxy

`pythonw opencode.py` (PID 14704) est vivant, mais aucune socket n'écoute sur `:4000`. Le statut persisté `logs/vpn-status-now.json` est périmé (2026-11-02) et contient `boot_error: "boot 2/6: connected 2 ... reconcile/pool"`. La supervision applicative ne peut donc pas exposer l'état réel ni servir les clients.

**Mise à jour 14:48** : le proxy a fini par écouter (`GET /healthz` → 200), mais le statut est **faux** :

```text
GET /api/vpn-status
  status                     = connected
  usable_stations            = 6
  unique_usable_ips          = 6
  active_stations_with_quota = 6
  capacity_degraded          = false
  boot_error                 = "boot 0/6: connected 0 ... reconcile/pool"

station_health[1..6]
  state=repairing, usable=false, last_error=auth   (6/6)

docker ps
  5 conteneurs NordVPN, tous unhealthy, station 2 absente
```

C'est un **faux 6/6** : les compteurs de capacité ne dérivent pas de la même
preuve que `station_health`. Il viole directement les principes I, VII et X de
la constitution. Voir F-32.

---

## 3. Cartographie du cycle de vie

### 3.1 NordVPN

#### Création / provisioning

| Étape | Mécanisme | Fichier |
|---|---|---|
| Nombre désiré | `ip_rotation.station_count` (1–10), défaut 6 | `config.yaml:403` |
| Services compose | `vpn-gluetun` (station 1), `vpn-gluetun-2..10` (profils `dual-station`/`multi-station`) | `docker-compose.yml:185-553` |
| Ports | station N : SOCKS `1079+N`, HTTP `8887+N`, loopback uniquement | `docker-compose.yml:200-202, 389-391` |
| Stack | `VPN_TYPE_STATION{n}` (openvpn/wireguard) | `docker-compose.yml:219, 397, 420...` |
| Host pin | `SERVER_HOSTNAMES_STATION{n}` | `docker-compose.yml:243, 395...` |
| Credentials | `credentials.env`, `vpn_configs/credentials.txt`, `vpn_configs/wireguard.env` | `docker-compose.yml:203-213` |
| Catalogue | `docs/nordvpn_sources.yaml`, `logs/nordvpn_api.json`, `logs/gluetun_manifest.json`, `registry.db` | `docs/nordvpn-sources.md` |
| Image | `qmcgaw/gluetun` épinglée par digest | `docker-compose.yml:186` |

Le manager crée/recrée via `_compose_up` / `_ensure_container` (`vpn/manager.py:6498-6557`) et pousse l'environnement via `_compose_env` (`vpn/manager.py:5394-5485`). Le downscale appelle `stop_container` (`vpn/manager.py:2233`).

**Défaut** : il n'y a pas d'objet « station désirée » unique. Le `.env`, le stack mémoire, le compose et le processus Python peuvent chacun imposer une valeur différente. La création n'est donc pas idempotente au sens fort : deux writers peuvent recréer la même station avec deux stacks différents.

#### Gestion

- `VPNManager` par station : `connect` (`vpn/manager.py:2255`), `connect_next` (`:2384`), `refresh_status` (`:2876`), `health_check` (`:3453`), `restart` (`:5528`).
- Sélection de transport : `transport_order` (`config.yaml:438-441`), cascade (`cascade_enabled: true`, `:113`), auto-flip (`:93-99`).
- Rotation de pays : `country_rotation`, `country_offset`, `pin_widen_*` (`config.yaml:122-124`, `237-240`).
- Gestion GUI/API : `/api/vpn-status`, `/api/vpn-config`, `/api/vpn-stack-info`, `/api/vpn/station/{id}/logs` (`dashboard/api.py:3691, 3723, 4253, 4289, 4362`).

**Défaut** : `station_repair.enabled: false` (`config.yaml:425`) alors que le watchdog et le superviseur continuent d'agir. Le propriétaire de la réparation n'est pas unique (constitution VIII).

#### Maintenance

| Mécanisme | Emplacement | Comportement |
|---|---|---|
| Watchdog | `vpn/manager.py` (heartbeat, `:2149`, `:5528`) | restart, backoff, budget (`watchdog_max_restarts_per_hour: 3`) |
| Healthcheck Docker | `docker-compose.yml:278-283` | `wget api.ipify.org` via tunnel, 12 s, 2 retries, start 60 s |
| `StationHealth` | `free/station_health.py:38-266` | états `unknown/starting/connected/degraded/parked/disabled`, probes bonnes/mauvaises, park |
| `station_repair.py` | `station_repair.py` | policy `restart/recreate/park`, `GlobalAuthGuard`, budgets |
| Superviseur | `scripts/fresh_chain_supervisor.py` | chaînes par station, claims, leases, warm pool, maintenance DB |
| Allocator | `free/allocator*`, `scripts/fresh_chain*.py` | sélection de serveur/transport, leases |
| Auto-update | `config.yaml:443-447` | fenêtre 03:00–05:00, idle 15 min |

**Défaut** : le healthcheck ne distingue pas `tunnel_down`, `server_dead`, `auth_failed`, `quota_429`, `dns_dead`. Un serveur mort et un quota épuisé produisent le même `unhealthy`, donc le même restart. Le restart ne peut réparer ni un hostname mort ni un quota.

#### Rotation

- Déclencheurs : 429/quota (`on_429_action: rotate`, `config.yaml:224`), égress mort (`egress_flip_after_s: 60`), serveur refusé, rotation manuelle, SLO.
- Budgets : `station_max_rotations_per_hour: 10`, `rotation_storm_cooldown_s: 30`, `rotation_concurrency: 1`, `rotation_stagger: 10` (`config.yaml:408, 272-280`).
- Leases et unicité : `registry.db` (`leases`, `claims`, `egress_ips`), `free/rotation.py`, `free/ip_registry`.
- Post-rotation : probe quota (`post_rotation_probe`, `config.yaml:241-244`), commit IP (`vpn/manager.py:4302-4458`), ledger (`free/quota_ledger.py`).

**Défaut** : la rotation est bloquée en amont par le pin mort. Le fast-pin (`vpn/manager.py:6372`) est censé changer de serveur, mais le stack reconcile supprime le conteneur avant/après, et le superviseur est idle car NordVPN n'est pas sélectionné. Les budgets ne sont donc jamais le facteur limitant : on boucle sur les mêmes serveurs morts.

#### Observabilité

- `logs/debug.log` (churn, IP probes), `logs/vpn-status-now.json` (snapshot), `logs/registry.db` (incidents, leases, quota), `logs/station_incidents.db`.
- GUI : pages VPN, carte capacité 010, logs par station.
- `station_contract()` (`free/station_health.py:354`) expose les preuves.

**Défaut** : le statut principal est un JSON écrit par le processus et périmé dès que le processus meurt. `connected` n'est pas `client_usable` ; le statut live n'est pas dans le registre. La constitution I exige des fenêtres 1 h/24 h/7 j/30 j et des échantillons bruts : ce n'est pas branché pour les stations.

### 3.2 WARP

#### Création / provisioning

| Étape | Mécanisme | Fichier |
|---|---|---|
| Déclaration | `ip_rotation.socks5_proxies[]` avec `provider: warp` | `config.yaml:290-381` |
| Conteneur | `caomingjun/warp:latest`, `--cap-add NET_ADMIN`, `-p port:1080`, `--restart unless-stopped` | `free/warp_rotation.py:315-333` |
| IP / bloc | `egress_ip`, `block` (`/64`), `ipv6: true` | `config.yaml:304-380` |
| Résolution egress | `api6.ipify.org` (IPv6), `api.ipify.org` (IPv4) | `free/warp_rotation.py:42-43, 366-399` |
| Gestion | `FreeIPPool.set_socks5_proxies` | `free/pool.py:3946-3991` |

**Défaut** : la création n'est pas un processus déclaratif. Elle est faite par `docker run` dans le rotator (`free/warp_rotation.py:315-333`) et par des commandes manuelles/scripts. Il n'y a pas de vérification d'unicité du `/64` à la création, ni de portée réseau `127.0.0.1` (les six conteneurs publient sur `0.0.0.0`).

#### Gestion

- Endpoint model : `free/pool.py` (`Socks5Endpoint`, `_socks5_eps`, `_socks5_usable`, `_socks5_enabled_eps`).
- Sélection : `free/auto_egress.py` (poids, round-robin), `free/fallback.py` (déclencheurs, cooldown), `free/egress_provider.py` (rotate/refresh/health).
- Persistance : `_persist_socks5_egress` (`free/pool.py:2938-2952`).
- API/GUI : `/api/egress/capacity`, inventaire endpoints, cases à cocher (`dashboard/api.py:1536-1610, 4302-4360`).

**Défaut** : pas de réconciliation périodique de l'egress. `refresh_egress` n'est appelé que si l'IP est vide ou au premier service (`free/pool.py:4139-4148, 4513-4518`). Un redémarrage externe du conteneur passe inaperçu.

#### Maintenance

- Santé conteneur : healthcheck de l'image (healthy/unhealthy).
- `_socks5_usable` : bad marks, `bad_until`, `degraded_override`, `consec_429`, quota.
- Park / ledger : `free/quota_ledger.py`, `free/pool.py`.
- Nettoyage : pas de job dédié ; un conteneur mort reste déclaré.

**Défaut** : pas de maintenance planifiée des endpoints WARP (rotation préventive, vérification du `/64`, nettoyage des conteneurs orphelins). La santé est observable mais pas auto-réparée.

#### Rotation

- `WarpRotator.rotate` : restart d'abord, recreate ensuite, jusqu'à 4 tentatives (`free/warp_rotation.py:179-259`).
- Rejet : `same_block`, `duplicate_block`, `quota_exhausted`, `health_timeout`, `egress_unresolved`.
- Verrou global `_ROTATE_LOCK` et `INFLIGHT` ; intervalle minimum 300 s (`:31-38`).
- Après succès : `_apply` met à jour l'endpoint, le ledger, la config (`:261-282`).
- Après échec : `_apply_tracking` conserve l'IP réelle et marque la station bad 60 s (`:284-313`).

**Défaut** : le rotator ne consulte pas `station_max_rotations_per_hour` et ne persiste pas `LAST_ROTATION` ; un redémarrage du processus remet le compteur à zéro. Le duplicate check se base sur les endpoints en mémoire ; si la config ou la mémoire est périmée, il ne voit pas le doublon.

---

## 4. Constats détaillés

### P0 — bloquants pour l'objectif « toutes les stations utilisables »

#### F-01 — Split-brain du stack et reconcile destructeur

**Preuve** : `.env` et `docker inspect` divergent sur 4 stations ; `logs/debug.log` montre `boot reconcile ... -> rm` toutes les ~10 min ; `_compose_env` impose le stack mémoire (`vpn/manager.py:5441-5444`) alors que le reconcile lit `.env` (`vpn/manager.py:11284-11288`).

**Impact** : les conteneurs sains sont détruits, les tunnels ne finissent jamais de converger, station 2 disparaît, le proxy ne démarre pas. C'est la cause première de la flotte NordVPN à 0/6.

**Correctif attendu** : un seul `DesiredStationSpec` (provider, stack, proto, host, pays, génération) écrit atomiquement ; `_compose_env` et le reconcile lisent ce même objet ; le reconcile ne supprime jamais un conteneur qui correspond au desired state ; une génération obsolète déclenche un recreate une seule fois, pas une boucle.

#### F-02 — Pins morts jamais remplacés

**Preuve** : `us10506`, `us10019`, `us10508`, `us10018` ne résolvent plus ; les conteneurs sont épinglés dessus ; `al41.nordvpn.com` résout et a des leases chauds (`warm_pool`).

**Impact** : tunnel impossible, healthcheck en échec, restart inutile, 429 sur les IP déjà brûlées.

**Correctif attendu** : résolution DNS avant pin ; TTL de pin ; bascule automatique vers un host du warm pool ou un host du catalogue validé ; interdiction de conserver un pin non résolvable plus de N minutes.

#### F-03 — Réparation désactivée et double propriétaire

**Preuve** : `station_repair.enabled: false` (`config.yaml:425`) ; watchdog 31 restarts/10 min ; `fresh_chain_supervisor.py:1236-1241` s'arrête si NordVPN n'est pas dans `egress_providers` ; `vpn/manager.py` continue de recréer.

**Impact** : personne ne répare vraiment ; le watchdog fait du churn ; le superviseur, seul outil capable de claims/leases, est idle. Violation VIII.

**Correctif attendu** : un seul propriétaire (superviseur) ; `enabled: true` ; le manager/watchdog ne recrée jamais une station que le superviseur répare ; la maintenance des stations est indépendante de la sélection trafic.

#### F-04 — Station manquante et comptage faux

**Preuve** : `opencode-vpn-2` absente ; `station_count: 6` ; `docker ps` 5 conteneurs ; `fresh_chain_supervisor` voit 6 stations `broken`.

**Impact** : la capacité annoncée est fausse ; le slot 2 n'existe pas ; les ports 1081/8889 ne sont pas servis.

**Correctif attendu** : le nombre de conteneurs est dérivé du desired state et vérifié à chaque cycle ; un slot manquant est recréé avant toute rotation ; le statut expose `configured/created/running/healthy/usable`.

#### F-05 — Doublon de `/64` WARP

**Preuve** : ports 11085 et 11087 → `2a09:bac1:27a0:2060::/64` ; 6 endpoints, 5 buckets ; `free/warp_rotation.py` refuse les doublons en mémoire mais pas les doublons issus d'un redémarrage externe.

**Impact** : la sixième station brûle le quota de la cinquième ; le débit/quota ne sont pas doublés ; l'objectif 6/6 est faux.

**Correctif attendu** : unicité du `/64` à la création et à chaque refresh ; un doublon déclenche une rotation ciblée ; le ledger et la capacité comptent les buckets uniques.

#### F-06 — Config WARP périmée

**Preuve** : 5/6 `egress_ip` config divergent du live ; `_persist_socks5_egress` n'est appelé que par le rotator (`free/pool.py:2938-2952`).

**Impact** : le ledger impute le quota au mauvais bloc ; la détection de doublons est aveugle ; le GUI affiche une fausse IP ; une rotation peut réutiliser un bloc brûlé.

**Correctif attendu** : réconciliation live → config → ledger à chaque cycle (ou à chaque changement d'IP détecté) ; `egress_ip`, `block`, `current_ip` et `quota_bucket` écrits ensemble ; alerte si le live ne correspond pas au persisté.

### P1 — dégradations majeures

#### F-07 — La capacité compte des endpoints, pas des buckets

**Preuve** : `dashboard/api.py:1599-1609`, `free/pool.py:4198-4220`, `free/capacity.py:10-11`.

**Impact** : verdict `ok` avec 6 endpoints et 5 `/64` ; le conseiller 010 ne peut pas garantir six stations réellement utilisables.

**Correctif attendu** : `unique_buckets` et `usable_buckets` dans le rapport ; `usable` = min(endpoints sains, buckets uniques) ; test 6 endpoints/5 blocs → `degraded`/`insufficient`.

#### F-08 — Le registre n'est pas la source de vérité des stations

**Preuve** : `endpoints=0`, `station_state=0`, `server_transports=0`, `repair_jobs=0` ; la décision vient de `config.yaml`/`.env`/JSON.

**Impact** : impossible de reconstruire l'état après crash ; les leases/quotas ne sont pas corrélés aux stations ; la constitution III n'est pas respectée.

**Correctif attendu** : écrire `DesiredStationSpec`, `StationState`, `Endpoint`, `Transport`, `Lease`, `RotationJob`, `RepairJob` dans `registry.db` ; migrer les JSON existants ; le runtime lit le registre, pas l'inverse.

#### F-09 — Proxy non démarré

**Preuve** : PID 14704 vivant, aucune socket `:4000` ; `boot_error` dans le statut ; boucle de reconcile.

**Impact** : aucun client ne peut être servi ; les métriques GUI/API sont indisponibles ; le travail de réparation n'est pas observable.

**Correctif attendu** : le boot doit être fail-fast mais non destructif ; le proxy doit écouter même si une station est down (mode dégradé explicite) ; un healthcheck `/healthz` doit distinguer `proxy_up` et `fleet_usable`.

#### F-10 — Transport et protocole incohérents

**Preuve** : `.env` station 1 `tcp`, conteneur `udp` ; station 3/4/6 `wireguard` vs `openvpn` ; `transport_order: wireguard, openvpn-udp, openvpn-tcp` non appliqué au pin.

**Impact** : les tunnels utilisent un transport qui ne correspond ni à l'intention ni au canary ; la cascade ne peut pas corriger.

**Correctif attendu** : le transport effectif est un champ du desired state ; un changement de transport passe par une génération et un recreate contrôlé ; le pin et le proto sont écrits ensemble.

#### F-11 — 429 sur IP fraîches et pools non alignés

**Preuve** : `fresh_chain_1.log` : `egress 92.119.17.178`, puis `free status=429 ra=41913` ; `fresh_chain_5.log` : `egress 92.119.19.217`, puis `429 ra=40690` ; `warm_pool` contient des IP `87.120.102.x` vérifiées sur `al41`.

**Impact** : le système dépense des rotations pour des IP déjà épuisées ; le warm pool n'est pas utilisé au bon moment ; les 429 sont traités comme des échecs de transport.

**Correctif attendu** : sélectionner d'abord les IP du warm pool non épuisées ; ne jamais réutiliser une IP avant `reset_at` ; distinguer `quota_429` de `transport_dead` ; si le pool chaud est vide, chercher un nouveau `/64` (WARP) ou un nouveau host (NordVPN) au lieu de boucler.

#### F-12 — Healthcheck trop grossier

**Preuve** : `docker-compose.yml:278-283` ; `health_check` (`vpn/manager.py:3453`) ; `_classify_probe_exc` (`:1069`) ; logs `public IP probe failed on all sources`.

**Impact** : le même `unhealthy` couvre DNS, TLS, auth, quota, serveur mort, route absente ; le restart est le seul remède, d'où le churn.

**Correctif attendu** : healthcheck interne par étapes (`container_running`, `tunnel_up`, `dns_ok`, `egress_ip`, `client_usable`) ; le healthcheck Docker ne doit pas être le seul signal ; chaque étape a une action de réparation propre.

#### F-13 — Création non idempotente / pas de nettoyage

**Preuve** : `WarpRotator._recreate` fait `rm -f` puis `run` sans vérifier les ports libres, les noms voisins ou un état partiel (`free/warp_rotation.py:315-333`) ; `boot reconcile` supprime des conteneurs (`vpn/manager.py:11241-11324`) ; station 2 absente.

**Impact** : collisions de ports, conteneurs orphelins, stations fantômes, re-créations en boucle.

**Correctif attendu** : création idempotente par nom + labels + génération ; préflight des ports ; rollback si `run` échoue ; jamais de `rm -f` sans preuve que le conteneur est obsolète ; un job de nettoyage des orphelins.

#### F-14 — Exposition réseau et token dashboard

**Preuve** : WARP publie `0.0.0.0:11080-11105` alors que `host: 127.0.0.1` dans la config ; `DASHBOARD_TOKEN not set while host is 0.0.0.0` dans `fresh_chain_*.log` ; `dashboard_trust.mode: lan` (`config.yaml:44-50`).

**Impact** : les proxies WARP sont accessibles depuis le LAN ; le dashboard est ouvert au LAN sans token. Un voisin peut consommer le quota ou lire les statuts.

**Correctif attendu** : bind `127.0.0.1` pour tous les endpoints WARP ; token obligatoire si `server.host=0.0.0.0` ; audit des ports publiés.

#### F-15 — Budgets de rotation WARP non persistés

**Preuve** : `MIN_ROTATION_INTERVAL_S = 300` et `LAST_ROTATION` en mémoire (`free/warp_rotation.py:31-38`) ; `station_max_rotations_per_hour` n'est pas lu par le rotator.

**Impact** : un restart du processus permet une rafale ; pas de plafond horaire par endpoint ; risque de rotation storm.

**Correctif attendu** : budget horaire persisté par endpoint, aligné sur `station_max_rotations_per_hour`, avec jitter et backoff.

#### F-16 — Pas de rotation préventive / pas de SLO par station

**Preuve** : `latency_rotation.enabled: false`, `quality_rotation.enabled: false` (`config.yaml:197-199, 254-256`) ; `station_health` observe mais ne déclenche pas de rotation préventive.

**Impact** : une station ne tourne que lorsqu'elle est déjà mauvaise ; l'objectif « toutes utilisables » est réactif, pas proactif.

**Correctif attendu** : rotation préventive sur quota bas, latence, âge de l'IP, échecs consécutifs ; seuils configurables ; jamais silencieuse.

### P2 — qualité, sécurité, dette

#### F-17 — `connected` ≠ `client_usable`

Le statut expose `status: connected` et `usable_station_count` peut compter une station qui n'a pas passé de probe client réelle. La constitution I et VII exigent une preuve de contenu, pas un `200` de page d'accueil.

#### F-32 — Faux 6/6 confirmé live (P0)

**Preuve** : `GET /api/vpn-status` le 2026-11-07 à 14:48 renvoie :

```text
usable_stations            = 6
unique_usable_ips          = 6
active_stations_with_quota = 6
capacity_degraded          = false
boot_error                 = "boot 0/6: connected 0 ... reconcile/pool"
```

alors que `station_health[1..6]` renvoie `state=repairing`, `usable=false`,
`last_error=auth` pour les six stations, et que `docker ps` ne montre que cinq
conteneurs NordVPN, tous `unhealthy`, sans station 2.

**Impact** : le GUI et le conseiller de capacité affichent un faux 6/6 ; les
décisions de rotation, de park et de dimensionnement sont prises sur des
compteurs qui ne dérivent pas de la preuve client. C'est la violation la plus
directe des principes I, VII et X.

**Correctif** : `usable_stations`, `unique_usable_ips`,
`active_stations_with_quota` et `capacity_degraded` doivent être calculés depuis
le même ensemble de slots `client_usable` que `station_health` ; un
`boot_error` à 0/6 ne peut pas coexister avec un 6/6 ; test de contrat
obligatoire.

#### F-33 — Duplication inter-providers (P1)

**Preuve** :

| Concept | NordVPN | WARP | Duplication |
|---|---|---|---|
| Normalisation de bloc | `block_for_ip` (`free/warp_rotation.py:58-65`) | `block_for_egress` (`free/egress_provider.py:15-22`) | même logique, deux fonctions |
| Résolution egress | `Socks5Endpoint.refresh_egress` (`free/pool.py:350-416`) | `WarpRotator._resolve_egress` (`free/warp_rotation.py:366-410`) | deux implémentations |
| Réparation | `station_repair.py` | branches ad hoc `free/pool.py` | deux politiques |
| Rotation budget | `_check_rotation_storm` (`vpn/manager.py:2348-2382`) | `MIN_ROTATION_INTERVAL_S` en mémoire (`free/warp_rotation.py:31-38`) | deux budgets |
| Statut | `VPNManager.get_status()` (`vpn/manager.py:5012-5291`) | `Socks5Endpoint.get_status()` (`free/pool.py:418-429`) | deux schémas |
| Logs | `vpn_manager` rattaché | `free.warp_rotation` non rattaché | deux pipelines |
| Provisioning | Compose + `_ensure_container` | `docker run` dans `_recreate` | deux chemins |

**Impact** : chaque correctif doit être appliqué deux fois ; les comportements
divergent (un provider park, l'autre non ; un provider a des logs, l'autre
non ; un provider compte des buckets, l'autre des endpoints). C'est une source
structurelle des P0.

**Correctif** : noyau commun `ops/lifecycle*` + adapters `NordVpnProvider` /
`WarpProvider` ; voir la feature 011, US7 et FR-043–FR-052.

#### F-18 — Statut JSON périmé

`logs/vpn-status-now.json` date du 2026-11-02 et n'est pas rafraîchi par le processus actuel. Le GUI peut afficher un état vieux de cinq jours.

#### F-19 — Dette de configuration

`.env` contient des clés contradictoires (`VPN_TYPE_STATION1=openvpn` + `SERVER_HOSTNAMES_STATION1=al41`), `station_count: 6` avec 5 conteneurs, `station_repair.enabled: false` avec un watchdog actif, `egress_providers: [warp]` avec six stations NordVPN qui tournent. La config n'est pas validée contre l'état runtime.

#### F-20 — Secrets et surface

`credentials.env`, `vpn_configs/wireguard.env`, `api_keys.json`, `DASHBOARD_TOKEN` absent. Les logs ne doivent jamais contenir les clés ; le contrôle Gluetun (`:8000`) n'est pas publié, ce qui est correct, mais le dashboard LAN reste ouvert, et les SOCKS5 WARP sont ouverts sans authentification (voir F-14).

### P0/P1 — constats WARP supplémentaires (audit détaillé)

#### F-21 — Rotation WARP : 4 recréations, aucun plafond horaire

**Preuve** : `free/warp_rotation.py:197-204` fait jusqu'à `MAX_ROTATION_ATTEMPTS = 4`
tentatives, avec `docker rm -f` + `run` à chaque tentative ≥ 2 ; `LAST_ROTATION`
est posé **avant** l'essai (`:114`), donc un échec immédiat coûte 300 s ;
`station_max_rotations_per_hour` n'est jamais lu par le rotator. Le verrou
`_ROTATE_LOCK` sérialise toutes les rotations WARP, ce qui transforme une
rafale en tempête lente.

**Impact** : 4 stations ont changé d'IP toutes les ~30 s pendant la fenêtre
04:10–04:23, avec ~90 rejets `post_rotation_probe`/`rotation_rejected`
(`logs/station_slo.jsonl`). Chaque recréation coupe les flux en cours et
consomme une registration Cloudflare.

**Correctif** : budget horaire persisté, une seule recréation par rotation,
backoff d'échec, jitter, et refus explicite au-delà du budget.

#### F-22 — Un conteneur WARP mort n'est jamais recréé

**Preuve** : `notify_connection_failure()` applique un bad-mark `timeout`
(`free/pool.py:5515-5517`) ; `_apply_bad_mark()` ne demande une rotation que
pour `rate_limit`/`quota` (`free/pool.py:2650-2654`). Aucun probe périodique
n'existe (`free/pool.py:4139-4148`), et `Socks5Endpoint.status` vaut
`"connected"` en dur (`free/pool.py:314`).

**Impact** : un conteneur tué, un daemon Docker arrêté ou une image corrompue
laisse la station « up » pour le pool jusqu'à un 429. La maintenance WARP est
réactive au quota, pas à la panne.

**Correctif** : sonde périodique de santé, état `failed`, recréation par le
provisioner, budget et preuve.

#### F-23 — Les logs de rotation WARP sont perdus

**Preuve** : `free/warp_rotation.py:22` utilise `logging.getLogger("free.warp_rotation")`,
mais seuls `vpn_manager`, `free_ip_pool` et `vpn_gate` sont rattachés à un
fichier (`opencode.py:587-591`, `dashboard/display.py:172-210`). Aucun handler
racine n'existe. `free.pool` (`free/pool.py:40`) est dans le même cas.

**Impact** : aucune trace des rotations, des rejets de probe ou des échecs de
persistance dans `logs/debug.log` ; l'audit live n'a pas pu trancher la cause
de la tempête. C'est une violation de la constitution XIII (traçabilité).

**Correctif** : rattacher `free.pool`, `free.warp_rotation`, `free.quota_ledger`
au fichier de logs ; test de présence.

#### F-24 — Pas de gel sur événement amont pour WARP

**Preuve** : la rotation Docker est gelée par `upstream_event` (`free/pool.py:4969-5043, 5800-5806`), mais les chemins WARP `_ledger_note_result` (`:1493-1497`), `_apply_bad_mark` (`:2650-2654`), `rotate_exhausted_managed` (`:2926`) et `on_quota_exhausted` SOCKS5 (`:5133-5150`) ne consultent pas ce gel.

**Impact** : une panne amont (429 global, incident provider) déclenche des
rotations WARP inutiles, qui brûlent des registrations et des buckets.

**Correctif** : le même gate `upstream_event` pour les deux providers, avec
une raison visible.

#### F-25 — La recréation WARP perd les options du conteneur

**Preuve** : `_recreate()` (`free/warp_rotation.py:315-333`) exécute
`rm -f` puis `docker run -d --name ... --cap-add NET_ADMIN -p port:1080
--restart unless-stopped image`, sans `-e`, sans limites mémoire/PID, sans
labels, sans devices. L'image est `caomingjun/warp:latest` (`:24`).

**Impact** : licence WARP+, hostname, DNS, limites, labels et options de
diagnostic sont perdus à la première recréation ; l'image peut changer sans
changement de code ; un conteneur peut consommer toutes les ressources.

**Correctif** : provisioner déclaratif qui réapplique toutes les options,
image épinglée par digest, limites explicites.

#### F-26 — Le statut WARP masque les champs utiles

**Preuve** : `Socks5Endpoint.get_status()` (`free/pool.py:418-429`) n'expose
pas `egress_ip`, `block`, `managed`, `container`, `ipv6`, `label` ;
`get_status()` force `duplicate_ip_groups: 0` (`:7193`) et
`parked_stations: 0` (`:7199`) en mode SOCKS5 ; `capacity_snapshot()` force
aussi `parked_stations: 0` (`:1287`).

**Impact** : le GUI ne peut pas montrer le bucket réel, l'état de park ou un
doublon. L'opérateur voit un faux « OK ».

**Correctif** : exposer tous les champs, calculer les doublons et les parks,
ne jamais forcer zéro.

#### F-27 — La reconstruction depuis la config écrase l'egress appris

**Preuve** : `set_socks5_proxies()` / `_rebuild_static_eps()`
(`free/pool.py:3946-4069`) reconstruisent tous les endpoints depuis
`config.yaml` et réinjectent `egress_ip`/`block` du fichier
(`:4040-4055`). Tout hot-reload (`:6335-6336`) peut donc régresser une IP
apprise par rotation si elle n'a pas été persistée.

**Impact** : le pool sert une IP périmée, le ledger impute au mauvais bloc,
la détection de doublon est aveugle.

**Correctif** : le registre est la source ; la config n'est lue qu'au boot ou
pour une modification explicite ; le live gagne toujours sur le fichier.

#### F-28 — Deux axes de quota incohérents

**Preuve** : le ledger journalier utilise la clé `/64`
(`free/quota_ledger.py:38-61, 324-326`) ; `free/ip_quota.py` utilise
`(model, current_ip)` avec l'IP complète (`:284-290`). `block_key` et
`quota_axis` de `Socks5Endpoint` sont calculés une seule fois au constructeur
(`free/pool.py:308-309`) et ne sont pas rafraîchis après rotation.

**Impact** : deux adresses du même `/64` ont deux leases « qualité » mais un
seul bucket ledger ; la couche qualité croit à deux slots. Le quota serveur
est partagé.

**Correctif** : une seule fonction de normalisation, rafraîchie à chaque
changement d'IP ; le ledger est l'autorité.

#### F-29 — Persistance SQLite : `model='default'` en dur

**Preuve** : `_save_sqlite()` écrit `model='default'` (`free/quota_ledger.py:698-703`)
alors que la clé primaire inclut `model` (`rotation/infrastructure/db/schema.sql:155-171`) ;
`provider` n'est ni écrit ni lu.

**Impact** : `ledger_bucket_mode=model_prefix` ne survit pas à un redémarrage ;
l'attribution par provider est perdue.

**Correctif** : écrire `model` et `provider` réels, migration et test de
redémarrage.

#### F-30 — Provisioning WARP absent et orphelins non nettoyés

**Preuve** : aucun `WarpProvisioner`, `create_endpoint` ou `ensure_endpoint`
dans le code ; le seul `docker run` est dans `_recreate()` ; le contrat 005
est explicitement « conception, aucun code » (`specs/005/.../contracts/egress-provisioning.md:3`).
Aucun `docker ps` de contrôle pour WARP, aucun job d'orphelins.

**Impact** : les conteneurs sont créés à la main, hors IaC ; un conteneur
renommé/dupliqué n'est jamais détecté ; la création des 6 endpoints n'est pas
reproductible.

**Correctif** : `free/warp_provisioner.py` plan/apply, labels, adoption des
existants, nettoyage des orphelins, idempotence.

### P0/P1 — constats NordVPN supplémentaires (audit détaillé)

| # | Sév. | Constat | Preuve | Correctif |
|---|---|---|---|---|
| G1 | **P0** | Flotte dormante : le manager ne se connecte plus au boot quand `repair_owner=supervisor`, et le superviseur s'arrête si NordVPN n'est pas sélectionné. Personne ne démarre/répare. | `vpn/manager.py:2128-2140` ; `scripts/fresh_chain_supervisor.py:1236-1241` ; `opencode.py:3431-3433` | Un propriétaire de démarrage explicite, indépendant de la sélection trafic ; `start()` doit connecter si la station est désirée. |
| G2 | **P1** | Courses multi-processus sur `.env` : manager (lock de process), superviseur (`write_text` non atomique), chaînes (`\.env.tmp` fixe partagé). | `vpn/manager.py:90, 7657-7692` ; `fresh_chain_supervisor.py:1104-1121` ; `fresh_chain.py:1263-1290` | Un seul writer atomique (fichier temporaire unique + lock inter-processus), ou registre comme source et `.env` généré. |
| G3 | **P1** | Pins fantômes stations 7–10 dans `.env` alors que `station_count=6` ; non purgés par `_sync_stack_env`. | `.env:15-43` ; `vpn/manager.py:7681-7684` | Purger les `SERVER_HOSTNAMES_STATIONn`/`SERVER_COUNTRIES_STATIONn` au-delà du count, ou les marquer stale. |
| G4 | **P1** | Clés `station_repair` inertes : `max_recreates_per_hour`, `park_after_failures`, `verify_egress_before_usable`, `last_good_first` non lues par le watchdog ; `max_restarts_per_hour` doublé. | `station_repair.py:117-131` ; `vpn/manager.py:1856-1857, 8656-8661` ; `free/pool.py:7027` | Une seule politique lue par le propriétaire ; tests de chaque clé. |
| G5 | **P2** | Backoff auth `[30,60,120]` : la valeur 120 s est inatteignable (2 STOP, index clampé). | `station_repair.py:150-157` ; `config.yaml:410-413` | Séquence alignée sur le nombre de valeurs, ou backoff exponentiel explicite. |
| G6 | **P1** | Auto-update incompatible avec l'image digest-pinnée : `check_update` raisonne sur le tag, `apply_update` fait `--pull never`, le rollback retague le tag. | `docker-compose.yml:26` ; `vpn/manager.py:9318-9349, 9402-9416, 9467-9493` ; `ops/updater.py:26-33, 108-121` | Updater basé sur digest, ou image taggée + digest vérifié ; test `compose config --images`. |
| G7 | **P1** | Collisions de ports non validées ; upscale `connect()` en échec persiste quand même `station_count`. | `opencode.py:2586-2595, 2706-2712` ; `dashboard/api.py:4547-4561` | Préflight `compose config`/ports avant apply ; rollback du count et `rm` de la station partielle. |
| G8 | **P1** | `disabled_stations` comptées dans `configured_stations` : `configured=6`, `usable=5`, dégradation permanente. | `free/pool.py:6910-6923, 6968-6974` ; `opencode.py:3424-3430` | Filtrer `_disabled` dans configured, ou exposer `disabled` séparément. |
| G9 | **P1** | `/api/vpn/next` et `pin_country` contournent `repair_owner` et peuvent courser une chaîne en cours. | `dashboard/api.py:4805, 4945-4979` ; `free/pool.py:4822, 5785-5796` | Passer par le superviseur ; verrou par station ; refuser si un job est en cours. |
| G10 | **P1** | Secret de control dupliqué dans `.env` (injecté dans le proxy) ; `GET /api/vpn/diagnostic` sans token. | `.env:26` ; `docker-compose.yml:121-122` ; `dashboard/api.py:5801-5876` | Secret uniquement dans `credentials.env` ; token obligatoire sur diagnostic. |
| G11 | **P1** | Le refresh des credentials ne met pas à jour `vpn_configs/wireguard.env` : les stations WG gardent l'ancienne clé. | `scripts/refresh_nord_credentials.py:47-98` ; `docker-compose.yml:36-37` | Écrire la clé NordLynx dans le même refresh, atomiquement, puis restart contrôlé. |
| G12 | **P2** | Dérives docs/vérificateurs : `verify_100.py` exige 4 stations, docs parlent `:latest`, stagger 30, allocator `enforce`. | `scripts/verify_100.py:62-67` ; `docs/runbook-docker.md:104-105, 135` ; `config.yaml:111, 403` | Réconcilier les docs et le vérificateur avec la config 6 stations. |
| G13 | **P3** | `nordvpn_scan.py` cible la station 7 par défaut alors que `station_count=6`. | `scripts/nordvpn_scan.py:672` | Dériver `--stations` de `station_count`. |
| G14 | **P2** | URLs du plan de contrôle codées en dur sur `127.0.0.1:4000` dans le superviseur et les chaînes. | `fresh_chain_supervisor.py:372-383` ; `fresh_chain.py:2539` | Lire `server.host`/`server.port`. |
| G15 | **P2** | `current_server` = nom de conteneur, pas hostname NordVPN ; trompeur pour l'audit. | `vpn/manager.py:2320-2323, 2579-2582, 6295-6357` | Exposer `current_host` comme serveur, garder le conteneur séparé. |
| G16 | **P2** | `repair_owner` non hot-reloadable : changer la clé n'a aucun effet sans restart. | `vpn/manager.py:1868-1870, 4611-4950` | Handler `update_config` + réévaluation du propriétaire. |
| G17 | **P2** | Le changement de `proxy_mode` ne démarre pas les tunnels ; il faut un `connect` séparé. | `dashboard/api.py:5601-5603` | Déclencher `start/connect` si la flotte devient `vpn`, ou documenter explicitement. |
| G18 | **P1** | En déploiement compose, `opencode-proxy` dépend de la santé de la station 1 : si elle est AUTH_FAILED, le proxy ne démarre pas même si 2–6 sont saines. | `docker-compose.yml:181-183, 43-48, 278-283` | `service_started` + healthcheck applicatif, ou dépendance à une station désignée. |
| G19 | **P1** | Échec d'upscale non annulé : la station partielle reste, `station_count` est persisté, le superviseur consomme des rebinds. | `opencode.py:2586-2595, 2706-2712` ; `free/catalog_claims.py:357-376` | Rollback complet (count, registry, conteneur) sur échec. |
| G20 | **P2** | `_persist_vpn_config` réécrit `config.yaml` via `yaml.dump` : perte des commentaires et du formatage. | `dashboard/api.py:918-919, 1128, 1143-1144` | Écriture ciblée (ruamel/yaml préservant les commentaires) ou fichier séparé. |
| G21 | **P3** | `make_credentials_env` non atomique. | `scripts/make_credentials_env.py:58-81` | tmp + `os.replace` + fsync. |
| G22 | **P3** | `refresh_nord_credentials.py` non atomique et sans restart ; l'opérateur peut croire la rotation appliquée. | `scripts/refresh_nord_credentials.py:92-98` ; `dashboard/api.py:4902-4905` | Écriture atomique + restart contrôlé + preuve. |
| G23 | **P2** | Deux repair owners possibles si `repair_owner=manager` et `station_repair.enabled=true` : watchdog + superviseur. | `vpn/manager.py:9743-9754` ; `fresh_chain_supervisor.py:545-569` | Verrou inter-processus par station, un seul job. |
| G24 | **P2** | `IpAllocator` n'est pas le chemin de production ; le superviseur utilise `CatalogClaims` + binder + probe. | `free/ip_registry/allocator.py:1-5` ; `fresh_chain.py:1717-1745` | Soit brancher `IpAllocator`, soit documenter le chemin réel et supprimer le doublon. |
| G25 | **P3** | Downscale tail-only : on ne peut retirer qu'une station de fin ; `disabled_stations` laisse le numéro. | `opencode.py:2639` | Documenter, ou supporter un slot désactivé propre. |



---

## 5. Matrice de risques

| Risque | Probabilité | Impact | Priorité |
|---|---|---|---|
| Boucle de reconcile détruit les stations | certaine | flotte 0/6, proxy down | P0 |
| Faux 6/6 dans l'API/GUI | certaine | décisions et dimensionnement faux | P0 |
| Aucun propriétaire ne démarre/répare NordVPN | certaine | flotte dormante après reboot | P0 |
| Courses `.env` multi-processus | élevée | pin perdu, station sans host | P1 |
| Upscale en échec non rollbacké | élevée | station fantôme, budget rebinds | P1 |
| `depends_on` santé station 1 | moyenne | proxy down en compose | P1 |
| Pin mort non remplacé | certaine | 0/6 NordVPN | P0 |
| Doublon `/64` WARP | certaine | quota gaspillé, 5/6 | P0 |
| Config WARP périmée | certaine | ledger faux, doublons | P0 |
| Superviseur idle | certaine | pas de réparation | P0 |
| Capacité 6/6 fictive | certaine | décisions fausses | P1 |
| Registre vide | certaine | pas de reprise après crash | P1 |
| Proxy non démarré | certaine | aucun service client | P1 |
| Ports WARP LAN | certaine | consommation/quota | P1 |
| Rotation storm | moyenne | quotas brûlés | P1 |
| SOCKS5 WARP ouvert sur le LAN | certaine | consommation/quota, surface | P0 |
| Conteneur WARP mort jamais recréé | élevée | station fantôme | P1 |
| Logs de rotation perdus | certaine | diagnostic impossible | P1 |
| Reconstruction config écrase l'egress | élevée | IP/ledger faux | P1 |
| 429 sur IP fraîches | élevée | épuisement | P1 |
| Secrets loggés | faible | fuite | P2 |

---

## 6. Écarts avec la constitution

| Principe | Écart constaté | Preuve |
|---|---|---|
| I — disponibilité mesurée | `connected`/`healthy` tient lieu de `client_usable` ; pas de fenêtres 1 h/24 h/7 j/30 j pour les stations | `free/station_health.py`, `logs/vpn-status-now.json` |
| II — trois cycles de vie | station, session et IP sont mélangées dans `.env`/config/JSON | `.env`, `config.yaml`, `registry.db` |
| III — une source de vérité | `endpoints=0`, `station_state=0`, `server_transports=0` ; config/JSON décident | `registry.db` |
| IV — reset fournisseur | 429 frais rejoués avant `reset_at` ? Les logs montrent des 429 immédiats sur IP fraîches | `fresh_chain_*.log` |
| V — unicité IP | deux endpoints WARP partagent un `/64` | `config.yaml`, sondes |
| VII — preuve de succès | pas de probe client réelle avant `usable` | `healthcheck` Docker |
| VIII — un seul réparateur | watchdog + superviseur + manager agissent | `logs/debug.log`, `fresh_chain_supervisor.log` |
| X — dégradation explicite | le proxy ne démarre pas, donc aucun état dégradé exposé | PID 14704 sans `:4000` |
| XI — fallbacks | la cascade est bloquée par le pin et le reconcile | `vpn/manager.py` |
| XII — options GUI effectives | `station_repair.enabled: false` mais réparation active ; capacité non unique | config + code |
| XIII — traçabilité | pas de `station_state`/`repair_jobs` ; statut JSON périmé | `registry.db` |
| XIV — modularité | `vpn/manager.py` > 11 000 lignes, plusieurs writers | `vpn/manager.py` |

---

## 7. Ce qu'il faut préserver

- Le `WarpRotator` : restart → recreate → probe → persist, avec refus des blocs dupliqués et verrou global.
- Le ledger de quota par bloc (`/64` IPv6, `/32` IPv4) et le reset fournisseur.
- Le catalogue NordVPN et le warm pool (`87.120.102.x` sur `al41`) : ils contiennent déjà des IP validées.
- L'isolation loopback des stations NordVPN et l'image Gluetun épinglée.
- Le conseiller de capacité 010, à condition de le brancher sur les buckets uniques.
- Les tests existants (`tests/test_warp_rotation.py`, `test_capacity.py`, `test_station_*`, `test_quota_ledger*`) : ils constituent un bon filet de sécurité.

---

## 8. Plan de remédiation (résumé)

Le plan complet, les exigences, le modèle de données, le contrat et les tâches sont dans `specs/011-station-fleet-usability/`.

### Phase 0 — Arrêter l'hémorragie (P0, même jour)

1. Désactiver le reconcile destructeur ou le rendre non destructif ; geler les writers concurrents.
2. Rendre le `.env` et le stack mémoire identiques ; une seule génération par station.
3. Remplacer les pins morts par des hosts vivants (warm pool `al41` ou catalogue validé) ; recréer la station 2.
4. Réconcilier les IP WARP live → config → ledger ; rotation ciblée du doublon `2a09:bac1:27a0:2060::/64`.
5. Démarrer le proxy et vérifier `:4000` + `/healthz`.
6. Activer `station_repair` et un seul propriétaire ; laisser le superviseur réparer.
7. Rendre le démarrage/connexion indépendant de la sélection trafic : le manager ou le superviseur doit connecter les stations désirées même si `egress_providers=[warp]`.
8. Sérialiser les écritures `.env` entre processus ; préflight des ports et rollback de l'upscale.
9. Publier les SOCKS5 WARP sur `127.0.0.1` et rattacher les loggers de rotation.
10. Remplacer `depends_on: service_healthy` de la station 1 par un démarrage non bloquant.

### Phase 1 — Desired state et reconcile idempotent

- `DesiredStationSpec` unique, génération, écriture atomique.
- Reconcile `desired → actual` sans `rm -f` injustifié ; preflight ports/noms ; rollback.
- `registry.db` comme source de vérité ; migration des JSON.

### Phase 2 — Cycle de vie et maintenance

- Machine à états par station (`created → starting → tunnel_up → dns_ok → egress_ok → client_usable → degraded/parked/failed`).
- Classification des échecs (`auth`, `server_dead`, `dns`, `transport`, `quota_429`, `healthcheck`).
- Réparation par étape, budgets, backoff, park, reprise ; un seul réparateur.
- Healthcheck interne + probe client réelle (contenu, pas seulement 200).

### Phase 3 — Rotation et unicité

- Rotation NordVPN : host vivant, transport, pays, IP unique, lease, post-probe.
- Rotation WARP : `/64` unique, budget horaire persisté, refresh egress, ledger.
- Rotation préventive sur quota/latence/âge ; jamais silencieuse ; journalisée.
- Respect strict de `reset_at` et des 429.

### Phase 4 — Capacité et observabilité

- Compter `configured/created/running/healthy/egress_ok/client_usable/unique_buckets`.
- Verdict `ok/degraded/insufficient` basé sur les buckets uniques et les preuves.
- GUI : état par station, preuve, dernier probe, quota, bloc, prochaine action.
- API : `/api/stations`, `/api/stations/{id}`, `/api/stations/{id}/rotate`, `/api/stations/{id}/repair`, `/api/capacity`.

### Phase 5 — Sécurité et exploitation

- Bind WARP `127.0.0.1` ; token dashboard obligatoire si `0.0.0.0`.
- Rotation des secrets, absence de secrets dans les logs, audit des ports.
- Runbook de reprise après crash ; backup/restore du registre ; tests de chaos.

### Phase 6 — Validation live

- 6/6 NordVPN et 6/6 WARP, 12 buckets uniques, 24 h sans dérive.
- 0 doublon, 0 `rm -f` injustifié, 0 station fantôme.
- MTTR < 180 s, < 3 restarts/h/station, 0 faux `ok`.

---

## 9. Annexes

### A. Commandes de diagnostic

```powershell
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
docker inspect opencode-vpn-3 --format "{{.State.Health.Status}}"
docker logs --tail 60 opencode-vpn-3
docker exec opencode-vpn-3 sh -c "ip route; wget -qO- -T 5 http://api.ipify.org"
curl.exe -s --max-time 10 --socks5-hostname 127.0.0.1:11080 https://api6.ipify.org
python C:\Users\julie\AppData\Local\Temp\opencode\dbpeek.py logs\registry.db
Get-NetTCPConnection -LocalPort 4000 -State Listen
```

### B. Fichiers de preuve

- `logs/debug.log` (reconcile, watchdog, IP probes)
- `logs/fresh_chain_supervisor.log` (cycles, idle NordVPN)
- `logs/fresh_chain_*.log` (claims, egress, 429)
- `logs/registry.db` (incidents, leases, quota, egress_ips)
- `config.yaml` (socks5_proxies, station_count, repair, providers)
- `.env` (stack/proto/host par station)
- `docker inspect` (état réel)

### C. Références

- `specs/010-egress-station-sizing/` — dimensionnement, capacité.
- `specs/007-warp-egress-fallback/` — fallback WARP.
- `docs/AUDIT_STATIONS_IP_2026-10-26.md`, `docs/PLAN_REPARATION_STATIONS_2026-10-31.md`, `docs/PLAN_RETABLISSEMENT_FREE_2026-11-05.md`.
- `.specify/memory/constitution.md` — principes I–XIV.
